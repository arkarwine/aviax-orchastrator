#!/usr/bin/env python3
import asyncio
import html
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.errors import RPCError
from pyrogram.types import Message, ReplyParameters
import psutil

ROOT = Path(__file__).resolve().parent
MANAGER_ENV = ROOT / "manager.env"
STORE_PATH = ROOT / "manager_deployments.json"
SUDO_STORE_PATH = ROOT / "manager_sudoers.json"
DEPLOYMENT_ENV_KEYS = {
    "API_ID",
    "API_HASH",
    "BOT_TOKEN",
    "MONGO_URL",
    "DB_NAME",
    "DEPLOYMENT_ID",
    "MANAGED_SETUP",
    "NAME",
    "OWNER_ID",
    "LOGGER_ID",
    "SESSION",
    "SESSION2",
    "SESSION3",
    "SESSION_PATH",
    "SUPPORT_CHAT",
    "SUPPORT_CHANNEL",
    "OWNER_LINK",
    "LANG_CODE",
    "API_URL",
    "VIDEO_API_URL",
    "API_KEY",
    "DOWNLOADS_PATH",
    "AUTO_LEAVE",
    "AUTO_END",
    "THUMB_GEN",
    "VIDEO_PLAY",
    "COOKIES_URL",
}

load_dotenv(MANAGER_ENV)

logging.basicConfig(
    format="[%(asctime)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


class SecretRedactionFilter(logging.Filter):
    @staticmethod
    def redact(message: str) -> str:
        message = re.sub(r"(?i)(key|token|api_key)=([^&\s\"']+)", r"\1=[REDACTED]", message)
        message = re.sub(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b", "[REDACTED_BOT_TOKEN]", message)
        return re.sub(r"mongodb(?:\+srv)?://[^\s\"']+", "[REDACTED_MONGO_URL]", message)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = self.redact("".join(traceback.format_exception(*record.exc_info)))
        return True


for handler in logging.getLogger().handlers:
    handler.addFilter(SecretRedactionFilter())


@dataclass
class ManagerConfig:
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int
    sudoers: set[int]
    default_mongo_url: Optional[str]
    deployments_dir: Path
    template_path: Path
    api_key: Optional[str]

    @classmethod
    def load(cls) -> "ManagerConfig":
        api_id = int(os.getenv("MANAGER_API_ID", "0"))
        api_hash = os.getenv("MANAGER_API_HASH", "")
        bot_token = os.getenv("MANAGER_BOT_TOKEN", "")
        owner_id = int(os.getenv("MANAGER_OWNER_ID", "0"))
        sudoers = {
            int(value)
            for value in re.split(r"[\s,]+", os.getenv("MANAGER_SUDOERS", "").strip())
            if value.isdigit() and int(value) > 0
        }
        try:
            stored_sudoers = json.loads(SUDO_STORE_PATH.read_text(encoding="utf-8"))
            sudoers = {
                int(value)
                for value in stored_sudoers.get("sudoers", [])
                if str(value).isdigit() and int(value) > 0
            }
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            logger.warning("Could not load manager sudoers from %s.", SUDO_STORE_PATH)
        sudoers.discard(owner_id)
        default_mongo_url = os.getenv("MANAGER_DEFAULT_MONGO_URL")
        deployments_dir = Path(os.getenv("DEPLOYMENTS_DIR", "deployments")).resolve()
        template_path = Path(os.getenv("TEMPLATE_PATH", ".")).resolve()
        api_key = os.getenv("MANAGER_API_KEY")

        missing = []
        if api_id <= 0:
            missing.append("MANAGER_API_ID")
        if not api_hash:
            missing.append("MANAGER_API_HASH")
        if not bot_token:
            missing.append("MANAGER_BOT_TOKEN")
        if owner_id <= 0:
            missing.append("MANAGER_OWNER_ID")
        if missing:
            raise SystemExit(
                "Missing required manager environment variables: " + ", ".join(missing)
            )
        return cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            owner_id=owner_id,
            sudoers=sudoers,
            default_mongo_url=default_mongo_url,
            deployments_dir=deployments_dir,
            template_path=template_path,
            api_key=api_key,
        )

    @property
    def authorized_users(self) -> list[int]:
        return sorted({self.owner_id, *self.sudoers})


@dataclass
class DeploymentMeta:
    name: str
    bot_id: int
    username: str
    created_at: str
    path: str
    db_name: Optional[str] = None
    deployment_id: Optional[str] = None
    pid: Optional[int] = None
    started_at: Optional[str] = None
    process_created_at: Optional[float] = None
    desired_running: bool = False
    intentionally_stopped: bool = False
    restart_history: list[str] = field(default_factory=list)
    last_failure: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["restart_history"] = self.restart_history or []
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeploymentMeta":
        values = dict(data)
        values.setdefault("desired_running", bool(values.get("pid")))
        values.setdefault("intentionally_stopped", not values["desired_running"])
        values.setdefault("restart_history", [])
        return cls(**values)

    @property
    def deployment_path(self) -> Path:
        return ROOT / self.path

    @property
    def is_running(self) -> bool:
        if not self.pid:
            return False
        try:
            process = psutil.Process(self.pid)
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return False
            if self.process_created_at and abs(process.create_time() - self.process_created_at) > 2:
                return False
            try:
                return Path(process.cwd()).resolve() == self.deployment_path.resolve()
            except (psutil.AccessDenied, psutil.ZombieProcess):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False


class DeploymentStore:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self.deployments: Dict[str, DeploymentMeta] = {}
        self.lock = threading.RLock()
        self.load()

    def load(self) -> None:
        if not self.store_path.exists():
            logger.info("Deployment store not found, starting with empty store.")
            self.deployments = {}
            return
        try:
            logger.info("Loading deployment store from %s", self.store_path)
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            self.deployments = {
                name: DeploymentMeta.from_dict(item)
                for name, item in data.get("deployments", {}).items()
            }
            logger.info("Loaded %d deployments.", len(self.deployments))
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse deployment store %s: %s", self.store_path, exc)
            self.deployments = {}

    def save(self) -> None:
        with self.lock:
            logger.info("Saving %d deployments to %s", len(self.deployments), self.store_path)
            temporary = self.store_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(
                    {"deployments": {name: deployment.to_dict() for name, deployment in self.deployments.items()}},
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            temporary.replace(self.store_path)

    def add(self, deployment: DeploymentMeta) -> None:
        with self.lock:
            self.deployments[deployment.name] = deployment
            self.save()

    def update(self, deployment: DeploymentMeta) -> None:
        with self.lock:
            self.deployments[deployment.name] = deployment
            self.save()

    def remove(self, name: str) -> None:
        with self.lock:
            self.deployments.pop(name, None)
            self.save()

    def get(self, name: str) -> Optional[DeploymentMeta]:
        with self.lock:
            return self.deployments.get(name)

    def list(self) -> Dict[str, DeploymentMeta]:
        with self.lock:
            return dict(self.deployments)


def normalize_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "_", value.strip().lower())
    return normalized.strip("_") or "bot"


def validate_database_name(value: str) -> Optional[str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return "Use only letters, numbers, underscores, and hyphens."
    if len(value.encode("utf-8")) > 63:
        return "Keep the database name at 63 bytes or fewer."
    return None


def env_from_template(**values: str) -> Dict[str, str]:
    env = {
        "API_ID": str(values["api_id"]),
        "API_HASH": values["api_hash"],
        "BOT_TOKEN": values["bot_token"],
        "MONGO_URL": values["mongo_url"],
        "DB_NAME": values["db_name"],
        "DEPLOYMENT_ID": values["deployment_id"],
        "MANAGED_SETUP": "True",
        "SESSION_PATH": values["session_path"],
        "NAME": values["name"],
        "AUTO_LEAVE": "False",
        "AUTO_END": "False",
        "THUMB_GEN": "True",
        "VIDEO_PLAY": "True",
        "LANG_CODE": "en",
    }
    for key, default in ("API_URL", values.get("api_url")), ("VIDEO_API_URL", values.get("video_api_url")):
        if default:
            env[key] = default
    if values.get("downloads_path"):
        env["DOWNLOADS_PATH"] = values["downloads_path"]
    if values.get("api_key"):
        env["API_KEY"] = values["api_key"]
    if values.get("owner_id"):
        env["OWNER_ID"] = values["owner_id"]
    return env


def handler_errors(func):
    async def wrapper(self, client: Client, message: Message):
        try:
            await func(self, client, message)
        except Exception as exc:
            logger.exception("Handler %s failed: %s", func.__name__, exc)
            await message.reply_text(
                "❌ I could not complete that request.\n\n"
                "💡 Check the command arguments and try again. If it keeps failing, review the manager logs.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
    return wrapper


class BotManager:
    def __init__(self, config: ManagerConfig, store: DeploymentStore) -> None:
        self.config = config
        self.store = store
        self.shutdown_event = threading.Event()
        self.monitor_interval = max(5, int(os.getenv("MANAGER_MONITOR_INTERVAL", "20")))
        self.health_stale_after = max(30, int(os.getenv("MANAGER_HEALTH_STALE_AFTER", "120")))
        self.health_confirmations = max(2, int(os.getenv("MANAGER_HEALTH_CONFIRMATIONS", "2")))
        self.recovery_limit = max(1, int(os.getenv("MANAGER_RECOVERY_LIMIT", "3")))
        self.recovery_window = max(60, int(os.getenv("MANAGER_RECOVERY_WINDOW", "3600")))
        self.processes: Dict[str, subprocess.Popen] = {}
        self.failed_restarts: set[str] = set()
        self.stale_health_counts: Dict[str, int] = {}
        self.recovering: set[str] = set()
        self.recovery_guard = threading.Lock()
        self.app = Client(
            name="deploy-manager",
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            bot_token=self.config.bot_token,
        )
        logger.info(
            "Manager bot configured for owner_id=%s, sudoers=%d, and deployments_dir=%s",
            self.config.owner_id,
            len(self.config.sudoers),
            self.config.deployments_dir,
        )

        self.authorized_filter = filters.user(self.config.authorized_users)
        owner = filters.user(self.config.owner_id)
        self.app.on_message(filters.private & filters.command("start") & self.authorized_filter)(self.start)
        self.app.on_message(filters.private & filters.command("help") & self.authorized_filter)(self.help)
        self.app.on_message(filters.private & filters.command("newbot") & self.authorized_filter)(self.newbot)
        self.app.on_message(filters.private & filters.command(["reconfigure", "rebuild"]) & self.authorized_filter)(self.reconfigure)
        self.app.on_message(filters.private & filters.command(["changedb", "switchdb"]) & self.authorized_filter)(self.change_database)
        self.app.on_message(filters.private & filters.command("list") & self.authorized_filter)(self.list_bots)
        self.app.on_message(filters.private & filters.command("status") & self.authorized_filter)(self.status)
        self.app.on_message(filters.private & filters.command("deploy") & self.authorized_filter)(self.deploy)
        self.app.on_message(filters.private & filters.command("stop") & self.authorized_filter)(self.stop)
        self.app.on_message(filters.private & filters.command("delete") & self.authorized_filter)(self.delete)
        self.app.on_message(filters.private & filters.command("restart") & self.authorized_filter)(self.restart)
        self.app.on_message(filters.private & filters.command("logs") & self.authorized_filter)(self.logs)
        self.app.on_message(filters.private & filters.command("sudolist") & self.authorized_filter)(self.sudolist)
        self.app.on_message(filters.private & filters.command("addsudo") & owner)(self.addsudo)
        self.app.on_message(filters.private & filters.command(["delsudo", "rmsudo"]) & owner)(self.delsudo)

    @handler_errors
    async def start(self, client: Client, message: Message) -> None:
        await message.reply_text(
            "<b>🚀 Music Bot Deployment Manager</b>\n"
            "📚 Use /help to see available commands.",
            reply_parameters=ReplyParameters(message_id=message.id),
        )

    @handler_errors
    async def help(self, client: Client, message: Message) -> None:
        await message.reply_text(
            "<b>🧰 Manager Commands</b>\n"
            "➕ /newbot &lt;name&gt; &lt;bot_token&gt; [owner_id] [database_name] - Create and start a deployment.\n"
            "🧰 /reconfigure &lt;name&gt; &lt;bot_token&gt; [owner_id] - Reconfigure a deployment while preserving its database.\n"
            "🗄️ /changedb &lt;name&gt; &lt;database_name&gt; - Switch a deployment to another database.\n"
            "📋 /list - Show all deployments.\n"
            "🔎 /status &lt;name&gt; - Show deployment status.\n"
            "▶️ /deploy &lt;name&gt; - Start a stopped deployment.\n"
            "⏹️ /stop &lt;name&gt; - Stop a running deployment.\n"
            "🗑️ /delete &lt;name&gt; - Permanently delete a deployment.\n"
            "🔄 /restart &lt;name&gt; - Restart a deployed bot.\n"
            "📄 /logs &lt;name&gt; - Retrieve a deployment's full log.\n"
            "👥 /sudolist - View manager owner and sudo users.\n"
            "➕ /addsudo &lt;user_id&gt; - Grant manager access. Owner only.\n"
            "➖ /delsudo &lt;user_id&gt; - Remove manager access. Owner only.\n",
            reply_parameters=ReplyParameters(message_id=message.id),
        )

    def save_sudoers(self) -> None:
        temporary = SUDO_STORE_PATH.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"sudoers": sorted(self.config.sudoers)}, indent=2),
            encoding="utf-8",
        )
        temporary.replace(SUDO_STORE_PATH)

    @handler_errors
    async def sudolist(self, client: Client, message: Message) -> None:
        lines = [
            "<b>👥 Manager Access</b>",
            f"\n👑 Owner: <code>{self.config.owner_id}</code>",
        ]
        if self.config.sudoers:
            lines.append("\n<b>🛡️ Sudo users</b>")
            lines.extend(f"• <code>{user_id}</code>" for user_id in sorted(self.config.sudoers))
        else:
            lines.append("\n📭 No additional sudo users.")
        await message.reply_text(
            "\n".join(lines),
            reply_parameters=ReplyParameters(message_id=message.id),
        )

    @handler_errors
    async def addsudo(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2 or not args[1].strip().isdigit() or int(args[1]) <= 0:
            return await message.reply_text(
                "➕ Usage: <code>/addsudo &lt;telegram_user_id&gt;</code>",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        user_id = int(args[1])
        if user_id == self.config.owner_id:
            return await message.reply_text("👑 The manager owner already has full access.")
        if user_id in self.config.sudoers:
            return await message.reply_text(f"🛡️ <code>{user_id}</code> is already a manager sudo user.")

        try:
            user = await client.get_users(user_id)
            if user.is_bot:
                return await message.reply_text("❌ Bot accounts cannot be manager sudo users.")
        except Exception:
            user = None

        self.config.sudoers.add(user_id)
        self.authorized_filter.add(user_id)
        try:
            self.save_sudoers()
        except OSError:
            self.config.sudoers.discard(user_id)
            self.authorized_filter.discard(user_id)
            raise
        label = (
            f"<b>{html.escape(user.first_name or str(user_id))}</b> "
            f"(<code>{user_id}</code>)"
            if user
            else f"<code>{user_id}</code>"
        )
        await message.reply_text(f"✅ Added {label} as a manager sudo user.")

    @handler_errors
    async def delsudo(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2 or not args[1].strip().isdigit() or int(args[1]) <= 0:
            return await message.reply_text(
                "➖ Usage: <code>/delsudo &lt;telegram_user_id&gt;</code>",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        user_id = int(args[1])
        if user_id == self.config.owner_id:
            return await message.reply_text("👑 The manager owner cannot be removed from access.")
        if user_id not in self.config.sudoers:
            return await message.reply_text(f"📭 <code>{user_id}</code> is not a manager sudo user.")

        self.config.sudoers.discard(user_id)
        self.authorized_filter.discard(user_id)
        try:
            self.save_sudoers()
        except OSError:
            self.config.sudoers.add(user_id)
            self.authorized_filter.add(user_id)
            raise
        await message.reply_text(f"✅ Removed <code>{user_id}</code> from manager sudo users.")

    @handler_errors
    async def list_bots(self, client: Client, message: Message) -> None:
        if not self.store.list():
            return await message.reply_text("📭 No deployments found.", reply_parameters=ReplyParameters(message_id=message.id))

        lines = ["<b>📋 Deployments</b>"]
        for name, deployment in self.store.list().items():
            state, _, _ = self.deployment_health(deployment)
            status = self.format_health_state(state)
            lines.append(
                f"<b>{deployment.username}</b> ({name}) — <code>{status}</code>"
            )
        await message.reply_text("\n".join(lines), reply_parameters=ReplyParameters(message_id=message.id))

    @handler_errors
    async def status(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text("🔎 Usage: /status &lt;name&gt;", reply_parameters=ReplyParameters(message_id=message.id))

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(f"❌ Deployment <b>{name}</b> was not found.\n\n💡 Use /list to check registered names.", reply_parameters=ReplyParameters(message_id=message.id))

        state, health, reason = self.deployment_health(deployment)
        text = (
            f"<b>{deployment.username}</b>\n"
            f"Name: <code>{deployment.name}</code>\n"
            f"Bot ID: <code>{deployment.bot_id}</code>\n"
            f"DB: <code>{deployment.db_name or 'legacy'}</code>\n"
            f"Deployment ID: <code>{deployment.deployment_id or 'legacy'}</code>\n"
            f"Status: <code>{state}</code>\n"
            f"PID: <code>{deployment.pid or 'none'}</code>\n"
            f"Desired: <code>{'running' if deployment.desired_running else 'stopped'}</code>\n"
            f"Created: {deployment.created_at}\n"
            f"Started: {deployment.started_at or 'never'}\n"
            f"Path: <code>{deployment.deployment_path}</code>"
        )
        if health:
            try:
                age_text = f"{max(0, int(time.time() - float(health.get('timestamp', 0))))}s"
            except (TypeError, ValueError):
                age_text = "unavailable"
            text += (
                f"\nHeartbeat age: <code>{age_text}</code>"
                f"\nEvent-loop delay: <code>{health.get('event_loop_delay', 0)}s</code>"
                f"\nActive calls: <code>{health.get('active_voice_chats', 0)}</code>"
            )
        if reason:
            text += f"\nReason: <code>{html.escape(reason)}</code>"
        if deployment.last_failure:
            text += f"\nLast failure: <code>{html.escape(deployment.last_failure)}</code>"
        await message.reply_text(text, reply_parameters=ReplyParameters(message_id=message.id))

    @handler_errors
    async def deploy(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text("▶️ Usage: /deploy &lt;name&gt;", reply_parameters=ReplyParameters(message_id=message.id))

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(f"❌ Deployment <b>{name}</b> was not found.\n\n💡 Use /list to check registered names.", reply_parameters=ReplyParameters(message_id=message.id))
        if name in self.recovering:
            return await message.reply_text(
                f"🔄 Deployment <b>{name}</b> is currently being recovered.\n\n"
                "💡 Wait for the recovery report before starting it manually.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        if deployment.is_running:
            return await message.reply_text(
                f"🟢 Deployment process <b>{name}</b> is still running.\n\n"
                f"💡 If the bot is unresponsive or already stopped responding, run <code>/stop {name}</code> "
                f"to finish terminating its process, then run <code>/deploy {name}</code>.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        status = await message.reply_text(f"🚀 Starting deployment <b>{name}</b>...", reply_parameters=ReplyParameters(message_id=message.id))
        started, error = self.start_process(deployment)
        if started:
            self.store.update(deployment)
            await status.edit_text(f"✅ Deployment <b>{name}</b> started.")
        else:
            logger.error("Failed to start deployment %s: %s", name, error)
            await status.edit_text(
                f"❌ Deployment <b>{name}</b> could not start.\n\n"
                "💡 Check its <code>run.log</code>, required dependencies, and generated <code>.env</code>, then try /deploy again."
            )

    @handler_errors
    async def stop(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text("⏹️ Usage: /stop &lt;name&gt;", reply_parameters=ReplyParameters(message_id=message.id))

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(f"❌ Deployment <b>{name}</b> was not found.\n\n💡 Use /list to check registered names.", reply_parameters=ReplyParameters(message_id=message.id))

        if not deployment.is_running:
            deployment.desired_running = False
            deployment.intentionally_stopped = True
            self.stale_health_counts.pop(name, None)
            self.store.update(deployment)
            return await message.reply_text(f"⚫ Deployment <b>{name}</b> is already stopped.", reply_parameters=ReplyParameters(message_id=message.id))

        deployment.desired_running = False
        deployment.intentionally_stopped = True
        self.stale_health_counts.pop(name, None)
        self.store.update(deployment)
        stopped, error = self.stop_process(deployment)
        if stopped:
            self.store.update(deployment)
            await message.reply_text(f"✅ Deployment <b>{name}</b> stopped.", reply_parameters=ReplyParameters(message_id=message.id))
        else:
            logger.error("Failed to stop deployment %s: %s", name, error)
            await message.reply_text(
                f"❌ Deployment <b>{name}</b> could not be stopped.\n\n"
                "💡 Check whether its process still exists, then try again.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

    @handler_errors
    async def logs(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text(
                "📄 Usage: <code>/logs &lt;name&gt;</code>",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> was not found.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        log_path = deployment.deployment_path / "run.log"
        if not log_path.exists():
            return await message.reply_text(
                f"📭 Deployment <b>{name}</b> does not have a run log yet.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        sanitized_path = None
        try:
            content = self.sanitize_text(
                deployment,
                log_path.read_text(encoding="utf-8", errors="replace"),
            )
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=f"-{name}-run.log",
                delete=False,
            ) as sanitized:
                sanitized.write(content)
                sanitized_path = sanitized.name
            await message.reply_document(
                sanitized_path,
                caption=f"📄 Sanitized full run log for <b>{name}</b>",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        finally:
            if sanitized_path:
                Path(sanitized_path).unlink(missing_ok=True)

    @handler_errors
    async def delete(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text(
                "🗑️ Usage: <code>/delete &lt;name&gt;</code>\n\n"
                "⚠️ This permanently removes the deployment directory and manager record.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> was not found.\n\n💡 Use /list to check the registered names.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        if name in self.recovering:
            return await message.reply_text(
                f"🔄 Deployment <b>{name}</b> is currently being recovered.\n\n"
                "💡 Wait for the recovery report before deleting it.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        target = deployment.deployment_path.resolve()
        deployments_root = self.config.deployments_dir.resolve()
        if target.parent != deployments_root:
            logger.error("Refusing unsafe deployment deletion path: %s", target)
            return await message.reply_text(
                "🛑 I refused to delete this deployment because its stored path is outside the deployments directory.\n\n"
                "💡 Correct the deployment record before trying again.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        status = await message.reply_text(
            f"🗑️ Preparing to delete deployment <b>{name}</b>...",
            reply_parameters=ReplyParameters(message_id=message.id),
        )
        if deployment.is_running:
            await status.edit_text(f"⏹️ Stopping deployment <b>{name}</b> before deletion...")
            stopped, _ = self.stop_process(deployment)
            if not stopped and deployment.is_running:
                return await status.edit_text(
                    "❌ The deployment is still running, so it was not deleted.\n\n"
                    "💡 Stop the process manually or use /stop, then run /delete again."
                )

        try:
            if target.exists():
                shutil.rmtree(target)
            self.store.remove(name)
            self.processes.pop(name, None)
            self.failed_restarts.discard(name)
        except Exception:
            logger.exception("Failed to delete deployment %s", name)
            return await status.edit_text(
                "❌ I could not remove the deployment files.\n\n"
                "💡 Check filesystem permissions and make sure no process is using the directory, then try again."
            )

        await status.edit_text(f"✅ Deployment <b>{name}</b> was permanently deleted.")

    @handler_errors
    async def restart(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text(
                "🔄 Usage: <code>/restart &lt;name&gt;</code>\n\n"
                "💡 Use <code>/list</code> to check registered deployment names.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> was not found.\n\n"
                "💡 Use <code>/list</code> to check registered deployment names.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        if name in self.recovering:
            return await message.reply_text(
                f"🔄 Deployment <b>{name}</b> is already being recovered automatically.\n\n"
                "💡 Wait for the recovery report before restarting it manually.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        status = await message.reply_text(
            f"🔄 Restarting deployment <b>{name}</b>...",
            reply_parameters=ReplyParameters(message_id=message.id),
        )
        if deployment.is_running:
            await status.edit_text(f"⏹️ Stopping deployment <b>{name}</b>...")
            stopped, error = self.stop_process(deployment)
            if not stopped:
                logger.error("Failed to stop deployment %s for restart: %s", name, error)
                return await status.edit_text(
                    f"❌ Deployment <b>{name}</b> could not be stopped, so it was not restarted.\n\n"
                    f"💡 Run <code>/stop {name}</code>, review the manager logs, then try again."
                )
            self.store.update(deployment)
        elif deployment.pid:
            deployment.pid = None
            self.processes.pop(deployment.name, None)
            self.store.update(deployment)

        await status.edit_text(f"🚀 Starting deployment <b>{name}</b>...")
        started, error = self.start_process(deployment)
        if not started:
            self.store.update(deployment)
            logger.error("Failed to restart deployment %s: %s", name, error)
            return await status.edit_text(
                f"❌ Deployment <b>{name}</b> stopped but could not start again.\n\n"
                f"💡 Check its <code>run.log</code>, then run <code>/deploy {name}</code>."
            )

        deployment.started_at = datetime.now(timezone.utc).isoformat()
        self.store.update(deployment)
        await status.edit_text(
            f"✅ Deployment <b>{name}</b> restarted successfully.\n"
            f"PID: <code>{deployment.pid}</code>"
        )

    @handler_errors
    async def newbot(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=4)
        if len(args) < 3:
            return await message.reply_text(
                "➕ Usage: /newbot &lt;name&gt; &lt;bot_token&gt; [owner_id] [database_name]\n\n"
                "💡 To set a database name without an owner ID, provide the database name directly.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        name = normalize_name(args[1])
        bot_token = args[2].strip()
        owner_id = ""
        requested_db_name = ""
        if len(args) > 3:
            optional_value = args[3].strip()
            if optional_value.isdigit():
                owner_id = optional_value
                requested_db_name = args[4].strip() if len(args) > 4 else ""
            elif optional_value == "-":
                requested_db_name = args[4].strip() if len(args) > 4 else ""
            else:
                requested_db_name = optional_value
                if len(args) > 4:
                    return await message.reply_text(
                        "❌ I could not understand the optional arguments.\n\n"
                        "💡 Use <code>/newbot &lt;name&gt; &lt;token&gt; [owner_id] [database_name]</code>."
                    )
        mongo_url = self.config.default_mongo_url
        logger.info("Received newbot request for %s", name)

        if requested_db_name:
            database_error = validate_database_name(requested_db_name)
            if database_error:
                return await message.reply_text(
                    f"❌ Invalid database name <code>{requested_db_name}</code>.\n\n"
                    f"💡 {database_error}",
                    reply_parameters=ReplyParameters(message_id=message.id),
                )
        elif len(args) > 3 and args[3].strip() == "-":
            return await message.reply_text(
                "❌ A database name is required after the owner placeholder.\n\n"
                "💡 Example: <code>/newbot music_bot token - music_database</code>",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        if not mongo_url:
            logger.warning("No MongoDB URL provided for new deployment %s", name)
            return await message.reply_text(
                "❌ New deployments need a MongoDB connection.\n\n"
                "💡 Add <code>MANAGER_DEFAULT_MONGO_URL</code> to <code>manager.env</code>, then restart the manager.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        if name in self.store.list():
            return await message.reply_text(
                f"⚠️ A deployment named <b>{name}</b> already exists.\n\n"
                f"💡 Use <code>/reconfigure {name} &lt;bot_token&gt;</code> to run the deployment setup again while preserving its database.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        status = await message.reply_text(f"🔎 Verifying the bot token for <b>{name}</b>...", reply_parameters=ReplyParameters(message_id=message.id))

        try:
            bot_user = await self.verify_bot_token(bot_token)
            logger.info("Verified bot token for %s (%s)", name, bot_user.username or bot_user.first_name)
        except RPCError:
            logger.exception("Bot token verification failed for %s", name)
            return await status.edit_text(
                "❌ I could not verify that bot token.\n\n"
                "💡 Copy a fresh token from @BotFather, make sure it belongs to a bot, then try /newbot again."
            )

        await status.edit_text(f"🧱 Creating isolated deployment <b>{name}</b>...")

        deployment_dir = self.config.deployments_dir / name
        deployment_dir.mkdir(parents=True, exist_ok=False)

        env_path = deployment_dir / ".env"
        manager_downloads_path = os.getenv("MANAGER_DOWNLOADS_PATH", "")
        if manager_downloads_path and not Path(manager_downloads_path).is_absolute():
            manager_downloads_path = str((ROOT / manager_downloads_path).resolve())

        deployment_id = uuid.uuid4().hex
        db_name = requested_db_name or normalize_name(f"{name}_{bot_user.id}_{deployment_id[:8]}")
        env_vars = env_from_template(
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            bot_token=bot_token,
            mongo_url=mongo_url,
            db_name=db_name,
            deployment_id=deployment_id,
            session_path=str(deployment_dir),
            name=name,
            api_url=os.getenv("DEFAULT_API_URL", ""),
            video_api_url=os.getenv("DEFAULT_VIDEO_API_URL", ""),
            downloads_path=manager_downloads_path,
            api_key=self.config.api_key,
            owner_id=owner_id,
        )
        env_path.write_text("\n".join(f"{key}={value}" for key, value in env_vars.items()), encoding="utf-8")

        deployment = DeploymentMeta(
            name=name,
            bot_id=bot_user.id,
            username=f"@{bot_user.username}" if bot_user.username else bot_user.first_name,
            created_at=datetime.now(timezone.utc).isoformat(),
            path=str(deployment_dir.relative_to(ROOT)),
            db_name=db_name,
            deployment_id=deployment_id,
        )

        started, error = self.start_process(deployment)
        if started:
            deployment.started_at = datetime.now(timezone.utc).isoformat()
            self.store.add(deployment)
            logger.info("Created and started deployment %s pid=%s", name, deployment.pid)
            created_text = (
                f"✅ Deployment <b>{name}</b> created and started.\n"
                f"Bot: <code>{deployment.username}</code>\n"
                f"DB: <code>{deployment.db_name}</code>\n"
                f"Deployment ID: <code>{deployment.deployment_id}</code>\n"
            )
            if owner_id:
                created_text += f"Owner ID: <code>{owner_id}</code>\n"
            created_text += f"Path: <code>{deployment.deployment_path}</code>\n\n"
            if owner_id:
                created_text += "➡️ Next: continue setup from the deployed bot."
            else:
                created_text += "➡️ Next: send /start to the deployed bot in private chat."
            await status.edit_text(created_text)
        else:
            logger.error("Deployment %s creation failed to start: %s", name, error)
            await status.edit_text(
                f"⚠️ Deployment <b>{name}</b> was created, but it could not start.\n\n"
                "💡 Check its <code>run.log</code>, dependencies, and generated <code>.env</code>, then use /deploy."
            )

    @handler_errors
    async def reconfigure(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=3)
        if len(args) < 3:
            return await message.reply_text(
                "🧰 Usage: /reconfigure &lt;name&gt; &lt;bot_token&gt; [owner_id]\n\n"
                "💾 The existing database, deployment identity, and stored bot setup will be preserved.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        name = normalize_name(args[1])
        bot_token = args[2].strip()
        owner_id = args[3].strip() if len(args) > 3 else ""
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> was not found.\n\n"
                "💡 Use <code>/newbot</code> to create a new deployment or <code>/list</code> to check registered names.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        if name in self.recovering:
            return await message.reply_text(
                f"🔄 Deployment <b>{name}</b> is currently being recovered.\n\n"
                "💡 Wait for the recovery report before reconfiguring it.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        if owner_id and not owner_id.isdigit():
            return await message.reply_text(
                "❌ Owner ID must be numeric.\n\n"
                "💡 Send only the Telegram user ID, for example <code>123456789</code>.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        if not deployment.db_name or not deployment.deployment_id:
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> does not have a stored database identity.\n\n"
                "💡 Restore its <code>db_name</code> and <code>deployment_id</code> in the manager deployment record before reconfiguring it.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        status = await message.reply_text(
            f"🔎 Verifying the new bot token for <b>{name}</b>...",
            reply_parameters=ReplyParameters(message_id=message.id),
        )
        try:
            bot_user = await self.verify_bot_token(bot_token)
        except Exception:
            logger.exception("Replacement bot token verification failed for %s", name)
            return await status.edit_text(
                "❌ I could not verify that bot token.\n\n"
                "💡 Copy a fresh token from @BotFather, make sure it belongs to a bot, then try again."
            )

        env_path = deployment.deployment_path / ".env"
        if not env_path.exists():
            return await status.edit_text(
                f"❌ Deployment <b>{name}</b> is missing its <code>.env</code> file.\n\n"
                "💡 Restore the deployment files before reconfiguring it. Its database was not changed."
            )
        env_vars = self.load_deployment_env(env_path)
        mongo_url = env_vars.get("MONGO_URL")
        if not mongo_url:
            return await status.edit_text(
                f"❌ Deployment <b>{name}</b> does not have a stored MongoDB connection.\n\n"
                "💡 Restore <code>MONGO_URL</code> in its <code>.env</code>, then try again. Its database was not changed."
            )

        await status.edit_text(f"⏹️ Stopping deployment <b>{name}</b> safely...")
        if deployment.is_running:
            stopped, error = self.stop_process(deployment)
            if not stopped:
                logger.error("Could not stop deployment %s before reconfiguration: %s", name, error)
                return await status.edit_text(
                    f"❌ Deployment <b>{name}</b> could not be stopped, so no settings were changed.\n\n"
                    f"💡 Run <code>/stop {name}</code>, then try <code>/reconfigure</code> again."
                )
            self.store.update(deployment)
        elif deployment.pid:
            deployment.pid = None
            self.processes.pop(deployment.name, None)
            self.store.update(deployment)

        await status.edit_text(f"💾 Updating deployment <b>{name}</b> while preserving its database...")
        env_vars.update(
            {
                "API_ID": str(self.config.api_id),
                "API_HASH": self.config.api_hash,
                "BOT_TOKEN": bot_token,
                "MONGO_URL": mongo_url,
                "DB_NAME": deployment.db_name,
                "DEPLOYMENT_ID": deployment.deployment_id,
                "MANAGED_SETUP": "True",
                "SESSION_PATH": str(deployment.deployment_path),
                "NAME": deployment.name,
            }
        )
        if self.config.api_key:
            env_vars["API_KEY"] = self.config.api_key
        if owner_id:
            env_vars["OWNER_ID"] = owner_id

        env_tmp_path = env_path.with_name(".env.tmp")
        try:
            env_tmp_path.write_text(
                "\n".join(f"{key}={value}" for key, value in env_vars.items()),
                encoding="utf-8",
            )
            env_tmp_path.replace(env_path)
        except Exception:
            logger.exception("Could not update deployment environment for %s", name)
            return await status.edit_text(
                f"❌ I could not update deployment <b>{name}</b>.\n\n"
                "💡 Check the deployment directory permissions and try again. Its database was not changed."
            )

        await status.edit_text(f"🔐 Refreshing the bot login for <b>{name}</b>...")
        try:
            for filename in ("Anony.session", "Anony.session-journal"):
                (deployment.deployment_path / filename).unlink(missing_ok=True)
        except Exception:
            logger.exception("Could not refresh bot login session for %s", name)
            return await status.edit_text(
                f"❌ Deployment <b>{name}</b> was updated, but its old bot login could not be cleared.\n\n"
                "💡 Check the deployment directory permissions, then run <code>/reconfigure</code> again. Its database was not changed."
            )

        deployment.bot_id = bot_user.id
        deployment.username = f"@{bot_user.username}" if bot_user.username else bot_user.first_name
        await status.edit_text(f"🚀 Starting reconfigured deployment <b>{name}</b>...")
        started, error = self.start_process(deployment)
        if not started:
            self.store.update(deployment)
            logger.error("Reconfigured deployment %s could not start: %s", name, error)
            return await status.edit_text(
                f"⚠️ Deployment <b>{name}</b> was reconfigured but could not start.\n\n"
                f"💾 Database <code>{deployment.db_name}</code> was preserved.\n"
                f"💡 Check its <code>run.log</code>, then run <code>/deploy {name}</code>."
            )

        deployment.started_at = datetime.now(timezone.utc).isoformat()
        self.store.update(deployment)
        await status.edit_text(
            f"✅ Deployment <b>{name}</b> reconfigured and started.\n"
            f"Bot: <code>{deployment.username}</code>\n"
            f"DB: <code>{deployment.db_name}</code> preserved\n"
            f"Deployment ID: <code>{deployment.deployment_id}</code> preserved\n\n"
            "✨ Existing setup and stored data remain available."
        )

    @handler_errors
    async def change_database(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=2)
        if len(args) < 3:
            return await message.reply_text(
                "🗄️ Usage: <code>/changedb &lt;name&gt; &lt;database_name&gt;</code>\n\n"
                "⚠️ This switches the deployment to another database. It does not copy or delete data.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        name = normalize_name(args[1])
        database_name = args[2].strip()
        database_error = validate_database_name(database_name)
        if database_error:
            return await message.reply_text(
                f"❌ Invalid database name <code>{database_name}</code>.\n\n"
                f"💡 {database_error}",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> was not found.\n\n"
                "💡 Use <code>/list</code> to check registered deployment names.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        if name in self.recovering:
            return await message.reply_text(
                f"🔄 Deployment <b>{name}</b> is currently being recovered.\n\n"
                "💡 Wait for the recovery report before changing its database.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        if deployment.db_name == database_name:
            return await message.reply_text(
                f"🟢 Deployment <b>{name}</b> already uses database <code>{database_name}</code>.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        env_path = deployment.deployment_path / ".env"
        if not env_path.exists():
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> is missing its <code>.env</code> file.\n\n"
                "💡 Restore the deployment files before changing its database.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        env_vars = self.load_deployment_env(env_path)
        if not env_vars.get("MONGO_URL"):
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> does not have a stored MongoDB connection.\n\n"
                "💡 Restore <code>MONGO_URL</code> in its <code>.env</code>, then try again.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        previous_database = deployment.db_name or env_vars.get("DB_NAME") or "unknown"
        status = await message.reply_text(
            f"🗄️ Preparing to switch <b>{name}</b> from <code>{previous_database}</code> "
            f"to <code>{database_name}</code>...\n\n"
            "⚠️ Existing data will remain in the previous database and will not be copied.",
            reply_parameters=ReplyParameters(message_id=message.id),
        )

        if deployment.is_running:
            await status.edit_text(f"⏹️ Stopping deployment <b>{name}</b> safely...")
            stopped, error = self.stop_process(deployment)
            if not stopped:
                logger.error("Could not stop deployment %s before database switch: %s", name, error)
                return await status.edit_text(
                    f"❌ Deployment <b>{name}</b> could not be stopped, so its database was not changed.\n\n"
                    f"💡 Run <code>/stop {name}</code>, then try again."
                )
            self.store.update(deployment)
        elif deployment.pid:
            deployment.pid = None
            self.processes.pop(deployment.name, None)
            self.store.update(deployment)

        await status.edit_text(f"💾 Switching deployment <b>{name}</b> to <code>{database_name}</code>...")
        env_vars["DB_NAME"] = database_name
        env_tmp_path = env_path.with_name(".env.tmp")
        try:
            env_tmp_path.write_text(
                "\n".join(f"{key}={value}" for key, value in env_vars.items()),
                encoding="utf-8",
            )
            env_tmp_path.replace(env_path)
        except Exception:
            logger.exception("Could not update database for deployment %s", name)
            return await status.edit_text(
                f"❌ I could not update deployment <b>{name}</b>.\n\n"
                f"💡 Check the deployment directory permissions. It still uses <code>{previous_database}</code>."
            )

        deployment.db_name = database_name
        self.store.update(deployment)
        await status.edit_text(f"🚀 Starting deployment <b>{name}</b> with its new database...")
        started, error = self.start_process(deployment)
        if not started:
            self.store.update(deployment)
            logger.error("Deployment %s could not start after database switch: %s", name, error)
            return await status.edit_text(
                f"⚠️ Deployment <b>{name}</b> now points to <code>{database_name}</code>, but it could not start.\n\n"
                f"💡 Check its <code>run.log</code>, then run <code>/deploy {name}</code>.\n"
                f"↩️ To return to the previous database, run <code>/changedb {name} {previous_database}</code>."
            )

        deployment.started_at = datetime.now(timezone.utc).isoformat()
        self.store.update(deployment)
        await status.edit_text(
            f"✅ Deployment <b>{name}</b> switched databases and started successfully.\n\n"
            f"🗄️ Current database: <code>{database_name}</code>\n"
            f"📦 Previous database: <code>{previous_database}</code>\n\n"
            "ℹ️ No data was copied or deleted."
        )

    async def verify_bot_token(self, bot_token: str):
        logger.info("Verifying bot token with temporary client.")
        temp = Client(
            name="verify-bot",
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            bot_token=bot_token,
            in_memory=True,
        )
        await temp.start()
        try:
            bot = await temp.get_me()
            logger.info("Bot token verified for %s", bot.username or bot.first_name)
            return bot
        finally:
            await temp.stop()

    def _log_task_exception(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning("Scheduled app task failed: %s", exc, exc_info=exc)

    def _run_on_app_loop(self, func, *args, **kwargs) -> None:
        if not hasattr(self.app, "loop"):
            return
        try:
            def schedule() -> None:
                task = asyncio.create_task(func(*args, **kwargs))
                task.add_done_callback(self._log_task_exception)

            self.app.loop.call_soon_threadsafe(schedule)
        except Exception as exc:
            logger.warning("Failed to schedule app call %s: %s", func.__name__, exc)

    async def _send_owner_with_retry(self, text: str) -> None:
        for attempt in range(1, 4):
            try:
                await self.app.send_message(self.config.owner_id, text)
                return
            except Exception as exc:
                logger.warning("Owner notification attempt %s failed: %s", attempt, exc)
                if attempt < 3:
                    await asyncio.sleep(attempt * 2)
        logger.error("Owner notification could not be delivered after 3 attempts.")

    def _notify_owner(self, text: str) -> None:
        self._run_on_app_loop(self._send_owner_with_retry, text)

    def process_matches(self, deployment: DeploymentMeta) -> bool:
        if not deployment.pid:
            return False
        try:
            process = psutil.Process(deployment.pid)
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return False
            if deployment.process_created_at and abs(process.create_time() - deployment.process_created_at) > 2:
                return False
            try:
                return Path(process.cwd()).resolve() == deployment.deployment_path.resolve()
            except (psutil.AccessDenied, psutil.ZombieProcess):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False

    def read_health(self, deployment: DeploymentMeta) -> Optional[dict]:
        path = deployment.deployment_path / ".health.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("pid") != deployment.pid:
                return None
            return data
        except (OSError, ValueError, TypeError):
            return None

    def deployment_health(self, deployment: DeploymentMeta) -> tuple[str, Optional[dict], str]:
        if deployment.name in self.recovering:
            return "recovering", self.read_health(deployment), "automatic recovery in progress"
        if not deployment.desired_running:
            return "stopped", self.read_health(deployment), "stopped intentionally"
        if not self.process_matches(deployment):
            return "stopped", self.read_health(deployment), "deployment process is not running"

        health = self.read_health(deployment)
        if not health:
            if not deployment.process_created_at:
                return "healthy", None, "heartbeat monitoring activates after the deployment's next restart"
            started = self.parse_timestamp(deployment.started_at)
            if started and time.time() - started < self.health_stale_after:
                return "starting", None, "waiting for the first heartbeat"
            return "frozen", None, "heartbeat file is missing or belongs to another process"

        try:
            age = time.time() - float(health.get("timestamp", 0))
        except (TypeError, ValueError):
            return "frozen", health, "heartbeat timestamp is invalid"
        state = str(health.get("state", "healthy"))
        if state == "fatal":
            return "frozen", health, str(health.get("reason") or "fatal runtime error")
        if age <= self.health_stale_after:
            return ("starting" if state == "starting" else "healthy"), health, ""
        return "frozen", health, f"heartbeat is {int(age)} seconds old"

    @staticmethod
    def format_health_state(state: str) -> str:
        return {
            "healthy": "🟢 healthy",
            "starting": "🟡 starting",
            "frozen": "🔴 frozen",
            "recovering": "🔄 recovering",
            "stopped": "⚫ stopped",
        }.get(state, state)

    @staticmethod
    def parse_timestamp(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None

    def sanitize_text(self, deployment: DeploymentMeta, text: str) -> str:
        env = self.load_deployment_env(deployment.deployment_path / ".env")
        for key in ("BOT_TOKEN", "API_HASH", "API_KEY", "MONGO_URL", "SESSION", "SESSION1", "SESSION2", "SESSION3"):
            secret = env.get(key)
            if secret:
                text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?i)(key|token|api_key)=([^&\s\"']+)", r"\1=[REDACTED]", text)
        text = re.sub(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b", "[REDACTED_BOT_TOKEN]", text)
        text = re.sub(r"mongodb(?:\+srv)?://[^\s\"']+", "[REDACTED_MONGO_URL]", text)
        return text

    def diagnostic_report(self, deployment: DeploymentMeta, reason: str) -> str:
        health = self.read_health(deployment) or {}
        lines = []
        log_path = deployment.deployment_path / "run.log"
        if log_path.exists():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
            except OSError:
                lines = []

        cpu = memory = 0.0
        children = ffmpeg = 0
        if deployment.pid:
            try:
                process = psutil.Process(deployment.pid)
                cpu = process.cpu_percent(interval=0.2)
                memory = process.memory_info().rss / 1024**2
                child_processes = process.children(recursive=True)
                children = len(child_processes)
                for child in child_processes:
                    try:
                        ffmpeg += int("ffmpeg" in child.name().lower())
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        system_memory = psutil.virtual_memory()
        system_cpu = psutil.cpu_percent(interval=0.2)
        started = self.parse_timestamp(deployment.started_at)
        uptime = int(time.time() - started) if started else 0
        tail = self.sanitize_text(deployment, "\n".join(lines))
        tail = html.escape(tail[-1200:])[:2200] if tail else "No recent log lines were available."
        return (
            f"<b>⚠️ Deployment health failure: {html.escape(deployment.name)}</b>\n\n"
            f"Reason: <code>{html.escape(reason)}</code>\n"
            f"PID: <code>{deployment.pid or 'none'}</code>\n"
            f"Uptime: <code>{uptime}s</code>\n"
            f"Last heartbeat: <code>{health.get('timestamp', 'unavailable')}</code>\n"
            f"Event-loop delay: <code>{health.get('event_loop_delay', 'unavailable')}s</code>\n"
            f"Bot CPU: <code>{cpu:.1f}%</code>\n"
            f"Bot memory: <code>{memory:.1f} MB</code>\n"
            f"System CPU: <code>{system_cpu:.1f}%</code>\n"
            f"System memory: <code>{system_memory.percent:.1f}%</code>\n"
            f"Child processes: <code>{children}</code> (FFmpeg: <code>{ffmpeg}</code>)\n"
            f"Recent automatic recoveries: <code>{len(deployment.restart_history or [])}</code>\n"
            f"Playback operations: <code>{html.escape(str(health.get('playback_operations', {})))[:500]}</code>\n\n"
            f"<b>Recent sanitized log lines</b>\n<pre>{tail}</pre>"
        )

    def recent_recoveries(self, deployment: DeploymentMeta) -> list[str]:
        cutoff = time.time() - self.recovery_window
        recent = [
            value for value in (deployment.restart_history or [])
            if (self.parse_timestamp(value) or 0) >= cutoff
        ]
        deployment.restart_history = recent
        return recent

    def recover_deployment(self, deployment: DeploymentMeta, reason: str) -> None:
        with self.recovery_guard:
            if (
                deployment.name in self.recovering
                or deployment.intentionally_stopped
                or not deployment.desired_running
            ):
                return
            self.recovering.add(deployment.name)
        try:
            if deployment.intentionally_stopped or not deployment.desired_running:
                return
            recent = self.recent_recoveries(deployment)
            if len(recent) >= self.recovery_limit:
                if deployment.name in self.failed_restarts:
                    return
                report = self.diagnostic_report(deployment, reason)
                deployment.last_failure = f"Recovery limit reached: {reason}"
                self.store.update(deployment)
                self.failed_restarts.add(deployment.name)
                self._notify_owner(
                    report
                    + f"\n\n🛑 Automatic recovery stopped after {self.recovery_limit} attempts "
                    f"within one hour. Inspect the deployment and use <code>/restart {deployment.name}</code>."
                )
                return

            report = self.diagnostic_report(deployment, reason)
            if self.process_matches(deployment):
                stopped, error = self.stop_process(deployment)
                if not stopped:
                    deployment.last_failure = f"Frozen process could not be stopped: {error}"
                    self.store.update(deployment)
                    self._notify_owner(report + "\n\n❌ Automatic recovery failed because the frozen process could not be stopped.")
                    return
            else:
                deployment.pid = None
                deployment.process_created_at = None

            if deployment.intentionally_stopped or not deployment.desired_running:
                self.store.update(deployment)
                self._notify_owner(
                    report
                    + f"\n\n⏹️ Automatic recovery for <b>{deployment.name}</b> was cancelled because it was stopped intentionally."
                )
                return

            deployment.restart_history = recent + [datetime.now(timezone.utc).isoformat()]
            deployment.last_failure = reason
            started, error = self.start_process(deployment, mark_desired=False)
            self.store.update(deployment)
            if not started:
                if deployment.intentionally_stopped or not deployment.desired_running:
                    return
                self._notify_owner(
                    report
                    + f"\n\n❌ Automatic restart failed. Use <code>/deploy {deployment.name}</code> after checking the logs."
                )
                return
            if deployment.intentionally_stopped or not deployment.desired_running:
                self.stop_process(deployment)
                self.store.update(deployment)
                self._notify_owner(
                    report
                    + f"\n\n⏹️ Deployment <b>{deployment.name}</b> was stopped because an intentional stop was requested during recovery."
                )
                return

            recovered = False
            deadline = time.time() + max(self.health_stale_after, 120)
            while time.time() < deadline and not self.shutdown_event.wait(3):
                health = self.read_health(deployment)
                try:
                    fresh = health and time.time() - float(health.get("timestamp", 0)) <= self.health_stale_after
                except (TypeError, ValueError):
                    fresh = False
                if self.process_matches(deployment) and fresh and health.get("state") == "healthy":
                    recovered = True
                    break
                if not self.process_matches(deployment):
                    break
            self.store.update(deployment)
            if deployment.intentionally_stopped or not deployment.desired_running:
                self._notify_owner(
                    report
                    + f"\n\n⏹️ Automatic recovery for <b>{deployment.name}</b> ended because it was stopped intentionally."
                )
                return
            if recovered:
                self._notify_owner(
                    report
                    + f"\n\n✅ Deployment <b>{deployment.name}</b> was restarted automatically because it stopped responding."
                )
            else:
                self._notify_owner(
                    report
                    + f"\n\n⚠️ Deployment <b>{deployment.name}</b> was restarted automatically, "
                    "but it did not become healthy before the recovery check timed out."
                )
        finally:
            self.stale_health_counts.pop(deployment.name, None)
            with self.recovery_guard:
                self.recovering.discard(deployment.name)

    def _send_deployment_log(self, deployment: DeploymentMeta) -> None:
        log_path = deployment.deployment_path / "run.log"
        if not log_path.exists():
            self._notify_owner(
                f"⚠️ Log file for deployment <b>{deployment.name}</b> was not found."
            )
            return

        self._run_on_app_loop(self._send_sanitized_log, deployment)

    async def _send_sanitized_log(self, deployment: DeploymentMeta) -> None:
        log_path = deployment.deployment_path / "run.log"
        sanitized_path = None
        try:
            content = self.sanitize_text(
                deployment,
                log_path.read_text(encoding="utf-8", errors="replace"),
            )
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=f"-{deployment.name}-run.log",
                delete=False,
            ) as sanitized:
                sanitized.write(content)
                sanitized_path = sanitized.name
            await self.app.send_document(
                self.config.owner_id,
                sanitized_path,
                caption=f"Sanitized error log for <b>{deployment.name}</b>",
            )
        except Exception as exc:
            logger.warning("Could not send deployment log for %s: %s", deployment.name, exc)
            await self._send_owner_with_retry(
                f"⚠️ I could not send the error log for <b>{deployment.name}</b>.\n\n"
                "💡 Check the deployment directory permissions and manager logs."
            )
        finally:
            if sanitized_path:
                Path(sanitized_path).unlink(missing_ok=True)

    def _monitor_loop(self) -> None:
        logger.info("Deployment watcher started with interval %s seconds.", self.monitor_interval)
        while not self.shutdown_event.wait(self.monitor_interval):
            for deployment in list(self.store.list().values()):
                try:
                    state, _, reason = self.deployment_health(deployment)
                    if state in {"healthy", "starting", "recovering"} or (
                        state == "stopped" and not deployment.desired_running
                    ):
                        self.stale_health_counts.pop(deployment.name, None)
                        continue
                    confirmations = self.stale_health_counts.get(deployment.name, 0) + 1
                    self.stale_health_counts[deployment.name] = confirmations
                    logger.warning(
                        "Deployment %s health check failed (%s/%s): %s",
                        deployment.name,
                        confirmations,
                        self.health_confirmations,
                        reason,
                    )
                    if confirmations >= self.health_confirmations:
                        if deployment.intentionally_stopped or not deployment.desired_running:
                            self.stale_health_counts.pop(deployment.name, None)
                            continue
                        threading.Thread(
                            target=self.recover_deployment,
                            args=(deployment, reason),
                            name=f"recover-{deployment.name}",
                            daemon=True,
                        ).start()
                except Exception:
                    logger.exception("Deployment health check failed unexpectedly for %s", deployment.name)

    def start_process(
        self,
        deployment: DeploymentMeta,
        *,
        mark_desired: bool = True,
    ) -> tuple[bool, Optional[str]]:
        if not mark_desired and (
            deployment.intentionally_stopped or not deployment.desired_running
        ):
            logger.info(
                "Refusing automatic start for intentionally stopped deployment %s.",
                deployment.name,
            )
            return False, "Deployment was intentionally stopped."

        env = os.environ.copy()
        for key in DEPLOYMENT_ENV_KEYS:
            env.pop(key, None)
        deployment_env = self.load_deployment_env(deployment.deployment_path / ".env")
        env.update(deployment_env)
        manager_downloads_path = os.getenv("MANAGER_DOWNLOADS_PATH")
        if manager_downloads_path:
            if not Path(manager_downloads_path).is_absolute():
                manager_downloads_path = str((ROOT / manager_downloads_path).resolve())
            env["DOWNLOADS_PATH"] = manager_downloads_path
        if self.config.api_key:
            env["API_KEY"] = self.config.api_key
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = str(self.config.template_path)

        log_file = deployment.deployment_path / "run.log"
        health_file = deployment.deployment_path / ".health.json"
        launch_pid_file = deployment.deployment_path / ".manager-launch.pid"
        try:
            health_file.unlink(missing_ok=True)
            launch_pid_file.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove stale launch files for %s", deployment.name)
        logger.info("Starting deployment %s at %s", deployment.name, deployment.deployment_path)
        try:
            if os.name == "posix":
                launcher = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "deployment_launcher.py"),
                        "--cwd",
                        str(deployment.deployment_path),
                        "--log",
                        str(log_file),
                        "--pid-file",
                        str(launch_pid_file),
                    ],
                    cwd=ROOT,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=15,
                )
                if launcher.returncode != 0:
                    raise RuntimeError(
                        launcher.stderr.strip() or "detached deployment launcher failed"
                    )
                deployment.pid = int(launch_pid_file.read_text(encoding="ascii").strip())
                launch_pid_file.unlink(missing_ok=True)
            else:
                with open(os.devnull, "rb") as stdin, log_file.open("a", encoding="utf-8") as stdout:
                    process = subprocess.Popen(
                        [sys.executable, "-m", "anony"],
                        cwd=deployment.deployment_path,
                        env=env,
                        stdin=stdin,
                        stdout=stdout,
                        stderr=subprocess.STDOUT,
                        start_new_session=os.name == "posix",
                    )
                deployment.pid = process.pid
                self.processes[deployment.name] = process

            deployment.process_created_at = psutil.Process(deployment.pid).create_time()
            if not mark_desired and (
                deployment.intentionally_stopped or not deployment.desired_running
            ):
                logger.info(
                    "Cancelling automatic start for intentionally stopped deployment %s.",
                    deployment.name,
                )
                self.stop_process(deployment)
                return False, "Deployment was intentionally stopped."

            if mark_desired:
                deployment.desired_running = True
                deployment.intentionally_stopped = False
            deployment.started_at = datetime.now(timezone.utc).isoformat()
            self.failed_restarts.discard(deployment.name)
            logger.info("Deployment %s started with pid=%s", deployment.name, deployment.pid)
            return True, None
        except Exception as exc:
            try:
                launch_pid_file.unlink(missing_ok=True)
            except OSError:
                pass
            error_text = str(exc)
            logger.error("Failed to start deployment %s: %s", deployment.name, error_text)
            return False, error_text

    def stop_process(self, deployment: DeploymentMeta) -> tuple[bool, Optional[str]]:
        if not deployment.pid:
            logger.warning("Deployment %s has no pid to stop.", deployment.name)
            return False, "No pid found for deployment."
        if not self.process_matches(deployment):
            logger.warning("Deployment %s has a stale or mismatched pid; clearing it without sending a signal.", deployment.name)
            deployment.pid = None
            deployment.process_created_at = None
            self.processes.pop(deployment.name, None)
            return True, None
        pid = deployment.pid
        logger.info("Stopping deployment %s pid=%s", deployment.name, pid)
        process = self.processes.get(deployment.name)

        def stopped() -> tuple[bool, Optional[str]]:
            for child in child_processes:
                try:
                    if child.is_running():
                        child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
            logger.info("Deployment %s stopped.", deployment.name)
            deployment.pid = None
            deployment.process_created_at = None
            self.processes.pop(deployment.name, None)
            return True, None

        try:
            root_process = psutil.Process(pid)
            child_processes = root_process.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            child_processes = []

        try:
            for child in child_processes:
                try:
                    child.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
            if process and process.poll() is None:
                process.terminate()
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception as exc:
            logger.warning("Failed graceful terminate for %s pid=%s: %s", deployment.name, pid, exc)
        try:
            if process:
                process.wait(timeout=10)
            else:
                psutil.Process(pid).wait(timeout=10)
            return stopped()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return stopped()
        except (psutil.TimeoutExpired, subprocess.TimeoutExpired) as exc:
            logger.warning("Graceful stop timed out for %s pid=%s: %s", deployment.name, pid, exc)
            try:
                if process:
                    process.kill()
                else:
                    os.killpg(os.getpgid(pid), getattr(signal, "SIGKILL", signal.SIGTERM))
                for child in child_processes:
                    try:
                        child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        pass
            except (ProcessLookupError, psutil.NoSuchProcess):
                return stopped()
            except Exception as kill_error:
                logger.error("Failed to kill deployment %s pid=%s: %s", deployment.name, pid, kill_error)
                return False, str(kill_error)

            try:
                if process:
                    process.wait(timeout=5)
                else:
                    psutil.Process(pid).wait(timeout=5)
                return stopped()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                return stopped()
            except (psutil.TimeoutExpired, subprocess.TimeoutExpired, psutil.AccessDenied) as wait_error:
                logger.error("Deployment %s pid=%s still exists after force-stop: %s", deployment.name, pid, wait_error)
                return False, str(wait_error)
        except psutil.AccessDenied as exc:
            logger.error("Permission denied while stopping deployment %s pid=%s: %s", deployment.name, pid, exc)
            return False, str(exc)

    def load_deployment_env(self, path: Path) -> Dict[str, str]:
        env = {}
        if not path.exists():
            return env
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.strip().startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
        return env

    def run(self) -> None:
        logger.info("Starting manager bot.")
        signal.signal(signal.SIGINT, self._shutdown_handler)
        signal.signal(signal.SIGTERM, self._shutdown_handler)
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        try:
            # Start the bot client so the event loop is available for notifications
            self.app.start()

            # start monitor thread after app started
            self.monitor_thread.start()

            # Block until stop signal; idle keeps the client running.
            idle()
        finally:
            # Shutdown sequence
            self.shutdown_event.set()
            if self.monitor_thread.is_alive():
                self.monitor_thread.join(timeout=2)
            try:
                self.app.stop()
            except Exception:
                logger.exception("Error while stopping manager app")

    def _shutdown_handler(self, signum, frame):
        logger.info("Manager received signal %s, shutting down manager only.", signum)
        # Do not stop managed deployments here; leave them running.
        self.shutdown_event.set()
        logger.info("Exiting manager.")
        sys.exit(0)

    def stop_all(self) -> None:
        logger.info("Stopping all managed deployments.")
        for name, deployment in list(self.store.list().items()):
            if deployment.is_running:
                self.stop_process(deployment)
                self.store.update(deployment)


def main() -> None:
    config = ManagerConfig.load()
    config.deployments_dir.mkdir(parents=True, exist_ok=True)
    store = DeploymentStore(STORE_PATH)
    bot_manager = BotManager(config, store)
    bot_manager.run()


if __name__ == "__main__":
    main()

