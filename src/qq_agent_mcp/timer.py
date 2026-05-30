"""定时任务调度器。每到特定时间或每隔特定时间通过 wake 唤醒 agent。"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

TIMERS_FILE = os.path.join(os.path.dirname(__file__), "timers.json")
CST = timezone(timedelta(hours=8))


@dataclass
class TimerTask:
    id: str
    cron_expr: str | None  # "0 8 * * *" = 每天早上8点
    interval_seconds: int | None  # 3600 = 每小时
    message: str  # 触发时唤醒 agent 的内容
    enabled: bool = True
    once: bool = False  # 单次触发，触发后自动删除
    last_fired: float | None = None  # 上次触发时间戳


def _parse_cron_minute_hour(cron: str) -> tuple[int, int] | None:
    """解析简易 cron 表达式 "minute hour * * *" → (minute, hour)。"""
    parts = cron.strip().split()
    if len(parts) != 5:
        return None
    try:
        minute = int(parts[0])
        hour = int(parts[1])
        return minute, hour
    except ValueError:
        return None


class TimerScheduler:
    """后台定时任务调度器。"""

    def __init__(self, wake_monitor=None):
        self._tasks: list[TimerTask] = []
        self._task: asyncio.Task | None = None
        self._running = False
        self._wake_monitor = wake_monitor
        self._next_id = 0
        self._load()

    def set_wake_monitor(self, wake_monitor) -> None:
        self._wake_monitor = wake_monitor

    # ── CRUD ──

    def add(self, cron_expr: str | None = None,
            interval_seconds: int | None = None,
            message: str = "",
            once: bool = False) -> int:
        tid = str(self._next_id)
        self._next_id += 1
        task = TimerTask(
            id=tid,
            cron_expr=cron_expr,
            interval_seconds=interval_seconds,
            message=message,
            once=once,
        )
        self._tasks.append(task)
        self._save()
        logger.info("Timer #%s added: cron=%s interval=%s once=%s msg=%s",
                     tid, cron_expr, interval_seconds, once, message)
        return int(tid)

    def remove(self, index: int) -> bool:
        if 0 <= index < len(self._tasks):
            removed = self._tasks.pop(index)
            self._save()
            logger.info("Timer #%s removed", removed.id)
            return True
        return False

    def list_tasks(self) -> list[dict]:
        return [
            {
                "index": i,
                "id": t.id,
                "cron_expr": t.cron_expr,
                "interval_seconds": t.interval_seconds,
                "message": t.message,
                "enabled": t.enabled,
                "once": t.once,
            }
            for i, t in enumerate(self._tasks)
        ]

    def set_enabled(self, index: int, enabled: bool) -> bool:
        if 0 <= index < len(self._tasks):
            self._tasks[index].enabled = enabled
            self._save()
            return True
        return False

    # ── 生命周期 ──

    def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Timer scheduler started (%d tasks)", len(self._tasks))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Timer scheduler stopped")

    async def _loop(self) -> None:
        """每 30 秒检查一次是否有任务需要触发。"""
        while self._running:
            now = time.time()
            dt_now = datetime.now(CST)
            for t in self._tasks:
                if not t.enabled:
                    continue
                if self._should_fire(t, now, dt_now):
                    t.last_fired = now
                    self._save()
                    await self._fire(t)
            await asyncio.sleep(30)

    def _should_fire(self, t: TimerTask, now: float, dt_now: datetime) -> bool:
        if t.interval_seconds is not None:
            if t.last_fired is None:
                return True  # 首次立即触发
            return (now - t.last_fired) >= t.interval_seconds

        if t.cron_expr is not None:
            parsed = _parse_cron_minute_hour(t.cron_expr)
            if parsed is None:
                return False
            minute, hour = parsed
            if dt_now.hour != hour or dt_now.minute != minute:
                return False
            if t.last_fired is not None:
                # 已触发过该分钟，不再重复
                last_dt = datetime.fromtimestamp(t.last_fired, tz=CST)
                if last_dt.hour == hour and last_dt.minute == minute:
                    return False
            return True

        return False

    async def _fire(self, t: TimerTask) -> None:
        """触发定时任务：通过 wake monitor 唤醒 agent。"""
        if self._wake_monitor is None:
            logger.warning("Timer #%s: no wake_monitor, skipping", t.id)
            return
        text = f"[MCP] timer {t.id} {t.message}"
        logger.info("Timer #%s firing: %s", t.id, text)
        await self._wake_monitor.wake_with_message(text)

        # 单次任务触发后自动删除
        if t.once:
            try:
                idx = next(i for i, task in enumerate(self._tasks) if task.id == t.id)
                self._tasks.pop(idx)
                self._save()
                logger.info("Timer #%s auto-removed (single-shot)", t.id)
            except StopIteration:
                pass

    # ── 持久化 ──

    def _save(self) -> None:
        try:
            data = [
                {
                    "id": t.id,
                    "cron_expr": t.cron_expr,
                    "interval_seconds": t.interval_seconds,
                    "message": t.message,
                    "enabled": t.enabled,
                    "once": t.once,
                    "last_fired": t.last_fired,
                }
                for t in self._tasks
            ]
            with open(TIMERS_FILE, "w", encoding="utf-8") as f:
                json.dump({"tasks": data, "next_id": self._next_id},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to save timers: %s", e)

    def _load(self) -> None:
        try:
            if not os.path.isfile(TIMERS_FILE):
                return
            with open(TIMERS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._next_id = raw.get("next_id", 0)
            self._tasks = [
                TimerTask(
                    id=t["id"],
                    cron_expr=t.get("cron_expr"),
                    interval_seconds=t.get("interval_seconds"),
                    message=t.get("message", ""),
                    enabled=t.get("enabled", True),
                    once=t.get("once", False),
                    last_fired=t.get("last_fired"),
                )
                for t in raw.get("tasks", [])
            ]
            logger.info("Loaded %d timer tasks", len(self._tasks))
        except Exception as e:
            logger.warning("Failed to load timers: %s", e)
