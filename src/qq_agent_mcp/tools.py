"""MCP Tools definitions."""

import asyncio
import hashlib
import logging
import random
import re
import time
import unicodedata
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

import aiohttp
from mcp.server.fastmcp import Context
from mcp.types import SamplingMessage, TextContent

from .config import Config
from .context import ContextManager, Message
from .onebot import OneBotClient

logger = logging.getLogger(__name__)

# Rate limiter state: target -> last_send_timestamp
_last_send: dict[str, float] = {}
RATE_LIMIT_SECONDS = 0.0
CST = timezone(timedelta(hours=8))

# ── @QQ号 → real at segment ──────────────────────────────
_AT_RE = re.compile(r"(?<![a-zA-Z0-9.])@(\d{5,11})(?!\d)")
# ── CQ码表情 → real face segment ────────────────────────
_CQ_FACE_RE = re.compile(r"\[CQ:face,id=(\d+)\]")

# Combined pattern: match either @QQ号 or [CQ:face,id=N]
_SEGMENT_RE = re.compile(
    r"(?P<at>(?<![a-zA-Z0-9.])@(?P<qq>\d{5,11})(?!\d))"
    r"|(?P<face>\[CQ:face,id=(?P<face_id>\d+)\])"
)


def _text_to_segments(text: str) -> list[dict]:
    """Convert @QQ号 and [CQ:face,id=N] in text to OneBot segments."""
    segments: list[dict] = []
    last_end = 0
    for m in _SEGMENT_RE.finditer(text):
        if m.start() > last_end:
            segments.append({"type": "text", "data": {"text": text[last_end:m.start()]}})
        if m.group("at"):
            segments.append({"type": "at", "data": {"qq": m.group("qq")}})
        elif m.group("face"):
            segments.append({"type": "face", "data": {"id": m.group("face_id")}})
        last_end = m.end()
    if last_end < len(text):
        segments.append({"type": "text", "data": {"text": text[last_end:]}})
    return segments or [{"type": "text", "data": {"text": text}}]


# ── </分段> tag-based message splitting ────────────────
_SPLIT_TAG_RE = re.compile(r"</\s*分段\s*>")


def _split_by_tag(text: str) -> list[str] | None:
    """Split text on </分段> tags.

    Returns the list of non-empty, stripped segments if the tag is present;
    returns None if no tag was found (so callers can fall through to other
    splitting strategies).
    """
    if not text or not _SPLIT_TAG_RE.search(text):
        return None
    parts = _SPLIT_TAG_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


# ── Duplicate send detection ────────────────────────────
_DEDUP_WINDOW_SECONDS = 60.0  # 1 minute
# key = "target_type:target_id" -> deque of (content_hash, send_time)
_sent_history: dict[str, deque[tuple[str, float]]] = {}


def _normalize_content(text: str) -> str:
    """Normalize text for dedup comparison: strip, collapse whitespace, NFKC."""
    text = unicodedata.normalize("NFKC", text.strip())
    text = re.sub(r"\s+", " ", text)
    return text.lower()


def _check_duplicate(target_key: str, content: str) -> str | None:
    """Check if content was sent to this target within the dedup window.

    Returns a warning message if duplicate, None otherwise.
    Automatically evicts expired entries.
    """
    now = time.time()
    h = hashlib.md5(_normalize_content(content).encode()).hexdigest()

    history = _sent_history.get(target_key)
    if history is None:
        history = deque(maxlen=50)
        _sent_history[target_key] = history

    # Evict expired entries
    while history and now - history[0][1] > _DEDUP_WINDOW_SECONDS:
        history.popleft()

    # Check for match
    for entry_hash, entry_time in history:
        if entry_hash == h:
            ago = int(now - entry_time)
            return (
                f"⚠️ 这条消息你在 {ago} 秒前已经发送过完全相同的内容，未重复发送。"
                f"如果确实需要重发，请稍作修改后重试。"
            )

    # Record this send
    history.append((h, now))
    return None

# Chunking config
CHUNK_MAX_CHARS = 30
# Delay: ms per character (scales with chunk length)
HUMAN_DELAY_MS_PER_CHAR = 80  # ~80ms per char ≈ real typing speed
HUMAN_DELAY_MIN_MS = 300
HUMAN_DELAY_MAX_MS = 3000

# Server start time for uptime tracking
_start_time: float = time.time()


def _human_delay_for_chunk(chunk: str) -> float:
    """Calculate a human-like delay (in seconds) based on chunk length."""
    base = len(chunk) * HUMAN_DELAY_MS_PER_CHAR
    # Add ±30% jitter
    jitter = random.uniform(0.7, 1.3)
    ms = max(HUMAN_DELAY_MIN_MS, min(int(base * jitter), HUMAN_DELAY_MAX_MS))
    return ms / 1000.0


def _chunk_message(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
    """Split a long message into natural chunks for sequential sending.

    1. Always split on \\n\\n (paragraph boundary).
    2. If a paragraph <= max_chars, keep it whole.
    3. If a paragraph > max_chars:
       a. Split by sentence-enders (.!?。！？~), group consecutive
          sentences so each chunk stays near max_chars (split roughly
          from the middle, not every sentence boundary).
       b. If a grouped chunk is still > max_chars (single long sentence),
          apply clause-level splitting (，,、：:；;——--) which removes
          the delimiter.
    """
    text = text.strip()
    if not text:
        return []

    # Protect URLs from being split on punctuation (.:?! etc.)
    _URL_PLACEHOLDER = "\x01"
    _url_re = re.compile(r'https?://\S+')
    _urls_found = _url_re.findall(text)
    for _i, _u in enumerate(_urls_found):
        text = text.replace(_u, f"{_URL_PLACEHOLDER}{_i}{_URL_PLACEHOLDER}", 1)

    # Protect file extensions from being split on the dot (case-insensitive)
    _PLACEHOLDER = "\x00"
    _ext_re = re.compile(r'\.(?:md|jpeg|jpg|png|py|js|ts|json|html|css|txt|csv|pdf|zip|gif|svg|mp3|mp4|wav)\b', re.IGNORECASE)
    text = _ext_re.sub(lambda m: _PLACEHOLDER + m.group(0)[1:], text)

    # Step 1: Split on \n\n unconditionally
    paragraphs = re.split(r'\n\n+', text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    # Level-1: sentence-enders (punctuation kept via lookbehind)
    # English period only splits when NOT preceded by a digit (avoids "1. item" or "v2.0")
    _sentence_re = re.compile(
        r'(?<=(?<!\d)[.])'
        r'|(?<=[!?。！？~\n])'
    )
    # Level-2: clause delimiters (consumed = removed)
    _clause_re = re.compile(
        r'[，,、：:；;]'
        r'|'
        r'(?:——|--)'
    )

    def _group_parts(parts: list[str], limit: int) -> list[str]:
        """Greedily group consecutive parts so each chunk <= limit."""
        groups: list[str] = []
        buf = ''
        for p in parts:
            candidate = (buf + p) if buf else p
            if len(candidate) <= limit:
                buf = candidate
            else:
                if buf:
                    groups.append(buf)
                buf = p
        if buf:
            groups.append(buf)
        return groups

    chunks: list[str] = []
    for para in paragraphs:
        if len(para) <= max_chars:
            chunks.append(para)
            continue

        # Level-1: split by sentence-enders, then group
        sentences = [s.strip() for s in _sentence_re.split(para) if s.strip()]
        grouped = _group_parts(sentences, max_chars)

        for chunk in grouped:
            if len(chunk) <= max_chars:
                chunks.append(chunk)
            else:
                # Level-2: clause-level split for overlong single sentence
                clauses = [c.strip() for c in _clause_re.split(chunk) if c.strip()]
                grouped2 = _group_parts(clauses, max_chars)
                chunks.extend(grouped2)

    # Restore protected file extensions and URLs
    result = [c.replace(_PLACEHOLDER, ".") for c in chunks if c]
    for _i, _u in enumerate(_urls_found):
        result = [c.replace(f"{_URL_PLACEHOLDER}{_i}{_URL_PLACEHOLDER}", _u) for c in result]
    return result


def _decide_chunks(
    content: str, split_content: bool, num_chunks: int | None,
) -> list[str]:
    """Decide how to split outgoing message content into chunks.

    Priority:
      1. num_chunks == 1        → single message (no split)
      2. num_chunks >= 2        → punctuation-split then merge into exactly N
      3. </分段> tag in content → split on tag, strip the tag, keep segments as-is
      4. split_content & ≤100   → punctuation split for short messages
      5. default                → single message
    """
    stripped = content.strip()

    if num_chunks is not None and num_chunks == 1:
        return [stripped] if stripped else []

    if num_chunks is not None and num_chunks >= 2 and stripped:
        fine_chunks = _chunk_message(content)
        if len(fine_chunks) <= num_chunks:
            return fine_chunks
        chunks: list[str] = []
        per_group = len(fine_chunks) / num_chunks
        for i in range(num_chunks):
            start = round(i * per_group)
            end = round((i + 1) * per_group)
            chunks.append("\n".join(fine_chunks[start:end]))
        return chunks

    tag_chunks = _split_by_tag(content)
    if tag_chunks is not None:
        return tag_chunks

    if split_content and len(stripped) <= 100:
        return _chunk_message(content)

    return [stripped] if stripped else []


def _write_file(path: str, data: bytes) -> None:
    """同步写文件（在 executor 中运行）。"""
    with open(path, "wb") as f:
        f.write(data)


def _make_relevant_fn(target_type: str, target: str, wake_monitor) -> Callable | None:
    """创建 wait_for_reply 的消息过滤函数：私聊全收，群聊按 wake 规则过滤。"""
    if wake_monitor is None:
        return None

    def relevant(msg: Message) -> bool:
        if target_type == "private":
            return True
        return wake_monitor.is_relevant(target_type, target, msg)

    return relevant


def register_tools(
    mcp: Any, config: Config, bot: OneBotClient, ctx: ContextManager,
    browser_holder: dict | None = None,
    wake_monitor: Any = None,
    timer_scheduler: Any = None,
) -> None:
    """Register all MCP tools on the FastMCP server instance."""

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
    async def check_status() -> dict:
        """Check QQ login status, NapCat connection, and monitored targets.

        Returns the bot's QQ account, online status, uptime, list of monitored
        groups and friends, and buffer statistics. Use this to verify the server
        is running correctly before calling other tools. Not the same as
        get_group_list or get_friend_list — this is a health check, not a query.

        Read-only. No side effects. Safe to call at any time.
        """
        try:
            login_info = await bot.get_login_info()
        except Exception as e:
            return {
                "napcat_running": False,
                "qq_logged_in": False,
                "error": str(e),
            }

        # Online status
        online_status = "unknown"
        try:
            status = await bot.get_status()
            if status.get("online", False):
                online_status = "online"
            else:
                online_status = "offline"
        except Exception:
            pass

        try:
            groups = await bot.get_group_list()
        except Exception:
            groups = []

        monitored_groups = []
        for g in groups:
            gid = str(g.get("group_id", ""))
            if config.is_group_monitored(gid):
                monitored_groups.append(
                    {
                        "group_id": gid,
                        "group_name": g.get("group_name", ""),
                        "member_count": g.get("member_count", 0),
                    }
                )

        # Resolve friend nicknames
        monitored_friends = []
        try:
            all_friends = await bot.get_friend_list()
            if config.friends is None:
                # Monitor all friends
                monitored_friends = [
                    {"user_id": str(f.get("user_id", "")),
                     "nickname": f.get("nickname", f.get("remark", ""))}
                    for f in all_friends
                ]
            else:
                friend_map = {str(f.get("user_id", "")): f for f in all_friends}
                for uid in config.friends:
                    f = friend_map.get(uid, {})
                    monitored_friends.append({
                        "user_id": uid,
                        "nickname": f.get("nickname", f.get("remark", "")),
                    })
        except Exception:
            if config.friends is not None:
                monitored_friends = [{"user_id": uid, "nickname": ""} for uid in config.friends]

        return {
            "napcat_running": True,
            "qq_logged_in": True,
            "qq_account": str(login_info.get("user_id", "")),
            "qq_nickname": login_info.get("nickname", ""),
            "online_status": online_status,
            "uptime_seconds": int(time.time() - _start_time),
            "monitored_groups": monitored_groups,
            "monitored_friends": monitored_friends,
            "total_groups": len(groups),
            "buffer_stats": ctx.buffer_stats,
        }

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
    async def get_group_list() -> dict:
        """List all QQ groups the bot has joined.

        Returns group IDs, names, and member counts for every group the bot is
        a member of. Use this to discover valid group IDs for send_message,
        get_recent_context, or other group-targeting tools. For friend list use
        get_friend_list instead. For batch name resolution use
        batch_get_recent_context.

        Read-only. No side effects.
        """
        groups = await bot.get_group_list()
        return {
            "groups": [
                {
                    "group_id": str(g.get("group_id", "")),
                    "group_name": g.get("group_name", ""),
                    "member_count": g.get("member_count", 0),
                }
                for g in groups
            ]
        }

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
    async def get_friend_list() -> dict:
        """List all QQ friends of the bot account.

        Returns user IDs and nicknames. Use this to discover valid friend IDs
        for send_message or get_recent_context with target_type="private". For
        group list use get_group_list instead.

        Read-only. No side effects.
        """
        friends = await bot.get_friend_list()
        return {
            "friends": [
                {
                    "user_id": str(f.get("user_id", "")),
                    "nickname": f.get("nickname", f.get("remark", "")),
                }
                for f in friends
            ]
        }

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
    async def get_recent_context(
        target: str,
        target_type: str = "group",
        limit: int = 200,
    ) -> dict:
        """Retrieve recent messages for one group or friend from the in-memory buffer.

        Returns buffered messages (backfill from history + real-time via WebSocket)
        as raw message objects with sender info, content, timestamps, and image
        URLs. This is the primary tool for reading chat context. For multiple
        targets at once, use batch_get_recent_context instead (fewer API calls).
        To archive old messages and free buffer space, use compress_context.

        Messages older than the buffer window are lost. Call compress_context
        periodically to preserve important conversations.

        Read-only. No side effects on the chat.
        """
        # Whitelist check
        if target_type == "group":
            if not config.is_group_monitored(target):
                return {"error": f"Group {target} is not monitored"}
        elif target_type == "private":
            if not config.is_friend_monitored(target):
                return {"error": f"User {target} is not in friends whitelist"}
        else:
            return {"error": f"Invalid target_type: {target_type}"}

        limit = max(1, limit)

        result = ctx.get_context(target, target_type, limit)

        # Add group_name / friend_name if possible
        if target_type == "group":
            try:
                info = await bot.get_group_info(target)
                result["group_name"] = info.get("group_name", "")
            except Exception:
                result["group_name"] = ""
        else:
            # Enrich friend_name from friend list
            friend_name = ""
            try:
                friends = await bot.get_friend_list()
                for f in friends:
                    if str(f.get("user_id", "")) == target:
                        friend_name = f.get("nickname", f.get("remark", ""))
                        break
            except Exception:
                pass
            result["friend_name"] = friend_name

        return result

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
    async def batch_get_recent_context(
        targets: list[dict],
        limit: int = 50,
    ) -> dict:
        """Query recent messages for multiple groups/friends in one call.

        More efficient than calling get_recent_context multiple times: uses at
        most 2 OneBot API calls (one for group names, one for friend names)
        regardless of how many targets you query. Each target's messages are
        returned independently with errors for unmonitored targets.

        Use this when you need to read context from 2+ conversations at once.
        For a single target, use get_recent_context instead (simpler).

        Read-only. No side effects.
        """
        limit = max(1, min(limit, 200))

        # Classify targets
        group_ids: list[str] = []
        friend_ids: list[str] = []
        for t in targets:
            tt = t.get("target_type", "group")
            tid = str(t.get("target", ""))
            if tt == "group":
                group_ids.append(tid)
            elif tt == "private":
                friend_ids.append(tid)

        # Batch fetch names — at most 2 API calls total
        group_name_map: dict[str, str] = {}
        if group_ids:
            try:
                all_groups = await bot.get_group_list()
                group_name_map = {
                    str(g.get("group_id", "")): g.get("group_name", "")
                    for g in all_groups
                }
            except Exception as e:
                logger.warning("batch: failed to get group list: %s", e)

        friend_name_map: dict[str, str] = {}
        if friend_ids:
            try:
                all_friends = await bot.get_friend_list()
                friend_name_map = {
                    str(f.get("user_id", "")): f.get("nickname", f.get("remark", ""))
                    for f in all_friends
                }
            except Exception as e:
                logger.warning("batch: failed to get friend list: %s", e)

        # Build results — pure memory reads
        results: list[dict] = []
        for t in targets:
            target = str(t.get("target", ""))
            target_type = t.get("target_type", "group")

            # Whitelist check
            if target_type == "group" and not config.is_group_monitored(target):
                results.append({"target": target, "target_type": target_type,
                                "error": f"Group {target} is not monitored"})
                continue
            if target_type == "private" and not config.is_friend_monitored(target):
                results.append({"target": target, "target_type": target_type,
                                "error": f"User {target} is not in friends whitelist"})
                continue
            if target_type not in ("group", "private"):
                results.append({"target": target, "target_type": target_type,
                                "error": f"Invalid target_type: {target_type}"})
                continue

            # Read from memory buffer
            result = ctx.get_context(target, target_type, limit)

            # Attach name from pre-fetched map (0 API calls)
            if target_type == "group":
                result["group_name"] = group_name_map.get(target, "")
            else:
                result["friend_name"] = friend_name_map.get(target, "")

            results.append(result)

        return {"results": results, "count": len(results)}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
    async def send_message(
        target: str,
        content: str,
        target_type: str = "group",
        reply_to: str | None = None,
        split_content: bool = False,
        num_chunks: int | None = None,
        wait_reply: bool = True,
    ) -> dict:
        """Send a text message to a QQ group or friend, optionally waiting for a reply.

        This is the primary tool for sending text messages. It supports message
        splitting via </分段> tags or punctuation-based chunking. The message is
        sent immediately and the bot's own message is written to the buffer.

        To split a reply into multiple messages, insert </分段> at split points.
        For example: "Hi</分段>How are you?" sends two separate messages.

        Behavior: rate-limited (60s dedup window), duplicate content within 60s
        is blocked. After sending, blocks until a reply arrives (unless
        wait_reply=False). The reply includes the full message objects.

        Use send_image for images, send_voice for audio, send_file for files.
        For a standalone wait without sending, use wait_for_reply.
        """
        # Whitelist check
        if target_type == "group":
            if not config.is_group_monitored(target):
                return {"success": False, "error": f"Group {target} is not monitored"}
        elif target_type == "private":
            if not config.is_friend_monitored(target):
                return {
                    "success": False,
                    "error": f"User {target} is not in friends whitelist",
                }
        else:
            return {"success": False, "error": f"Invalid target_type: {target_type}"}

        # Silence check — LLM should not call send_message for [沉默]
        if content.strip().startswith("[沉默]"):
            return {
                "success": False,
                "error": "你发送了沉默，沉默不该调用 MCP 接口。",
            }

        # Rate limit
        now = time.time()
        key = f"{target_type}:{target}"
        last = _last_send.get(key, 0)
        if now - last < RATE_LIMIT_SECONDS:
            wait = RATE_LIMIT_SECONDS - (now - last)
            return {
                "success": False,
                "error": f"Rate limited. Try again in {wait:.1f}s",
            }
        _last_send[key] = now

        # Duplicate detection (before chunking)
        dup_warning = _check_duplicate(key, content)
        if dup_warning:
            return {"success": False, "error": dup_warning}

        chunks = _decide_chunks(content, split_content, num_chunks)
        if not chunks:
            return {"success": False, "error": "Empty message content"}

        sent_ids: list[str] = []
        first_reply_to = reply_to  # Only first chunk is a reply
        t0 = time.time()  # record baseline for incremental message snapshot

        try:
            for i, chunk_text in enumerate(chunks):
                # Strip trailing periods for natural chat style
                chunk_text = chunk_text.rstrip("。.")
                if not chunk_text:
                    continue
                msg = _text_to_segments(chunk_text)
                rto = first_reply_to if i == 0 else None

                if target_type == "group":
                    result = await bot.send_group_msg(target, msg, reply_to=rto)
                else:
                    result = await bot.send_private_msg(target, msg, reply_to=rto)

                msg_id = str(result.get("message_id", ""))
                sent_ids.append(msg_id)

                # Write bot's own message directly into buffer (don't wait for WS echo)
                bot_msg = Message(
                    sender_id=config.qq,
                    sender_name="bot",
                    content=chunk_text,
                    timestamp=datetime.now(CST).isoformat(),
                    message_id=msg_id,
                    is_self=True,
                )
                ctx.add_message(target, target_type, bot_msg)


        except Exception as e:
            _last_send[key] = last  # rollback rate limit on failure
            if sent_ids:
                return {
                    "success": False,
                    "error": f"Partial send ({len(sent_ids)}/{len(chunks)} chunks): {e}",
                    "message_ids": sent_ids,
                }
            return {"success": False, "error": str(e)}

        # Mark reply sent to wake monitor (lock stays until user replies back)
        if wake_monitor:
            wake_monitor.mark_reply_sent(target_type, target)

        # Snapshot: all messages since this send_message started (incremental)
        recent_msgs = ctx.get_messages_since(target, target_type, t0)
        recent_lines: list[str] = []
        for m in recent_msgs:
            if m.is_self:
                tag = "[bot(self)]"
            else:
                tag = f"[{m.sender_name}]"
            recent_lines.append(f"{tag} {m.content}")

        result = {
            "success": True,
            "message_ids": sent_ids,
            "chunks": len(chunks),
            "target": target,
            "target_type": target_type,
            "timestamp": datetime.now(CST).isoformat(),
            "recent_messages": recent_lines,
        }

        # Auto-wait for reply unless explicitly disabled
        if wait_reply:
            if wake_monitor:
                wake_monitor.set_waiting_for_reply(True)
            try:
                wait_since = time.time()
                _relevant = _make_relevant_fn(target_type, target, wake_monitor)
                reply_msgs, timed_out = await ctx.wait_for_new_message(
                    target, target_type, wait_since, timeout=None,
                    relevant_fn=_relevant,
                )
                result["reply"] = {
                    "messages": [m.to_dict() for m in reply_msgs if not m.is_self],
                    "timed_out": timed_out,
                    "waited_seconds": round(time.time() - wait_since, 1),
                }
            finally:
                if wake_monitor:
                    wake_monitor.set_waiting_for_reply(False)

        return result

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def wait_for_reply(
        target: str,
        target_type: str = "group",
        timeout: float = 120.0,
    ) -> dict:
        """Block until a new message arrives from a specific group or friend.

        Use this as a standalone follow-up after send_message (with
        wait_reply=False) or when you need to wait for a reply without sending
        first. Returns only messages from others (not the bot's own). Times out
        after `timeout` seconds (max 300).

        If you want to send a message AND wait for a reply in one call, use
        send_message with wait_reply=True instead — it's simpler.

        Read-only. No side effects on the chat.
        """
        if target_type == "group":
            if not config.is_group_monitored(target):
                return {"error": f"Group {target} is not monitored"}
        elif target_type == "private":
            if not config.is_friend_monitored(target):
                return {"error": f"User {target} is not in friends whitelist"}
        else:
            return {"error": f"Invalid target_type: {target_type}"}

        timeout = max(1.0, min(timeout, 300.0))
        since = time.time()
        _relevant = _make_relevant_fn(target_type, target, wake_monitor)
        if wake_monitor:
            wake_monitor.set_waiting_for_reply(True)
        try:
            messages, timed_out = await ctx.wait_for_new_message(
                target, target_type, since, timeout, relevant_fn=_relevant,
            )
        finally:
            if wake_monitor:
                wake_monitor.set_waiting_for_reply(False)

        result = {
            "target": target,
            "target_type": target_type,
            "new_messages": [m.to_dict() for m in messages if not m.is_self],
            "timed_out": timed_out,
            "waited_seconds": round(time.time() - since, 1),
        }

        if target_type == "group":
            try:
                info = await bot.get_group_info(target)
                result["group_name"] = info.get("group_name", "")
            except Exception:
                result["group_name"] = ""
        else:
            friend_name = ""
            try:
                friends = await bot.get_friend_list()
                for f in friends:
                    if str(f.get("user_id", "")) == target:
                        friend_name = f.get("nickname", f.get("remark", ""))
                        break
            except Exception:
                pass
            result["friend_name"] = friend_name

        return result

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
    async def send_image(
        target: str,
        image: str,
        target_type: str = "group",
        reply_to: str | None = None,
    ) -> dict:
        """Send a base64-encoded image to a QQ group or friend.

        For text messages use send_message. For audio use send_voice. For files
        use send_file. This tool only sends images.

        Behavior: same rate limiting and dedup as send_message. The image is
        sent as a QQ-native image (not a file attachment).

        Mutates the chat by sending an image.
        """
        # Whitelist check
        if target_type == "group":
            if not config.is_group_monitored(target):
                return {"success": False, "error": f"Group {target} is not monitored"}
        elif target_type == "private":
            if not config.is_friend_monitored(target):
                return {
                    "success": False,
                    "error": f"User {target} is not in friends whitelist",
                }
        else:
            return {"success": False, "error": f"Invalid target_type: {target_type}"}

        # Rate limit
        now = time.time()
        key = f"{target_type}:{target}"
        last = _last_send.get(key, 0)
        if now - last < RATE_LIMIT_SECONDS:
            wait = RATE_LIMIT_SECONDS - (now - last)
            return {
                "success": False,
                "error": f"Rate limited. Try again in {wait:.1f}s",
            }
        _last_send[key] = now

        msg = [{"type": "image", "data": {"file": f"base64://{image}"}}]

        try:
            if target_type == "group":
                result = await bot.send_group_msg(target, msg, reply_to=reply_to)
            else:
                result = await bot.send_private_msg(target, msg, reply_to=reply_to)
        except Exception as e:
            _last_send[key] = last  # rollback rate limit on failure
            return {"success": False, "error": str(e)}

        msg_id = str(result.get("message_id", ""))

        # Write bot's own message into buffer
        bot_msg = Message(
            sender_id=config.qq,
            sender_name="bot",
            content="[图片]",
            timestamp=datetime.now(CST).isoformat(),
            message_id=msg_id,
            is_self=True,
        )
        ctx.add_message(target, target_type, bot_msg)

        return {
            "success": True,
            "message_id": msg_id,
            "target": target,
            "target_type": target_type,
            "timestamp": datetime.now(CST).isoformat(),
        }

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
    async def send_voice(
        target: str,
        audio: str,
        target_type: str = "group",
    ) -> dict:
        """Send a base64-encoded voice message to a QQ group or friend.

        NapCat auto-converts common audio formats (MP3, WAV, AMR, OGG, FLAC)
        to SILK for QQ voice playback. For text use send_message, for images
        use send_image, for files use send_file.

        Behavior: same rate limiting as send_message. Voice messages cannot be
        replied to (no reply_to parameter).

        Mutates the chat by sending a voice message.
        """
        # Whitelist check
        if target_type == "group":
            if not config.is_group_monitored(target):
                return {"success": False, "error": f"Group {target} is not monitored"}
        elif target_type == "private":
            if not config.is_friend_monitored(target):
                return {
                    "success": False,
                    "error": f"User {target} is not in friends whitelist",
                }
        else:
            return {"success": False, "error": f"Invalid target_type: {target_type}"}

        msg = [{"type": "record", "data": {"file": f"base64://{audio}"}}]

        try:
            if target_type == "group":
                result = await bot.send_group_msg(target, msg)
            else:
                result = await bot.send_private_msg(target, msg)
        except Exception as e:
            return {"success": False, "error": str(e)}

        msg_id = str(result.get("message_id", ""))

        # Write bot's own message into buffer
        bot_msg = Message(
            sender_id=config.qq,
            sender_name="bot",
            content="[语音]",
            timestamp=datetime.now(CST).isoformat(),
            message_id=msg_id,
            is_self=True,
        )
        ctx.add_message(target, target_type, bot_msg)

        return {
            "success": True,
            "message_id": msg_id,
            "target": target,
            "target_type": target_type,
            "timestamp": datetime.now(CST).isoformat(),
        }

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
    async def send_file(
        target: str,
        file_url: str,
        file_name: str | None = None,
        target_type: str = "group",
    ) -> dict:
        """Download a file from a URL and send it to a QQ group or friend.

        Downloads the file to a temporary directory, uploads it to QQ, then
        cleans up. Supports any file type. For text use send_message, for
        images use send_image, for audio use send_voice.

        Behavior: requires a publicly accessible URL (no auth). Download
        timeout is 60 seconds. The file is sent as a QQ file attachment.

        Mutates the chat by sending a file.
        """
        import os
        import tempfile

        if target_type == "group":
            if not config.is_group_monitored(target):
                return {"success": False, "error": f"Group {target} is not monitored"}
        elif target_type == "private":
            if not config.is_friend_monitored(target):
                return {"success": False, "error": f"User {target} is not in friends whitelist"}
        else:
            return {"success": False, "error": f"Invalid target_type: {target_type}"}

        name = file_name or os.path.basename(file_url.split("?")[0]) or "file"
        tmp = tempfile.mkdtemp()
        local_path = os.path.join(tmp, name)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        return {"success": False, "error": f"Download failed: HTTP {resp.status}"}
                    data = await resp.read()
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _write_file, local_path, data)

            if target_type == "group":
                await bot.upload_group_file(target, local_path, name)
            else:
                await bot.upload_private_file(target, local_path, name)
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            try:
                os.remove(local_path)
                os.rmdir(tmp)
            except Exception:
                pass

        return {
            "success": True,
            "file_name": name,
            "target": target,
            "target_type": target_type,
        }

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False})
    async def compress_context(
        target: str,
        ctx_mcp: Context,
        target_type: str = "group",
    ) -> dict:
        """Compress all buffered messages for a target into a single summary, freeing buffer space.

        This is destructive: raw messages are replaced by a compressed summary.
        Once compressed, individual messages cannot be recovered from the buffer.
        Use this after get_recent_context when you want to archive old
        conversations and make room for new messages.

        The compression uses the client LLM (via MCP sampling) to generate a
        concise summary. Falls back to rule-based compression if LLM is
        unavailable.

        Destructive: permanently replaces raw messages with a summary.
        """
        # Whitelist check
        if target_type == "group":
            if not config.is_group_monitored(target):
                return {"error": f"Group {target} is not monitored"}
        elif target_type == "private":
            if not config.is_friend_monitored(target):
                return {"error": f"User {target} is not in friends whitelist"}
        else:
            return {"error": f"Invalid target_type: {target_type}"}

        buf_key = ctx._buffer_key(target_type, target)
        buf = ctx._buffers.get(buf_key)
        if buf is None or len(buf.messages) == 0:
            return {
                "success": True,
                "compressed": 0,
                "message": "No messages to compress",
                "compressed_summary": buf.compressed_summary if buf else None,
            }

        # Extract all messages
        all_msgs = list(buf.messages)
        buf.messages.clear()
        buf._compress_pending = False
        buf._compress_all_pending = False
        buf._msg_since_compress = 0

        # Try LLM compression, fall back to rule-based
        try:
            summary = await _llm_compress(ctx_mcp, all_msgs)
            method = "llm"
        except Exception as e:
            logger.warning("LLM compression failed, using rule-based: %s", e)
            summary = _rule_based_compress(all_msgs)
            method = "rule-based"

        buf.apply_summary(summary)
        logger.info("%s compressed %d messages for %s", method, len(all_msgs), buf_key)

        return {
            "success": True,
            "compressed": len(all_msgs),
            "method": method,
            "compressed_summary": buf.compressed_summary,
        }

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
    async def screenshot_chat(
        target: str,
        message_id: str,
        target_type: str = "group",
    ) -> dict:
        """Render a QQ-style dark-mode chat screenshot starting from a given message.

        Uses Playwright to render messages as an iPhone-style QQ chat screenshot
        and returns a base64-encoded PNG image. The screenshot starts from the
        given message_id and renders downward. If messages fit on one screen,
        earlier messages are prepended to fill it (bottom-aligned). If they
        overflow, later messages are cut off.

        Requires: Playwright browser (lazy-started on first call). If the
        message_id is not found in the buffer, returns an error.

        Use get_recent_context first to find valid message_ids. The screenshot
        is read-only and does not modify the chat.

        Read-only. No side effects on the chat.
        """
        from .renderer import render_to_base64, measure_chat_height

        if target_type == "group":
            if not config.is_group_monitored(target):
                return {"success": False, "error": f"Group {target} is not monitored"}
        elif target_type == "private":
            if not config.is_friend_monitored(target):
                return {"success": False, "error": f"User {target} is not in friends whitelist"}
        else:
            return {"success": False, "error": f"Invalid target_type: {target_type}"}

        # Get browser (lazy-start Playwright)
        if browser_holder is None:
            return {"success": False, "error": "Screenshot not available (no browser)"}

        if browser_holder["browser"] is None:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            browser_holder["pw"] = pw
            browser_holder["browser"] = await pw.chromium.launch()
            logger.info("Playwright browser started")

        browser = browser_holder["browser"]

        # Find message_id in buffer and split into before/after
        buf_key = ctx._buffer_key(target_type, target)
        buf = ctx._buffers.get(buf_key)
        if buf is None:
            return {"success": False, "error": f"No messages buffered for {target}"}

        all_msgs = list(buf.messages)
        # Find the index of message_id
        start_idx = None
        for i, m in enumerate(all_msgs):
            if m.message_id == message_id:
                start_idx = i
                break

        if start_idx is None:
            return {"success": False, "error": f"Message {message_id} not found in buffer"}

        after_msgs = all_msgs[start_idx:]  # message_id and everything after
        before_msgs = all_msgs[:start_idx]  # everything before message_id

        # Get group info
        group_name = target
        member_count = 0
        if target_type == "group":
            try:
                info = await bot.get_group_info(target)
                group_name = info.get("group_name", target)
                member_count = info.get("member_count", 0)
            except Exception:
                pass

        # Get member info (title/level/role) for all senders
        sender_ids = set(m.sender_id for m in all_msgs)
        member_info: dict[str, dict] = {}
        for uid in sender_ids:
            try:
                mi = await bot._call("get_group_member_info",
                                     group_id=int(target), user_id=int(uid))
                if mi:
                    member_info[uid] = mi
            except Exception:
                pass

        def _msg_to_dict(m: Message) -> dict:
            mi = member_info.get(m.sender_id, {})
            return {
                "sender_id": m.sender_id,
                "sender_name": m.sender_name,
                "content": m.content,
                "timestamp": m.timestamp,
                "message_id": m.message_id,
                "is_at_me": m.is_at_me,
                "is_self": m.is_self,
                "image_urls": m.image_urls,
                "_title": mi.get("title", ""),
                "_role": mi.get("role", "member"),
                "_level": mi.get("level", ""),
            }

        after_dicts = [_msg_to_dict(m) for m in after_msgs]

        # Measure if after_msgs fit on screen
        chat_scroll, chat_client = await measure_chat_height(
            browser, after_dicts, group_name, member_count,
        )

        if chat_scroll <= chat_client:
            # Fits — prepend earlier messages to fill the screen
            combined = [_msg_to_dict(m) for m in before_msgs] + after_dicts
            # Keep adding earlier msgs until it overflows, then use bottom_align
            b64 = await render_to_base64(
                browser, combined, group_name, member_count,
                bottom_align=True,
            )
        else:
            # Overflow — render from message_id, top-aligned
            b64 = await render_to_base64(
                browser, after_dicts, group_name, member_count,
                bottom_align=False,
            )

        return {
            "success": True,
            "image": b64,
            "message_count": len(after_dicts),
            "target": target,
            "target_type": target_type,
            "start_message_id": message_id,
        }

    if wake_monitor is not None:

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def test_wake_activation() -> dict:
            """Manually trigger the wake activation sequence to verify it works.

            Sends a test keystroke to the opencode window. Use this to verify
            the wake system is functional. On Linux, this always returns False
            (wake is Windows-only). Does not affect any QQ messages.

            Read-only with respect to QQ. May type text into opencode window.
            """
            from .wake import _type_via_keyboard
            try:
                text = "[MCP] test wake"
                patterns = wake_monitor.config.window_title_patterns
                shortcut = wake_monitor.config.focus_shortcut
                ok = _type_via_keyboard(text, patterns, shortcut)
                return {"success": ok, "message": "Wake executed" if ok else "Wake FAILED"}
            except Exception as e:
                return {"success": False, "error": str(e)}

        @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def debug_wake_pipeline(target_type: str = "private", target_id: str = "3838379219") -> dict:
            """Simulate a wake trigger to debug the wake pipeline.

            Creates a fake message and checks: callback is set, rules match,
            pending state, and running state. Invokes the callback to test the
            full pipeline. Use diagnose_wake for a read-only state check.

            Read-only with respect to QQ. May invoke the wake callback.
            """
            try:
                from .context import Message
                msg = Message(
                    sender_id=target_id,
                    sender_name="DEBUG",
                    content="测试消息",
                    timestamp="2026-05-31T12:00:00+08:00",
                    message_id="debug_test_1",
                    is_self=False,
                )
                # Check callback
                cb = ctx._on_message
                result = {
                    "callback_set": cb is not None,
                    "callback_name": cb.__name__ if cb else None,
                    "matches_rules": wake_monitor._matches_rule(target_type, target_id, msg) is not None,
                    "pending": wake_monitor._pending,
                    "running": wake_monitor._running,
                }
                if cb:
                    try:
                        cb(target_type, target_id, msg)
                        result["callback_invoked"] = True
                    except Exception as e:
                        result["callback_invoked"] = False
                        result["callback_error"] = str(e)
                return result
            except Exception as e:
                return {"error": str(e)}

        @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def diagnose_wake() -> dict:
            """Return the current state of the wake monitor for debugging.

            Shows: running status, active rules, whether the message callback
            is registered, and total buffered messages. For testing the pipeline
            with a fake message, use debug_wake_pipeline instead.

            Read-only. No side effects.
            """
            return {
                "monitor_created": True,
                "running": wake_monitor._running,
                "rules": wake_monitor.list_rules(),
                "callback_set": ctx._on_message is not None,
                "callback_name": ctx._on_message.__name__ if ctx._on_message else None,
                "total_buffered": ctx.buffer_stats["total_messages_buffered"],
            }

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def add_wake_rule(
            target_type: str,
            target_id: str | None = None,
            keywords: list[str] | None = None,
            ignore_if_focused: bool = True,
        ) -> dict:
        """Add a wake rule: when a matching message arrives, opencode wakes up.

        When a message matches this rule, the agent is activated and the message
        context is made available. Use add_wake_rule to create rules,
        list_wake_rules to see them, remove_wake_rule to delete, and
        set_wake_enabled to toggle.

        Rules are persisted to disk and survive restarts. Keywords are optional:
        empty list matches any message. target_id=None matches any source.

        Mutates the wake rule configuration.
        """
            idx = wake_monitor.add_rule(target_type, target_id, keywords, ignore_if_focused)
            return {
                "success": True,
                "index": idx,
                "rule": {
                    "target_type": target_type,
                    "target_id": target_id,
                    "keywords": keywords or [],
                    "enabled": True,
                    "ignore_if_focused": ignore_if_focused,
                },
            }

        @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def list_wake_rules() -> dict:
        """List all configured wake rules with their index, target, and keywords.

        Use add_wake_rule to create, remove_wake_rule to delete, and
        set_wake_enabled to toggle individual rules.

        Read-only. No side effects.
        """
            return {"rules": wake_monitor.list_rules()}

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
        async def remove_wake_rule(index: int) -> dict:
        """Remove a wake rule by its index (from list_wake_rules).

        This is destructive: the rule is permanently deleted. Use
        set_wake_enabled to temporarily disable instead.

        Destructive: permanently removes the rule.
        """
            ok = wake_monitor.remove_rule(index)
            return {"success": ok}

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def set_wake_enabled(
            enabled: bool,
            index: int | None = None,
        ) -> dict:
        """Enable or disable wake rules. Pass index for one rule, or omit for all.

        Disabling a rule pauses wake triggers without deleting it. Use
        remove_wake_rule to permanently delete, add_wake_rule to create new.

        Mutates rule enabled state.
        """
            if index is not None:
                ok = wake_monitor.set_enabled(index, enabled)
            else:
                wake_monitor.set_enabled_all(enabled)
                ok = True
            return {"success": ok}

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def set_wake_pending(pending: bool) -> dict:
        """Manually lock or unlock wake triggers.

        When pending=True, incoming messages matching rules will NOT trigger
        wake. When pending=False, normal wake behavior resumes. Use this to
        prevent duplicate wakes while the agent is already working. Auto-expires
        after 5 minutes.

        Mutates the pending lock state.
        """
            wake_monitor.set_pending(pending)
            return {"success": True, "pending": pending}

        @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def get_wake_config() -> dict:
        """Return the current wake configuration: window title patterns and focus shortcut.

        Use set_wake_config to modify. Read-only.
        """
            return wake_monitor.get_config()

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def set_wake_config(
            window_title_patterns: list[str] | None = None,
            focus_shortcut: str | None = None,
        ) -> dict:
        """Configure which window to activate and how to focus its input box.

        window_title_patterns: substrings to match against window titles
        (e.g. ["opencode", "cursor"]). focus_shortcut: keyboard shortcut to
        focus the input (e.g. "ctrl+l"). Use get_wake_config to read current
        values.

        Mutates the wake configuration.
        """
            wake_monitor.set_config(window_title_patterns, focus_shortcut)
            return {"success": True, "config": wake_monitor.get_config()}

        # ── 群管理 ──────────────────────────────────────────

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
        async def mute_member(
            group_id: str,
            user_id: str,
            duration: int = 600,
        ) -> dict:
        """Mute a member in a QQ group for a specified duration.

        Mutes the user for `duration` seconds (default 600 = 10 minutes).
        Use unmute_member to reverse. Use kick_member to remove from group.
        Requires bot to have admin privileges in the group.

        Destructive: restricts a user's ability to speak.
        """
            try:
                await bot.set_group_ban(group_id, user_id, duration)
                return {"success": True, "group_id": group_id, "user_id": user_id, "duration": duration}
            except Exception as e:
                return {"success": False, "error": str(e)}

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def unmute_member(group_id: str, user_id: str) -> dict:
        """Remove a mute from a member in a QQ group.

        Reverses a mute applied by mute_member. Safe to call on non-muted
        members (no-op). Requires bot to have admin privileges.

        Mutates: restores the user's ability to speak.
        """
            try:
                await bot.set_group_ban(group_id, user_id, 0)
                return {"success": True, "group_id": group_id, "user_id": user_id}
            except Exception as e:
                return {"success": False, "error": str(e)}

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
        async def kick_member(
            group_id: str, user_id: str, reject_add_request: bool = False,
        ) -> dict:
        """Kick a member from a QQ group, optionally blocking re-entry.

        Removes the user from the group. If reject_add_request=True, future
        join requests from this user are automatically rejected. Use
        mute_member for temporary restrictions. Requires bot to have admin
        privileges.

        Destructive: permanently removes the user from the group.
        """
            try:
                await bot.set_group_kick(group_id, user_id, reject_add_request)
                action = "并拉黑" if reject_add_request else ""
                return {"success": True, "group_id": group_id, "user_id": user_id, "action": f"已踢出{action}"}
            except Exception as e:
                return {"success": False, "error": str(e)}

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def set_member_card(group_id: str, user_id: str, card: str = "") -> dict:
        """Set or clear a member's group nickname (card).

        Pass an empty string to clear the card. This is the display name shown
        in the group, separate from the user's QQ nickname. Requires bot to
        have admin privileges.

        Mutates: changes the user's display name in the group.
        """
            try:
                await bot.set_group_card(group_id, user_id, card)
                return {"success": True, "group_id": group_id, "user_id": user_id, "card": card or "(已清除)"}
            except Exception as e:
                return {"success": False, "error": str(e)}

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def send_group_notice(group_id: str, content: str) -> dict:
        """Post a group announcement (notice) to a QQ group.

        The announcement is visible to all group members. Requires bot to have
        admin privileges. For sending regular messages use send_message instead.

        Mutates: creates a group announcement.
        """
            try:
                await bot.send_group_notice(group_id, content)
                return {"success": True, "group_id": group_id}
            except Exception as e:
                return {"success": False, "error": str(e)}

        # ── 消息撤回 ──────────────────────────────────────

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
        async def recall_message(message_id: str) -> dict:
        """Recall (delete) a message sent by the bot.

        Only works for messages the bot itself sent, or if the bot is a group
        admin. Cannot recall messages from other users. Use this to correct
        mistakes.

        Destructive: permanently removes the message from the chat.
        """
            try:
                await bot.delete_msg(message_id)
                return {"success": True, "message_id": message_id}
            except Exception as e:
                return {"success": False, "error": str(e)}

        # ── 群成员查询 ────────────────────────────────────

        @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def get_group_member_list(group_id: str) -> dict:
        """List all members of a QQ group with their IDs, nicknames, and roles.

        Returns user_id, nickname, card (group display name), and role
        (owner/admin/member). Use get_group_member_info for detailed info on
        a single member. For group list use get_group_list.

        Read-only. No side effects.
        """
            try:
                members = await bot.get_group_member_list(group_id)
                summary = [
                    {
                        "user_id": str(m.get("user_id", "")),
                        "nickname": m.get("nickname", ""),
                        "card": m.get("card", ""),
                        "role": m.get("role", ""),
                    }
                    for m in members
                ]
                return {"success": True, "group_id": group_id, "member_count": len(summary), "members": summary}
            except Exception as e:
                return {"success": False, "error": str(e)}

        @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def get_group_member_info(group_id: str, user_id: str) -> dict:
        """Get detailed info for one member of a QQ group.

        Returns nickname, card, role, join_time, last_sent_time, level, and
        title. Use get_group_member_list to see all members at once.

        Read-only. No side effects.
        """
            try:
                info = await bot.get_group_member_info(group_id, user_id)
                return {
                    "success": True,
                    "group_id": group_id,
                    "user_id": str(info.get("user_id", "")),
                    "nickname": info.get("nickname", ""),
                    "card": info.get("card", ""),
                    "role": info.get("role", ""),
                    "join_time": info.get("join_time", 0),
                    "last_sent_time": info.get("last_sent_time", 0),
                    "level": info.get("level", ""),
                    "title": info.get("title", ""),
                }
            except Exception as e:
                return {"success": False, "error": str(e)}

    if timer_scheduler is not None:

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def add_timer(
            message: str,
            cron_expr: str | None = None,
            interval_seconds: int | None = None,
            once: bool = False,
        ) -> dict:
        """Schedule a recurring or one-shot task that wakes the agent with a message.

        Supports two scheduling modes:
        - cron_expr: cron expression like "0 8 * * *" (daily at 8am)
        - interval_seconds: fixed interval like 3600 (every hour)

        Exactly one of cron_expr or interval_seconds must be provided. When the
        timer fires, it triggers the wake system with the given message text.
        Use list_timers to see scheduled tasks, remove_timer to delete.

        Mutates: adds a scheduled task.
        """
            if not cron_expr and not interval_seconds:
                return {"success": False, "error": "需要 cron_expr 或 interval_seconds"}
            tid = timer_scheduler.add(cron_expr, interval_seconds, message, once)
            return {"success": True, "task_id": tid}

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
        async def remove_timer(index: int) -> dict:
        """Delete a scheduled timer task by index (from list_timers).

        Destructive: permanently removes the task.
        """
            ok = timer_scheduler.remove(index)
            return {"success": ok}

        @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
        async def list_timers() -> dict:
        """List all scheduled timer tasks with their index, schedule, and message.

        Read-only. No side effects.
        """
            return {"timers": timer_scheduler.list_tasks()}


async def _llm_compress(ctx_mcp: Context, messages: list) -> str:
    """Use the client's LLM (via MCP sampling) to compress messages into a summary."""
    # Format messages for the LLM
    lines = []
    for m in messages:
        lines.append(f"[{m.timestamp}] {m.sender_name}: {m.content}")
    chat_log = "\n".join(lines)

    result = await ctx_mcp.session.create_message(
        messages=[
            SamplingMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=(
                        "请将以下聊天记录压缩为一段简洁的中文摘要，保留关键信息（话题、观点、重要发言者）。"
                        "摘要应在 300 字以内，不要使用列表格式，用自然段落描述。\n\n"
                        f"聊天记录：\n{chat_log}"
                    ),
                ),
            )
        ],
        max_tokens=8192,
        system_prompt="你是一个聊天记录摘要助手。只输出摘要内容，不要添加任何前缀或解释。",
    )

    # Extract text from result
    if hasattr(result, "content"):
        content = result.content
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, TextContent):
            return content.text.strip()
        if isinstance(content, list):
            parts = []
            for c in content:
                if hasattr(c, "text"):
                    parts.append(c.text)
            return " ".join(parts).strip()
    return str(result).strip()


def _rule_based_compress(messages: list) -> str:
    """Fallback: rule-based compression when LLM is unavailable."""
    lines = []
    for m in messages:
        content = m.content[:80] + "..." if len(m.content) > 80 else m.content
        lines.append(f"{m.sender_name}: {content}")
    summary_block = " | ".join(lines)
    ts_range = f"[{messages[0].timestamp} ~ {messages[-1].timestamp}]"
    return f"{ts_range} {summary_block}"
