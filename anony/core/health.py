import asyncio
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class HealthReporter:
    def __init__(self, interval: int = 15) -> None:
        self.interval = interval
        self.path = Path.cwd() / ".health.json"
        self.started_at = time.time()
        self.state = "starting"
        self.reason = ""
        self._task: asyncio.Task | None = None

    def snapshot(self, event_loop_delay: float = 0.0) -> dict:
        from anony import anon, db, userbot

        return {
            "state": self.state,
            "reason": self.reason,
            "timestamp": time.time(),
            "pid": os.getpid(),
            "started_at": self.started_at,
            "event_loop_delay": round(max(event_loop_delay, 0.0), 3),
            "active_voice_chats": len(db.active_calls),
            "assistants_online": len(userbot.clients),
            "playback_operations": anon.active_operations(),
        }

    def write(self, *, state: str | None = None, reason: str = "", event_loop_delay: float = 0.0) -> None:
        if state:
            self.state = state
        self.reason = reason
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.snapshot(event_loop_delay), ensure_ascii=True),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        expected = loop.time()
        while True:
            expected += self.interval
            await asyncio.sleep(max(0, expected - loop.time()))
            try:
                self.write(event_loop_delay=loop.time() - expected)
            except OSError as exc:
                logger.error("Could not write health heartbeat: %s", exc)

    def start(self) -> None:
        self.write(state="starting")
        self._task = asyncio.create_task(self.run(), name="health-reporter")

    def mark_healthy(self) -> None:
        self.write(state="healthy")

    async def stop(self, reason: str = "graceful shutdown") -> None:
        self.write(state="stopping", reason=reason)
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def fatal(self, reason: str) -> None:
        try:
            self.write(state="fatal", reason=reason[:300])
        except Exception:
            pass


health = HealthReporter()
