"""OneBot v11 HTTP API async client."""

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class OneBotError(Exception):
    """Raised when OneBot API returns a non-zero retcode."""

    def __init__(self, action: str, retcode: int, message: str):
        self.action = action
        self.retcode = retcode
        super().__init__(f"OneBot {action} failed (retcode={retcode}): {message}")


class OneBotClient:
    """Async client for NapCat's OneBot v11 HTTP API."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _call(self, action: str, **params: Any) -> Any:
        """Call an OneBot API action and return the data field."""
        session = await self._ensure_session()

        url = f"{self.base_url}/{action}"
        payload = {k: v for k, v in params.items() if v is not None}

        logger.debug("OneBot call: %s %s", action, payload)

        async with session.post(url, json=payload) as resp:
            result = await resp.json()

        retcode = result.get("retcode", -1)
        if retcode != 0:
            raise OneBotError(
                action, retcode, result.get("message", result.get("wording", ""))
            )

        return result.get("data")

    # ── Query APIs ──────────────────────────────────────────

    async def get_login_info(self) -> dict:
        """Get bot account info. Returns {user_id, nickname}."""
        return await self._call("get_login_info")

    async def get_group_list(self) -> list[dict]:
        """Get joined group list."""
        return await self._call("get_group_list")

    async def get_group_info(self, group_id: str) -> dict:
        """Get info for a specific group."""
        return await self._call("get_group_info", group_id=int(group_id))

    async def get_friend_list(self) -> list[dict]:
        """Get friend list."""
        return await self._call("get_friend_list")

    async def get_status(self) -> dict:
        """Get bot online status."""
        return await self._call("get_status")

    # ── Send APIs ───────────────────────────────────────────

    async def send_group_msg(
        self, group_id: str, message: list[dict], reply_to: str | None = None
    ) -> dict:
        """Send a group message. Returns {message_id}."""
        segments = []
        if reply_to:
            segments.append({"type": "reply", "data": {"id": reply_to}})
        segments.extend(message)
        return await self._call(
            "send_group_msg", group_id=int(group_id), message=segments
        )

    async def send_private_msg(
        self, user_id: str, message: list[dict], reply_to: str | None = None
    ) -> dict:
        """Send a private message. Returns {message_id}."""
        segments = []
        if reply_to:
            segments.append({"type": "reply", "data": {"id": reply_to}})
        segments.extend(message)
        return await self._call(
            "send_private_msg", user_id=int(user_id), message=segments
        )

    async def get_group_msg_history(
        self, group_id: str, count: int = 20
    ) -> list[dict]:
        """Fetch recent group message history. Returns list of message events."""
        data = await self._call(
            "get_group_msg_history", group_id=int(group_id), count=count
        )
        return data.get("messages", []) if data else []

    async def get_friend_msg_history(
        self, user_id: str, count: int = 20
    ) -> list[dict]:
        """Fetch recent private message history (NapCat extension API).

        Returns list of message events, same format as get_group_msg_history.
        """
        data = await self._call(
            "get_friend_msg_history", user_id=int(user_id), count=count
        )
        return data.get("messages", []) if data else []

    async def get_msg(self, message_id: str) -> dict:
        """Fetch a single message by its ID.

        Returns the full message event dict (sender, message, time, etc.).
        """
        return await self._call("get_msg", message_id=int(message_id))

    async def get_forward_msg(self, id: str) -> list[dict]:
        """Fetch forwarded message content by forward ID.

        Returns a list of message nodes, each with sender info and content.
        """
        data = await self._call("get_forward_msg", id=id)
        return data.get("messages", data.get("message", [])) if data else []

    # ── Moderation ───────────────────────────────────────────

    async def set_group_ban(
        self, group_id: str, user_id: str, duration: int = 1800,
    ) -> dict:
        """禁言成员 (duration=0 解禁)."""
        return await self._call(
            "set_group_ban",
            group_id=int(group_id), user_id=int(user_id), duration=duration,
        )

    async def set_group_kick(
        self, group_id: str, user_id: str, reject_add_request: bool = False,
    ) -> dict:
        """踢出群成员."""
        return await self._call(
            "set_group_kick",
            group_id=int(group_id), user_id=int(user_id),
            reject_add_request=reject_add_request,
        )

    async def set_group_card(
        self, group_id: str, user_id: str, card: str = "",
    ) -> dict:
        """设置群名片 (空字符串清除)."""
        return await self._call(
            "set_group_card",
            group_id=int(group_id), user_id=int(user_id), card=card,
        )

    async def send_group_notice(
        self, group_id: str, content: str,
    ) -> dict:
        """发送群公告 (NapCat 扩展 API)."""
        return await self._call(
            "_send_group_notice", group_id=int(group_id), content=content,
        )

    async def delete_msg(self, message_id: str) -> dict:
        """撤回消息."""
        return await self._call("delete_msg", message_id=int(message_id))

    async def get_group_member_list(self, group_id: str) -> list[dict]:
        """获取群成员列表."""
        return await self._call("get_group_member_list", group_id=int(group_id))

    async def get_group_member_info(
        self, group_id: str, user_id: str,
    ) -> dict:
        """获取指定群成员信息."""
        return await self._call(
            "get_group_member_info",
            group_id=int(group_id), user_id=int(user_id),
        )
