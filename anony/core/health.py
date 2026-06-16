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
        from anony import anon, config, db, queue, userbot
        from anony.plugins.broadcast import broadcast_active
        from anony.plugins.play import active_play_requests

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
            "playback_failures": anon.playback_diagnostics(),
            "restart_request": anon.restart_request(),
            "live_queued_requests": queue.live_count(),
            "maintenance_queued_requests": queue.deferred_count(),
            "active_play_requests": active_play_requests,
            "broadcast_active": broadcast_active(),
            "maintenance_grace_remaining": anon.maintenance_grace_remaining(),
            "maintenance_grace_minutes": config.MAINTENANCE_GRACE_MINUTES,
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

    async def process_control_requests(self) -> None:
        from anony import app, db
        from anony.core.commands import sync_command_menus

        async def refresh_sudoers() -> dict:
            previous_privileged = set(app.sudoers)
            sudoers = set(await db.get_sudoers())
            if app.owner:
                sudoers.add(app.owner)
            app.sudoers.clear()
            app.sudoers.update(sudoers)
            try:
                warnings = await asyncio.wait_for(
                    sync_command_menus(previous_privileged),
                    timeout=45,
                )
            except asyncio.TimeoutError:
                logger.warning("Command menu refresh timed out.")
                warnings = ["command menu refresh timed out"]
            except Exception:
                logger.exception("Command menu refresh failed.")
                warnings = ["command menu refresh failed"]
            return {"warnings": warnings, "sudoer_count": len(app.sudoers)}

        async def check_setup() -> dict:
            from anony import config, userbot

            return {
                "owner_configured": bool(app.owner),
                "assistant_slots_online": userbot.available_slots(),
                "logger_available": bool(app.logger),
                "logging_disabled": bool(config.LOGGING_DISABLED),
            }

        async def add_sudoer(payload: dict) -> dict:
            user_id = int(payload.get("user_id") or 0)
            if user_id <= 0:
                raise ValueError("invalid sudo user id")
            await db.add_sudo(user_id)
            data = await refresh_sudoers()
            data["user_id"] = user_id
            return data

        async def del_sudoer(payload: dict) -> dict:
            user_id = int(payload.get("user_id") or 0)
            if user_id <= 0:
                raise ValueError("invalid sudo user id")
            if app.owner and user_id == app.owner:
                raise ValueError("owner cannot be removed from sudo access")
            await db.del_sudo(user_id)
            data = await refresh_sudoers()
            data["user_id"] = user_id
            return data

        async def broadcast_text(payload: dict) -> dict:
            from anony.plugins.broadcast import start_runtime_broadcast

            return await start_runtime_broadcast(
                text=str(payload.get("text") or ""),
                include_users=bool(payload.get("include_users")),
                exclude_groups=bool(payload.get("exclude_groups")),
                requested_by=int(payload.get("requested_by") or 0),
            )

        handlers = {
            "refresh_sudoers": refresh_sudoers,
            "check_setup": check_setup,
            "add_sudoer": add_sudoer,
            "del_sudoer": del_sudoer,
            "broadcast_text": broadcast_text,
        }
        for request_path in Path.cwd().glob(".runtime-control-*.json"):
            if request_path.name.startswith(".runtime-control-result-"):
                continue
            request_id = request_path.stem.removeprefix(".runtime-control-")
            result_path = Path.cwd() / f".runtime-control-result-{request_id}.json"
            result = {"request_id": request_id, "success": False}
            try:
                request = json.loads(request_path.read_text(encoding="utf-8"))
                if request.get("request_id") != request_id:
                    raise ValueError("request identity does not match its filename")
                operation = request.get("operation")
                handler = handlers.get(operation)
                if not handler:
                    raise ValueError("unsupported runtime control operation")
                payload = request.get("payload") or {}
                result.update({
                    "success": True,
                    "data": (
                        await handler(payload)
                        if operation in {"add_sudoer", "del_sudoer", "broadcast_text"}
                        else await handler()
                    ),
                })
                logger.info("Applied runtime control request %s operation=%s.", request_id, operation)
            except Exception as exc:
                logger.exception("Runtime control request %s failed.", request_id)
                result["error"] = f"{type(exc).__name__}: {exc}"[:300]
            finally:
                temporary = result_path.with_suffix(".json.tmp")
                try:
                    temporary.write_text(
                        json.dumps(result, ensure_ascii=True),
                        encoding="utf-8",
                    )
                    temporary.replace(result_path)
                    request_path.unlink(missing_ok=True)
                except OSError:
                    logger.exception("Could not save runtime control result %s.", request_id)

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
            if self.state == "healthy":
                try:
                    await self.process_control_requests()
                except Exception:
                    logger.exception("Could not process runtime control requests.")

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
