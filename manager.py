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
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Optional

import psutil
from dotenv import load_dotenv
from pymongo import AsyncMongoClient
from pyrogram import Client, filters, idle
from pyrogram.errors import RPCError
from pyrogram.types import Message, ReplyParameters

from manager_support import AuditLog, DeploymentOperations, RecoveryBackup

ROOT = Path(__file__).resolve().parent
MANAGER_ENV = ROOT / "manager.env"
STORE_PATH = ROOT / "manager_deployments.json"
SUDO_STORE_PATH = ROOT / "manager_sudoers.json"
BACKUP_STATE_PATH = ROOT / "manager_backup_state.json"
AUDIT_LOG_PATH = ROOT / "manager_audit.jsonl"
DEPLOYED_REFRESH_FALLBACK_NOTICE = (
    "⚡ <b>Activation required:</b> Have the deployed bot owner or an existing sudo user "
    "run <code>/refreshconfig</code> in that deployed bot's private chat. "
    "The stored change could not be applied to the running bot automatically."
)
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
    "COOKIES_PATH",
    "MAINTENANCE_GRACE_MINUTES",
    "USER_QUEUE_LIMIT",
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
        message = re.sub(
            r"(?i)(key|token|api_key)=([^&\s\"']+)", r"\1=[REDACTED]", message
        )
        message = re.sub(
            r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b", "[REDACTED_BOT_TOKEN]", message
        )
        return re.sub(r"mongodb(?:\+srv)?://[^\s\"']+", "[REDACTED_MONGO_URL]", message)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = self.redact(
                "".join(traceback.format_exception(*record.exc_info))
            )
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
    pending_restart: bool = False
    restart_requested_at: Optional[str] = None
    restart_requested_by: Optional[int] = None
    pending_restart_reason: Optional[str] = None
    pending_restart_mode: str = "drain"
    manager_sudoers: list[int] = field(default_factory=list)

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
        values.setdefault("pending_restart_reason", None)
        values.setdefault("pending_restart_mode", "drain")
        values.setdefault("manager_sudoers", [])
        values["manager_sudoers"] = sorted(
            {
                int(value)
                for value in values.get("manager_sudoers", [])
                if str(value).isdigit() and int(value) > 0
            }
        )
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
            if (
                self.process_created_at
                and abs(process.create_time() - self.process_created_at) > 2
            ):
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
            logger.warning(
                "Failed to parse deployment store %s: %s", self.store_path, exc
            )
            self.deployments = {}

    def save(self) -> None:
        with self.lock:
            logger.info(
                "Saving %d deployments to %s", len(self.deployments), self.store_path
            )
            temporary = self.store_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "deployments": {
                            name: deployment.to_dict()
                            for name, deployment in self.deployments.items()
                        }
                    },
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


def resolve_manager_path(value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return str(path.resolve())


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
    for key, default in (
        ("API_URL", values.get("api_url")),
        ("VIDEO_API_URL", values.get("video_api_url")),
    ):
        if default:
            env[key] = default
    if values.get("downloads_path"):
        env["DOWNLOADS_PATH"] = values["downloads_path"]
    if values.get("cookies_path"):
        env["COOKIES_PATH"] = values["cookies_path"]
    if values.get("cookies_url"):
        env["COOKIES_URL"] = values["cookies_url"]
    if values.get("api_key"):
        env["API_KEY"] = values["api_key"]
    if values.get("owner_id"):
        env["OWNER_ID"] = values["owner_id"]
    return env


def handler_errors(func):
    @wraps(func)
    async def wrapper(self, client: Client, message: Message):
        issuer_id = message.from_user.id if message.from_user else None
        deployment = None
        parts = (message.text or "").split(maxsplit=2)
        deployment_commands = {
            "add_bot_sudo",
            "bot_sudo_list",
            "change_bot_owner",
            "cancel_restart",
            "change_database",
            "delete",
            "del_bot_sudo",
            "deploy",
            "logs",
            "reconfigure",
            "restart",
            "status",
            "stop",
        }
        if (
            func.__name__ in deployment_commands
            and len(parts) > 1
            and parts[1].lower() != "all"
        ):
            deployment = normalize_name(parts[1])
        if hasattr(self, "audit"):
            self.audit.record(
                func.__name__,
                issuer_id=issuer_id,
                deployment=deployment,
            )
        try:
            await func(self, client, message)
            if hasattr(self, "audit"):
                self.audit.record(
                    func.__name__,
                    issuer_id=issuer_id,
                    deployment=deployment,
                    result="completed",
                )
        except Exception as exc:
            logger.exception("Handler %s failed: %s", func.__name__, exc)
            if hasattr(self, "audit"):
                self.audit.record(
                    func.__name__,
                    issuer_id=issuer_id,
                    deployment=deployment,
                    result="failed",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            await message.reply_text(
                "❌ I could not complete that request.\n\n"
                "💡 Check the command arguments and try again. If it keeps failing, review the manager logs.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

    return wrapper


def deployment_operation(operation: str):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, client: Client, message: Message):
            parts = (message.text or "").split(maxsplit=2)
            if len(parts) < 2 or parts[1].strip().lower() == "all":
                return await func(self, client, message)
            name = normalize_name(parts[1])
            with self.operations.acquire(name, operation, token=object()) as acquired:
                if not acquired:
                    return await message.reply_text(
                        f"⏳ Deployment <b>{name}</b> is busy with "
                        f"<code>{html.escape(self.operations.current(name) or 'another operation')}</code>.\n\n"
                        "💡 Wait for that operation to finish, then try again.",
                        reply_parameters=ReplyParameters(message_id=message.id),
                    )
                return await func(self, client, message)

        return wrapper

    return decorator


class BotManager:
    def __init__(self, config: ManagerConfig, store: DeploymentStore) -> None:
        self.config = config
        self.store = store
        self.shutdown_event = threading.Event()
        self.monitor_interval = max(5, int(os.getenv("MANAGER_MONITOR_INTERVAL", "20")))
        self.health_stale_after = max(
            30, int(os.getenv("MANAGER_HEALTH_STALE_AFTER", "120"))
        )
        self.health_confirmations = max(
            2, int(os.getenv("MANAGER_HEALTH_CONFIRMATIONS", "2"))
        )
        self.recovery_limit = max(1, int(os.getenv("MANAGER_RECOVERY_LIMIT", "3")))
        self.recovery_window = max(
            60, int(os.getenv("MANAGER_RECOVERY_WINDOW", "3600"))
        )
        self.backup_interval = max(
            3600, int(os.getenv("MANAGER_BACKUP_INTERVAL", "86400"))
        )
        self.processes: Dict[str, subprocess.Popen] = {}
        self.failed_restarts: set[str] = set()
        self.stale_health_counts: Dict[str, int] = {}
        self.recovering: set[str] = set()
        self.restarting: set[str] = set()
        self.sudo_reconciled: set[str] = set()
        self.recovery_guard = threading.Lock()
        self.backup_lock = threading.Lock()
        self.operations = DeploymentOperations()
        self.audit = AuditLog(AUDIT_LOG_PATH)
        self.recovery_backup = RecoveryBackup(
            root=ROOT,
            store=self.store,
            manager_env=MANAGER_ENV,
            store_path=STORE_PATH,
            sudo_store_path=SUDO_STORE_PATH,
            audit_path=AUDIT_LOG_PATH,
            backup_state_path=BACKUP_STATE_PATH,
            load_env=self.load_deployment_env,
        )
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
        self.app.on_message(
            filters.private & filters.command("start") & self.authorized_filter
        )(self.start)
        self.app.on_message(
            filters.private & filters.command("help") & self.authorized_filter
        )(self.help)
        self.app.on_message(
            filters.private & filters.command("newbot") & self.authorized_filter
        )(self.newbot)
        self.app.on_message(
            filters.private
            & filters.command(["reconfigure", "rebuild"])
            & self.authorized_filter
        )(self.reconfigure)
        self.app.on_message(
            filters.private
            & filters.command(["changedb", "switchdb"])
            & self.authorized_filter
        )(self.change_database)
        self.app.on_message(
            filters.private & filters.command("list") & self.authorized_filter
        )(self.list_bots)
        self.app.on_message(
            filters.private & filters.command("status") & self.authorized_filter
        )(self.status)
        self.app.on_message(
            filters.private & filters.command("deploy") & self.authorized_filter
        )(self.deploy)
        self.app.on_message(
            filters.private & filters.command("stop") & self.authorized_filter
        )(self.stop)
        self.app.on_message(
            filters.private & filters.command("delete") & self.authorized_filter
        )(self.delete)
        self.app.on_message(
            filters.private & filters.command("restart") & self.authorized_filter
        )(self.restart)
        self.app.on_message(
            filters.private & filters.command("cancelrestart") & self.authorized_filter
        )(self.cancel_restart)
        self.app.on_message(
            filters.private & filters.command("logs") & self.authorized_filter
        )(self.logs)
        self.app.on_message(
            filters.private & filters.command("sudolist") & self.authorized_filter
        )(self.sudolist)
        self.app.on_message(filters.private & filters.command("addsudo") & owner)(
            self.addsudo
        )
        self.app.on_message(
            filters.private & filters.command(["delsudo", "rmsudo"]) & owner
        )(self.delsudo)
        self.app.on_message(filters.private & filters.command("backup") & owner)(
            self.backup
        )
        self.app.on_message(filters.private & filters.command("broadcast") & owner)(
            self.broadcast
        )
        self.app.on_message(
            filters.private & filters.command("addbotsudo") & self.authorized_filter
        )(self.add_bot_sudo)
        self.app.on_message(
            filters.private
            & filters.command(["changebotowner", "transferbotowner"])
            & self.authorized_filter
        )(self.change_bot_owner)
        self.app.on_message(
            filters.private
            & filters.command(["delbotsudo", "rmbotsudo"])
            & self.authorized_filter
        )(self.del_bot_sudo)
        self.app.on_message(
            filters.private & filters.command("botsudolist") & self.authorized_filter
        )(self.bot_sudo_list)

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
            "🧰 /reconfigure &lt;name&gt; [bot_token] [owner_id] - Reconfigure using the stored token unless a new one is provided.\n"
            "🗄️ /changedb &lt;name&gt; &lt;database_name&gt; - Switch a deployment to another database.\n"
            "📋 /list - Show all deployments.\n"
            "🔎 /status &lt;name&gt; - Show deployment status.\n"
            "▶️ /deploy &lt;name&gt; - Start a stopped deployment.\n"
            "⏹️ /stop &lt;name&gt; - Stop a running deployment.\n"
            "🗑️ /delete &lt;name&gt; - Permanently delete a deployment.\n"
            "🛠️ /restart &lt;name|all&gt; [idle|force] - Restart after complete natural idleness, drain streams for maintenance, or force immediately.\n"
            "✖️ /cancelrestart &lt;name|all&gt; - Cancel queued maintenance restarts.\n"
            "📄 /logs &lt;name&gt; - Retrieve a deployment's full log.\n"
            "👥 /sudolist - View manager owner and sudo users.\n"
            "➕ /addsudo &lt;user_id&gt; - Grant manager access. Owner only.\n"
            "➖ /delsudo &lt;user_id&gt; - Remove manager access. Owner only.\n"
            "💾 /backup - Send a full disaster-recovery backup including databases. Owner only.\n"
            "📣 /broadcast [-user] [-nochat] [-owners] &lt;text&gt; - Broadcast through deployed bots or directly to deployed owners. Owner only.\n"
            "🛡️ /addbotsudo &lt;deployment&gt; &lt;user_id&gt; - Add a deployed-bot sudo user and refresh it live.\n"
            "👑 /changebotowner &lt;deployment&gt; &lt;user_id&gt; [keep|remove] - Change a deployed bot owner.\n"
            "🚫 /delbotsudo &lt;deployment&gt; &lt;user_id&gt; - Remove a deployed-bot sudo user and refresh it live.\n"
            "👥 /botsudolist &lt;deployment&gt; - View a deployed bot's sudo users.\n",
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
            lines.extend(
                f"• <code>{user_id}</code>" for user_id in sorted(self.config.sudoers)
            )
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
            return await message.reply_text(
                "👑 The manager owner already has full access."
            )
        if user_id in self.config.sudoers:
            return await message.reply_text(
                f"🛡️ <code>{user_id}</code> is already a manager sudo user."
            )

        try:
            user = await client.get_users(user_id)
            if user.is_bot:
                return await message.reply_text(
                    "❌ Bot accounts cannot be manager sudo users."
                )
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
            return await message.reply_text(
                "👑 The manager owner cannot be removed from access."
            )
        if user_id not in self.config.sudoers:
            return await message.reply_text(
                f"📭 <code>{user_id}</code> is not a manager sudo user."
            )

        self.config.sudoers.discard(user_id)
        self.authorized_filter.discard(user_id)
        try:
            self.save_sudoers()
        except OSError:
            self.config.sudoers.add(user_id)
            self.authorized_filter.add(user_id)
            raise
        await message.reply_text(
            f"✅ Removed <code>{user_id}</code> from manager sudo users."
        )

    def deployment_database(self, deployment: DeploymentMeta):
        env = self.load_deployment_env(deployment.deployment_path / ".env")
        mongo_url = env.get("MONGO_URL")
        db_name = deployment.db_name or env.get("DB_NAME")
        if not mongo_url or not db_name:
            raise ValueError("Deployment environment is missing MONGO_URL or DB_NAME")
        mongo = AsyncMongoClient(mongo_url, serverSelectionTimeoutMS=12500)
        return mongo, mongo[db_name], env

    @staticmethod
    def deployment_sudo_doc_id(env: dict[str, str]) -> str:
        if env.get("MANAGED_SETUP", "").strip().lower() in {"true", "1", "yes", "on"}:
            deployment_id = (env.get("DEPLOYMENT_ID") or "").strip()
            if deployment_id:
                return f"sudoers:{deployment_id}"
        return "sudoers"

    def update_deployment_owner_env(self, deployment: DeploymentMeta, owner_id: int) -> str:
        env_path = deployment.deployment_path / ".env"
        if not env_path.exists():
            return "\n⚠️ The owner was changed, but the deployment <code>.env</code> file was not found."
        try:
            env_vars = self.load_deployment_env(env_path)
            env_vars["OWNER_ID"] = str(owner_id)
            temporary = env_path.with_name(".env.tmp")
            temporary.write_text(
                "\n".join(f"{key}={value}" for key, value in env_vars.items()),
                encoding="utf-8",
            )
            temporary.replace(env_path)
        except Exception:
            logger.exception("Could not update OWNER_ID in .env for %s", deployment.name)
            return "\n⚠️ The owner was changed, but <code>.env</code> could not be updated."
        return ""

    async def deployed_owner_id(
        self, deployment: DeploymentMeta
    ) -> tuple[Optional[int], str]:
        mongo = None
        try:
            mongo, database, env = self.deployment_database(deployment)
            runtime = await database.cache.find_one({"_id": "runtime_config"}) or {}
            owner_id = int(
                runtime.get("settings", {}).get("OWNER_ID") or env.get("OWNER_ID") or 0
            )
            return (owner_id if owner_id > 0 else None), "runtime config"
        except Exception as exc:
            logger.warning(
                "Could not resolve owner for %s from database: %s", deployment.name, exc
            )
            env = self.load_deployment_env(deployment.deployment_path / ".env")
            try:
                owner_id = int(env.get("OWNER_ID") or 0)
            except (TypeError, ValueError):
                owner_id = 0
            return (owner_id if owner_id > 0 else None), "deployment .env"
        finally:
            if mongo is not None:
                await mongo.close()

    async def request_runtime_control(
        self,
        deployment: DeploymentMeta,
        operation: str,
        payload: Optional[dict] = None,
    ) -> tuple[bool, str, dict]:
        if not self.process_matches(deployment):
            return False, "the deployment is not running", {}
        health = self.read_health(deployment)
        try:
            healthy = (
                health
                and health.get("state") == "healthy"
                and time.time() - float(health.get("timestamp", 0))
                <= self.health_stale_after
            )
        except (TypeError, ValueError):
            healthy = False
        if not healthy:
            return (
                False,
                "the deployment is not currently reporting a healthy heartbeat",
                {},
            )

        request_id = uuid.uuid4().hex
        request_path = (
            deployment.deployment_path / f".runtime-control-{request_id}.json"
        )
        result_path = (
            deployment.deployment_path / f".runtime-control-result-{request_id}.json"
        )
        temporary = request_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "request_id": request_id,
                        "operation": operation,
                        "payload": payload or {},
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=True,
                ),
                encoding="utf-8",
            )
            temporary.replace(request_path)
        except OSError as exc:
            logger.warning(
                "Could not request runtime control operation %s for %s: %s",
                operation,
                deployment.name,
                exc,
            )
            return (
                False,
                "the manager could not reach the deployment's runtime control file",
                {},
            )

        deadline = time.monotonic() + max(20, self.monitor_interval + 10)
        try:
            while time.monotonic() < deadline:
                if result_path.exists():
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    if result.get("success"):
                        data = result.get("data") or {}
                        warnings = data.get("warnings") or []
                        if warnings:
                            return (
                                True,
                                "the live update applied, but some command menus could not be updated",
                                data,
                            )
                        return True, "the live update was applied automatically", data
                    logger.warning(
                        "Deployment %s rejected runtime control operation %s: %s",
                        deployment.name,
                        operation,
                        result.get("error") or "unknown error",
                    )
                    return False, "the deployed bot could not apply the live update", {}
                if not self.process_matches(deployment):
                    return (
                        False,
                        "the deployment stopped before confirming the live update",
                        {},
                    )
                await asyncio.sleep(1)
            return False, "the deployed bot did not confirm the live update in time", {}
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(
                "Could not read runtime control operation %s result for %s: %s",
                operation,
                deployment.name,
                exc,
            )
            return (
                False,
                "the manager could not read the deployed bot's update result",
                {},
            )
        finally:
            request_path.unlink(missing_ok=True)
            result_path.unlink(missing_ok=True)

    @handler_errors
    async def broadcast(self, client: Client, message: Message) -> None:
        parts = message.text.split(maxsplit=1)
        command_text = parts[1].strip() if len(parts) > 1 else ""
        include_users = "-user" in command_text.split()
        exclude_groups = "-nochat" in command_text.split()
        owners_only = "-owners" in command_text.split()
        text = re.sub(r"(?<!\S)-(?:user|nochat|owners)(?!\S)", "", command_text).strip()
        if message.reply_to_message:
            text = (
                message.reply_to_message.text
                or message.reply_to_message.caption
                or text
            )
        if not text:
            return await message.reply_text(
                "📣 <b>Broadcast text is required.</b>\n\n"
                "Reply to a text message or use:\n"
                "<code>/broadcast [-user] [-nochat] [-owners] your message</code>",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        if len(text) > 4096:
            return await message.reply_text(
                "❌ <b>The broadcast is too long.</b>\n\n"
                "💡 Keep the message within Telegram's 4096-character text limit.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        status = await message.reply_text(
            (
                "📣 <b>Dispatching owner broadcast...</b>\n\n"
                "👑 I will message the owners recorded for the deployed bots."
                if owners_only
                else "📣 <b>Dispatching broadcast...</b>\n\n"
                "⚡ Each healthy bot will begin delivering independently."
            ),
            reply_parameters=ReplyParameters(message_id=message.id),
        )
        summary = await self.dispatch_broadcast_all(
            text=text,
            include_users=include_users,
            exclude_groups=exclude_groups,
            owners_only=owners_only,
            requested_by=message.from_user.id,
        )
        await status.edit_text(summary)

    async def dispatch_broadcast_all(
        self,
        *,
        text: str,
        include_users: bool,
        exclude_groups: bool,
        requested_by: int,
        owners_only: bool = False,
    ) -> str:
        all_deployments = list(self.store.list().values())
        if owners_only:
            if not all_deployments:
                return "📭 No deployments found."
            return await self.dispatch_broadcast_to_deployed_owners(
                all_deployments,
                text=text,
                requested_by=requested_by,
            )
        deployments = [
            deployment
            for deployment in self.store.list().values()
            if deployment.desired_running and not deployment.intentionally_stopped
        ]
        if not deployments:
            return "📭 No deployed bots are currently expected to be running."
        payload = {
            "text": text,
            "include_users": include_users,
            "exclude_groups": exclude_groups,
            "requested_by": requested_by,
        }
        results = await asyncio.gather(
            *(
                self.request_runtime_control(deployment, "broadcast_text", payload)
                for deployment in deployments
            )
        )
        accepted = []
        rejected = []
        recipients = 0
        for deployment, (success, detail, data) in zip(deployments, results):
            if success:
                accepted.append(deployment.name)
                recipients += int(data.get("recipient_count", 0) or 0)
            else:
                rejected.append(f"{deployment.name}: {detail}")
        summary = (
            "✅ <b>Broadcast dispatched.</b>\n\n"
            f"🤖 Accepted by: <code>{len(accepted)}/{len(deployments)}</code> deployed bots\n"
            f"📬 Combined recipients queued: <code>{recipients}</code>\n"
            f"👤 Include users: <code>{'yes' if include_users else 'no'}</code>\n"
            f"👥 Include groups: <code>{'no' if exclude_groups else 'yes'}</code>\n\n"
            "⚡ Delivery continues independently on each deployed bot."
        )
        if rejected:
            summary += "\n\n⚠️ <b>Not started</b>\n" + "\n".join(
                f"• <code>{html.escape(item)}</code>" for item in rejected[:15]
            )
        self.audit.record(
            "broadcast_all",
            issuer_id=requested_by,
            deployment="all",
            result="dispatched",
            detail=f"accepted={len(accepted)} rejected={len(rejected)} recipients={recipients}",
        )
        return summary

    async def dispatch_broadcast_to_deployed_owners(
        self,
        deployments: list[DeploymentMeta],
        *,
        text: str,
        requested_by: int,
    ) -> str:
        owner_sources = await asyncio.gather(
            *(self.deployed_owner_id(deployment) for deployment in deployments)
        )
        owner_map: dict[int, list[str]] = {}
        missing = []
        for deployment, (owner_id, source) in zip(deployments, owner_sources):
            if owner_id:
                owner_map.setdefault(owner_id, []).append(deployment.name)
            else:
                missing.append(f"{deployment.name}: no owner configured in {source}")

        delivered = []
        failed = []
        for owner_id, names in owner_map.items():
            owner_text = (
                "📣 <b>Manager broadcast for your deployed bot"
                + ("s" if len(names) > 1 else "")
                + "</b>\n\n"
                f"{text}\n\n"
                "🤖 Related deployment"
                + ("s" if len(names) > 1 else "")
                + f": <code>{html.escape(', '.join(sorted(names)))}</code>"
            )
            try:
                await self.app.send_message(owner_id, owner_text)
                delivered.append(owner_id)
            except Exception as exc:
                logger.warning(
                    "Could not deliver owner broadcast to %s: %s", owner_id, exc
                )
                failed.append(f"{owner_id}: {type(exc).__name__}")

        summary = (
            "✅ <b>Owner broadcast processed.</b>\n\n"
            f"👑 Unique owners found: <code>{len(owner_map)}</code>\n"
            f"📬 Delivered: <code>{len(delivered)}</code>\n"
            f"⚠️ Failed: <code>{len(failed)}</code>\n"
            f"📭 Missing owner config: <code>{len(missing)}</code>"
        )
        warnings = failed + missing
        if warnings:
            summary += "\n\n⚠️ <b>Attention</b>\n" + "\n".join(
                f"• <code>{html.escape(item)}</code>" for item in warnings[:15]
            )
        self.audit.record(
            "broadcast_owners",
            issuer_id=requested_by,
            deployment="all",
            result="dispatched",
            detail=f"owners={len(owner_map)} delivered={len(delivered)} failed={len(failed)} missing={len(missing)}",
        )
        return summary

    async def refresh_deployment_sudoers(
        self,
        deployment: DeploymentMeta,
    ) -> tuple[bool, str]:
        success, detail, _ = await self.request_runtime_control(
            deployment, "refresh_sudoers"
        )
        return success, detail

    async def reconcile_manager_sudoers(
        self,
        deployment: DeploymentMeta,
    ) -> tuple[bool, str]:
        desired = sorted(
            {
                int(value)
                for value in deployment.manager_sudoers
                if str(value).isdigit() and int(value) > 0
            }
        )
        if desired != deployment.manager_sudoers:
            deployment.manager_sudoers = desired
            self.store.update(deployment)
        if not desired:
            return True, "no manager-managed sudo users to reconcile"

        mongo = None
        try:
            mongo, database, env = self.deployment_database(deployment)
            await mongo.admin.command("ping")
            result = await database.cache.update_one(
                {"_id": self.deployment_sudo_doc_id(env)},
                {"$addToSet": {"user_ids": {"$each": desired}}},
                upsert=True,
            )
        except Exception as exc:
            logger.warning(
                "Could not reconcile manager sudoers for %s: %s", deployment.name, exc
            )
            return False, "the deployment database could not be updated"
        finally:
            if mongo is not None:
                await mongo.close()

        refreshed, refresh_detail = await self.refresh_deployment_sudoers(deployment)
        changed = bool(result.modified_count or result.upserted_id)
        if refreshed:
            return True, (
                f"{len(desired)} manager-managed sudo user(s) verified"
                + (" and restored" if changed else "")
            )
        return False, (
            f"{len(desired)} manager-managed sudo user(s) were saved in Mongo, "
            f"but live refresh was unavailable because {refresh_detail}"
        )

    async def update_running_deployment_sudoer(
        self,
        deployment: DeploymentMeta,
        operation: str,
        user_id: int,
    ) -> tuple[bool, str, dict]:
        return await self.request_runtime_control(
            deployment,
            operation,
            {"user_id": user_id},
        )

    def parse_bot_sudo_args(
        self,
        message: Message,
        command: str,
    ) -> tuple[Optional[DeploymentMeta], Optional[int], Optional[str]]:
        args = message.text.split(maxsplit=2)
        if len(args) < 3 or not args[2].strip().isdigit() or int(args[2]) <= 0:
            return (
                None,
                None,
                (
                    f"Usage: <code>/{command} &lt;deployment&gt; &lt;telegram_user_id&gt;</code>"
                ),
            )
        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return (
                None,
                None,
                (
                    f"Deployment <b>{name}</b> was not found. Use <code>/list</code> to check names."
                ),
            )
        return deployment, int(args[2]), None

    @handler_errors
    async def add_bot_sudo(self, client: Client, message: Message) -> None:
        deployment, user_id, error = self.parse_bot_sudo_args(message, "addbotsudo")
        if error:
            return await message.reply_text(
                f"❌ {error}",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        try:
            user = await client.get_users(user_id)
            if user.is_bot:
                return await message.reply_text("❌ Bot accounts cannot be sudo users.")
        except Exception:
            user = None

        status = await message.reply_text(
            f"🛡️ Adding <code>{user_id}</code> to <b>{deployment.name}</b>...",
            reply_parameters=ReplyParameters(message_id=message.id),
        )

        (
            runtime_success,
            runtime_detail,
            runtime_data,
        ) = await self.update_running_deployment_sudoer(
            deployment,
            "add_sudoer",
            user_id,
        )
        if runtime_success:
            reconciled, reconcile_detail = await self.reconcile_manager_sudoers(
                deployment
            )
            label = (
                f"<b>{html.escape(user.first_name or str(user_id))}</b> (<code>{user_id}</code>)"
                if user
                else f"<code>{user_id}</code>"
            )
            persistence = (
                f"🧷 Manager persistence check: {reconcile_detail}."
                if reconciled
                else f"⚠️ Manager persistence check failed: {reconcile_detail}."
            )
            await status.edit_text(
                f"✅ {label} was added to <b>{deployment.name}</b>.\n\n"
                "💾 Saved through the running deployed bot's active database, so it will persist across restarts.\n"
                f"⚡ {runtime_detail}.\n"
                f"{persistence}"
            )
            return

        mongo = None
        try:
            mongo, database, env = self.deployment_database(deployment)
            await mongo.admin.command("ping")
            result = await database.cache.update_one(
                {"_id": self.deployment_sudo_doc_id(env)},
                {"$addToSet": {"user_ids": user_id}},
                upsert=True,
            )
        except Exception:
            logger.exception(
                "Could not add deployed-bot sudo user for %s", deployment.name
            )
            return await status.edit_text(
                "❌ I could not update that deployed bot's sudo list.\n\n"
                "💡 Check its database configuration and connection, then try again."
            )
        finally:
            if mongo is not None:
                await mongo.close()

        label = (
            f"<b>{html.escape(user.first_name or str(user_id))}</b> (<code>{user_id}</code>)"
            if user
            else f"<code>{user_id}</code>"
        )
        changed = bool(result.modified_count or result.upserted_id)
        state = "was added" if changed else "already had sudo access"
        if changed:
            await status.edit_text(
                f"✅ {label} {state} on <b>{deployment.name}</b>.\n\n"
                "⚡ Applying the updated access to the running bot..."
            )
            refreshed, refresh_detail = await self.refresh_deployment_sudoers(
                deployment
            )
        else:
            refreshed, refresh_detail = True, "no live refresh was needed"
        activation = (
            f"⚡ {refresh_detail}."
            if refreshed
            else f"⚠️ Automatic refresh was unavailable because {refresh_detail}.\n\n{DEPLOYED_REFRESH_FALLBACK_NOTICE}"
        )
        await status.edit_text(
            f"✅ {label} {state} on <b>{deployment.name}</b>.\n\n{activation}"
        )

    @handler_errors
    async def change_bot_owner(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=3)
        if len(args) < 3 or not args[2].strip().isdigit() or int(args[2]) <= 0:
            return await message.reply_text(
                "👑 Usage: <code>/changebotowner &lt;deployment&gt; &lt;new_owner_user_id&gt; [keep|remove]</code>\n\n"
                "💡 <code>keep</code> keeps the previous owner as sudo. <code>remove</code> removes their access. Default: <code>keep</code>.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> was not found. Use <code>/list</code> to check names.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        new_owner = int(args[2])
        action = args[3].strip().lower() if len(args) > 3 else "keep"
        if action not in {"keep", "keepsudo", "keep_sudo", "yes", "remove", "removesudo", "remove_sudo", "no"}:
            return await message.reply_text(
                "❌ The final option must be <code>keep</code> or <code>remove</code>.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        keep_previous_sudo = action in {"keep", "keepsudo", "keep_sudo", "yes"}

        try:
            user = await client.get_users(new_owner)
            if user.is_bot:
                return await message.reply_text(
                    "❌ A bot account cannot become the deployed bot owner."
                )
            owner_label = f"<b>{html.escape(user.first_name or str(new_owner))}</b> (<code>{new_owner}</code>)"
        except Exception:
            owner_label = f"<code>{new_owner}</code>"

        status = await message.reply_text(
            f"👑 Changing owner for <b>{deployment.name}</b> to {owner_label}...",
            reply_parameters=ReplyParameters(message_id=message.id),
        )
        previous_owner, previous_source = await self.deployed_owner_id(deployment)

        runtime_success, runtime_detail, runtime_data = await self.request_runtime_control(
            deployment,
            "change_owner",
            {"user_id": new_owner, "keep_previous_sudo": keep_previous_sudo},
        )

        if runtime_success and int(runtime_data.get("effective_owner") or 0) == new_owner:
            env_warning = self.update_deployment_owner_env(deployment, new_owner)
            previous_owner = int(runtime_data.get("previous_owner") or previous_owner or 0) or None
            if previous_owner and previous_owner != new_owner:
                if keep_previous_sudo and previous_owner not in deployment.manager_sudoers:
                    deployment.manager_sudoers.append(previous_owner)
                    deployment.manager_sudoers.sort()
                    self.store.update(deployment)
                elif not keep_previous_sudo and previous_owner in deployment.manager_sudoers:
                    deployment.manager_sudoers = [value for value in deployment.manager_sudoers if value != previous_owner]
                    self.store.update(deployment)
            access = "Previous owner kept as sudo." if keep_previous_sudo else "Previous owner access removed."
            warnings = runtime_data.get("warnings") or []
            warning_text = ""
            if warnings:
                warning_text = "\n⚠️ Menu refresh warnings: " + html.escape(", ".join(str(item) for item in warnings[:3]))
            return await status.edit_text(
                f"✅ Owner changed for <b>{deployment.name}</b>.\n\n"
                f"👑 New owner: {owner_label}\n"
                f"🔁 {access}\n"
                f"⚡ Applied live through the running deployed bot. {html.escape(runtime_detail)}."
                f"{warning_text}{env_warning}"
            )
        if runtime_success:
            runtime_detail = (
                "the deployed bot accepted the request but did not confirm the new owner as active"
            )

        mongo = None
        try:
            mongo, database, env = self.deployment_database(deployment)
            await mongo.admin.command("ping")
            sudo_doc_id = self.deployment_sudo_doc_id(env)
            if previous_owner is None:
                runtime = await database.cache.find_one({"_id": "runtime_config"}) or {}
                previous_owner = int(runtime.get("settings", {}).get("OWNER_ID") or env.get("OWNER_ID") or 0) or None
            await database.cache.update_one(
                {"_id": "runtime_config"},
                {"$set": {"settings.OWNER_ID": new_owner}},
                upsert=True,
            )
            await database.cache.update_one(
                {"_id": sudo_doc_id},
                {"$addToSet": {"user_ids": new_owner}},
                upsert=True,
            )
            if previous_owner and previous_owner != new_owner:
                if keep_previous_sudo:
                    await database.cache.update_one(
                        {"_id": sudo_doc_id},
                        {"$addToSet": {"user_ids": previous_owner}},
                        upsert=True,
                    )
                    if previous_owner not in deployment.manager_sudoers:
                        deployment.manager_sudoers.append(previous_owner)
                        deployment.manager_sudoers.sort()
                        self.store.update(deployment)
                else:
                    await database.cache.update_one(
                        {"_id": sudo_doc_id},
                        {"$pull": {"user_ids": previous_owner}},
                    )
                    if previous_owner in deployment.manager_sudoers:
                        deployment.manager_sudoers = [value for value in deployment.manager_sudoers if value != previous_owner]
                        self.store.update(deployment)
        except Exception:
            logger.exception("Could not change deployed-bot owner for %s", deployment.name)
            return await status.edit_text(
                "❌ I could not change that deployed bot owner.\n\n"
                "💡 Check the deployment database configuration and connection, then try again."
            )
        finally:
            if mongo is not None:
                await mongo.close()

        env_warning = self.update_deployment_owner_env(deployment, new_owner)
        saved_target = "Mongo and <code>.env</code>" if not env_warning else "Mongo"
        activation = (
            f"⚠️ Saved to {saved_target}, but it could not be applied live because "
            f"{html.escape(runtime_detail)}. Restart the deployment or run <code>/refreshconfig</code> from an existing owner/sudo session."
        )
        access = "Previous owner kept as sudo." if keep_previous_sudo else "Previous owner access removed."
        await status.edit_text(
            f"✅ Owner change saved for <b>{deployment.name}</b>.\n\n"
            f"👑 New owner: {owner_label}\n"
            f"🔁 {access}\n"
            f"🧭 Previous owner source: <code>{html.escape(previous_source)}</code>\n\n"
            f"{activation}{env_warning}"
        )
    @handler_errors
    async def del_bot_sudo(self, client: Client, message: Message) -> None:
        deployment, user_id, error = self.parse_bot_sudo_args(message, "delbotsudo")
        if error:
            return await message.reply_text(
                f"❌ {error}",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        status = await message.reply_text(
            f"🚫 Removing <code>{user_id}</code> from <b>{deployment.name}</b>...",
            reply_parameters=ReplyParameters(message_id=message.id),
        )

        (
            runtime_success,
            runtime_detail,
            _,
        ) = await self.update_running_deployment_sudoer(
            deployment,
            "del_sudoer",
            user_id,
        )
        if runtime_success:
            if user_id in deployment.manager_sudoers:
                deployment.manager_sudoers = [
                    value for value in deployment.manager_sudoers if value != user_id
                ]
                self.store.update(deployment)
            await status.edit_text(
                f"✅ <code>{user_id}</code> was removed from <b>{deployment.name}</b>.\n\n"
                "💾 Saved through the running deployed bot's active database, so it will persist across restarts.\n"
                f"⚡ {runtime_detail}."
            )
            return

        mongo = None
        try:
            mongo, database, env = self.deployment_database(deployment)
            await mongo.admin.command("ping")
            runtime = await database.cache.find_one({"_id": "runtime_config"}) or {}
            owner_id = int(
                runtime.get("settings", {}).get("OWNER_ID") or env.get("OWNER_ID") or 0
            )
            if user_id == owner_id:
                return await status.edit_text(
                    "👑 The deployed bot owner cannot be removed from sudo access.\n\n"
                    "💡 Use that deployed bot's <code>/changeowner</code> command first."
                )
            result = await database.cache.update_one(
                {"_id": self.deployment_sudo_doc_id(env)},
                {"$pull": {"user_ids": user_id}},
            )
        except Exception:
            logger.exception(
                "Could not remove deployed-bot sudo user for %s", deployment.name
            )
            return await status.edit_text(
                "❌ I could not update that deployed bot's sudo list.\n\n"
                "💡 Check its database configuration and connection, then try again."
            )
        finally:
            if mongo is not None:
                await mongo.close()

        state = "was removed" if result.modified_count else "was not in the sudo list"
        if user_id in deployment.manager_sudoers:
            deployment.manager_sudoers = [
                value for value in deployment.manager_sudoers if value != user_id
            ]
            self.store.update(deployment)
        if result.modified_count:
            await status.edit_text(
                f"✅ <code>{user_id}</code> {state} on <b>{deployment.name}</b>.\n\n"
                "⚡ Applying the updated access to the running bot..."
            )
            refreshed, refresh_detail = await self.refresh_deployment_sudoers(
                deployment
            )
        else:
            refreshed, refresh_detail = True, "no live refresh was needed"
        activation = (
            f"⚡ {refresh_detail}."
            if refreshed
            else f"⚠️ Automatic refresh was unavailable because {refresh_detail}.\n\n{DEPLOYED_REFRESH_FALLBACK_NOTICE}"
        )
        await status.edit_text(
            f"✅ <code>{user_id}</code> {state} on <b>{deployment.name}</b>.\n\n"
            f"{activation}"
        )

    @handler_errors
    async def bot_sudo_list(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text(
                "👥 Usage: <code>/botsudolist &lt;deployment&gt;</code>",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> was not found.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        status = await message.reply_text(
            f"👥 Loading sudo users for <b>{name}</b>...",
            reply_parameters=ReplyParameters(message_id=message.id),
        )
        reconcile_note = ""
        if deployment.manager_sudoers:
            await status.edit_text(
                f"🧷 Verifying manager-managed sudo users for <b>{name}</b>..."
            )
            reconciled, reconcile_detail = await self.reconcile_manager_sudoers(
                deployment
            )
            reconcile_note = (
                f"\n🧷 Manager-managed sudoers: <code>{html.escape(reconcile_detail)}</code>"
                if reconciled
                else f"\n⚠️ Manager-managed sudoers: <code>{html.escape(reconcile_detail)}</code>"
            )
            await status.edit_text(f"👥 Loading sudo users for <b>{name}</b>...")
        mongo = None
        try:
            mongo, database, env = self.deployment_database(deployment)
            await mongo.admin.command("ping")
            sudo_doc_id = self.deployment_sudo_doc_id(env)
            sudo_doc = await database.cache.find_one({"_id": sudo_doc_id}) or {}
            if not sudo_doc and sudo_doc_id != "sudoers":
                sudo_doc = await database.cache.find_one({"_id": "sudoers"}) or {}
            runtime = await database.cache.find_one({"_id": "runtime_config"}) or {}
            owner_id = int(
                runtime.get("settings", {}).get("OWNER_ID") or env.get("OWNER_ID") or 0
            )
            sudoers = sorted(
                {
                    int(value)
                    for value in sudo_doc.get("user_ids", [])
                    if str(value).isdigit() and int(value) > 0
                }
            )
        except Exception:
            logger.exception(
                "Could not read deployed-bot sudo users for %s", deployment.name
            )
            return await status.edit_text(
                "❌ I could not read that deployed bot's sudo list.\n\n"
                "💡 Check its database configuration and connection, then try again."
            )
        finally:
            if mongo is not None:
                await mongo.close()

        lines = [f"<b>👥 Sudo users for {html.escape(name)}</b>"]
        if owner_id:
            lines.append(f"\n👑 Owner: <code>{owner_id}</code>")
        additional = [user_id for user_id in sudoers if user_id != owner_id]
        if additional:
            lines.append("\n<b>🛡️ Additional sudo users</b>")
            lines.extend(f"• <code>{user_id}</code>" for user_id in additional)
        else:
            lines.append("\n📭 No additional sudo users.")
        if deployment.manager_sudoers:
            lines.append(
                "\n<b>🧷 Manager-managed</b>\n"
                + "\n".join(
                    f"• <code>{user_id}</code>"
                    for user_id in deployment.manager_sudoers
                )
            )
        if reconcile_note:
            lines.append(reconcile_note)
        await status.edit_text("\n".join(lines))

    async def create_backup_archive(self) -> Path:
        return await self.recovery_backup.create_archive()

    def read_backup_state(self) -> dict:
        try:
            return json.loads(BACKUP_STATE_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return {}

    def save_backup_state(self, *, success: bool, error: str = "") -> None:
        state = self.read_backup_state()
        state["last_attempt"] = datetime.now(timezone.utc).isoformat()
        if success:
            state["last_success"] = state["last_attempt"]
            state.pop("last_error", None)
        elif error:
            state["last_error"] = error[:500]
        temporary = BACKUP_STATE_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
        temporary.replace(BACKUP_STATE_PATH)

    async def send_backup(self, *, scheduled: bool) -> tuple[bool, str]:
        if not self.backup_lock.acquire(blocking=False):
            return False, "A backup is already in progress."
        archive_path = None
        try:
            archive_path = await self.create_backup_archive()
            caption = (
                "💾 <b>Daily manager recovery backup</b>\n\n"
                if scheduled
                else "💾 <b>Manager recovery backup</b>\n\n"
            )
            caption += (
                f"Deployments: <code>{len(self.store.list())}</code>\n"
                "⚠️ Contains credentials, Telegram sessions, and database data. Store it securely.\n"
                "ℹ️ Includes recovery instructions. Downloads, logs, and caches are excluded."
            )
            if self.recovery_backup.last_database_errors:
                caption += (
                    "\n\n🚨 <b>Incomplete database export:</b> "
                    f"<code>{len(self.recovery_backup.last_database_errors)}</code> deployment database(s) failed. "
                    "Check the archive manifest and retry the backup."
                )
            for attempt in range(1, 4):
                try:
                    await self.app.send_document(
                        self.config.owner_id,
                        str(archive_path),
                        caption=caption,
                    )
                    break
                except Exception:
                    if attempt >= 3:
                        raise
                    await asyncio.sleep(attempt * 3)
            self.save_backup_state(success=True)
            logger.info("Manager recovery backup sent to owner.")
            if self.recovery_backup.last_database_errors:
                return True, (
                    f"{len(self.recovery_backup.last_database_errors)} deployment database export(s) failed. "
                    "Check the archive manifest and retry."
                )
            return True, ""
        except Exception as exc:
            logger.exception("Could not create or send manager recovery backup")
            try:
                self.save_backup_state(
                    success=False, error=f"{type(exc).__name__}: {exc}"
                )
            except OSError:
                logger.exception("Could not persist backup failure state")
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            if archive_path:
                archive_path.unlink(missing_ok=True)
            self.backup_lock.release()

    @handler_errors
    async def backup(self, client: Client, message: Message) -> None:
        status = await message.reply_text(
            "💾 Preparing recovery backup...",
            reply_parameters=ReplyParameters(message_id=message.id),
        )
        success, error = await self.send_backup(scheduled=False)
        if success:
            if error:
                return await status.edit_text(
                    "⚠️ Recovery backup sent, but it is incomplete.\n\n"
                    f"Reason: <code>{html.escape(error)}</code>"
                )
            return await status.edit_text(
                "✅ Full disaster-recovery backup sent to the manager owner."
            )
        await status.edit_text(
            "❌ I could not send the recovery backup.\n\n"
            f"Reason: <code>{html.escape(error)}</code>"
        )

    @handler_errors
    async def list_bots(self, client: Client, message: Message) -> None:
        if not self.store.list():
            return await message.reply_text(
                "📭 No deployments found.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        lines = ["<b>📋 Deployments</b>"]
        for name, deployment in self.store.list().items():
            state, health, _ = self.deployment_health(deployment)
            status = self.format_health_state(state)
            pending = (
                " — <code>💤 passive idle restart queued</code>"
                if deployment.pending_restart
                and deployment.pending_restart_mode == "idle"
                else (
                    " — <code>🛠️ maintenance restart queued</code>"
                    if deployment.pending_restart
                    else ""
                )
            )
            streams = int((health or {}).get("active_voice_chats", 0) or 0)
            stream_text = f" — <code>🎵 {streams}</code>" if streams else ""
            operation = self.operations.current(name)
            operation_text = f" — <code>⚙️ {operation}</code>" if operation else ""
            lines.append(
                f"<b>{deployment.username}</b> ({name}) — <code>{status}</code>"
                f"{stream_text}{pending}{operation_text}"
            )
        await message.reply_text(
            "\n".join(lines), reply_parameters=ReplyParameters(message_id=message.id)
        )

    @handler_errors
    async def status(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text(
                "🔎 Usage: /status &lt;name&gt;",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> was not found.\n\n💡 Use /list to check registered names.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

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
            f"Path: <code>{deployment.deployment_path}</code>\n"
            f"Manager-managed sudoers: <code>{len(deployment.manager_sudoers)}</code>"
        )
        if health:
            try:
                age_text = (
                    f"{max(0, int(time.time() - float(health.get('timestamp', 0))))}s"
                )
            except (TypeError, ValueError):
                age_text = "unavailable"
            text += (
                f"\nHeartbeat age: <code>{age_text}</code>"
                f"\nEvent-loop delay: <code>{health.get('event_loop_delay', 0)}s</code>"
                f"\nActive calls: <code>{health.get('active_voice_chats', 0)}</code>"
                f"\nAssistants online: <code>{health.get('assistants_online', 0)}</code>"
                f"\nPlayback operations: <code>{html.escape(str(health.get('playback_operations', {})))[:400]}</code>"
                f"\nPlayback failures: <code>{html.escape(str(health.get('playback_failures', {})))[:400]}</code>"
                f"\nSaved maintenance requests: <code>{health.get('maintenance_queued_requests', 0)}</code>"
                f"\nLive queue items: <code>{health.get('live_queued_requests', 0)}</code>"
                f"\nPreparing play requests: <code>{health.get('active_play_requests', 0)}</code>"
                f"\nBroadcast active: <code>{'yes' if health.get('broadcast_active') else 'no'}</code>"
                f"\nMaintenance grace remaining: <code>{health.get('maintenance_grace_remaining', 'not pending')}</code>"
            )
        if deployment.pid and self.process_matches(deployment):
            try:
                process = psutil.Process(deployment.pid)
                text += (
                    f"\nCPU: <code>{process.cpu_percent(interval=0.1):.1f}%</code>"
                    f"\nMemory: <code>{process.memory_info().rss / 1024**2:.1f} MB</code>"
                    f"\nChild processes: <code>{len(process.children(recursive=True))}</code>"
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        operation = self.operations.current(name)
        if operation:
            text += f"\nManager operation: <code>{html.escape(operation)}</code>"
        if reason:
            text += f"\nReason: <code>{html.escape(reason)}</code>"
        if deployment.last_failure:
            text += (
                f"\nLast failure: <code>{html.escape(deployment.last_failure)}</code>"
            )
        if deployment.pending_restart:
            text += (
                "\nRestart: <code>"
                + (
                    "passively waiting for complete natural idleness; playback remains unrestricted"
                    if deployment.pending_restart_mode == "idle"
                    else "maintenance restart queued until active streams finish"
                )
                + "</code>"
                f"\nRequested: <code>{deployment.restart_requested_at or 'unknown'}</code>"
            )
            if deployment.pending_restart_reason:
                text += (
                    "\nRestart reason: "
                    f"<code>{html.escape(deployment.pending_restart_reason)}</code>"
                )
        await message.reply_text(
            text, reply_parameters=ReplyParameters(message_id=message.id)
        )

    @handler_errors
    @deployment_operation("deploy")
    async def deploy(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text(
                "▶️ Usage: /deploy &lt;name&gt;",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> was not found.\n\n💡 Use /list to check registered names.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
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

        status = await message.reply_text(
            f"🚀 Starting deployment <b>{name}</b>...",
            reply_parameters=ReplyParameters(message_id=message.id),
        )
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
    @deployment_operation("stop")
    async def stop(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text(
                "⏹️ Usage: /stop &lt;name&gt;",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> was not found.\n\n💡 Use /list to check registered names.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        if not deployment.is_running:
            deployment.desired_running = False
            deployment.intentionally_stopped = True
            self.clear_pending_restart(deployment)
            self.stale_health_counts.pop(name, None)
            self.store.update(deployment)
            return await message.reply_text(
                f"⚫ Deployment <b>{name}</b> is already stopped.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        deployment.desired_running = False
        deployment.intentionally_stopped = True
        self.clear_pending_restart(deployment)
        self.stale_health_counts.pop(name, None)
        self.store.update(deployment)
        stopped, error = self.stop_process(deployment)
        if stopped:
            self.store.update(deployment)
            await message.reply_text(
                f"✅ Deployment <b>{name}</b> stopped.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
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
    @deployment_operation("delete")
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
            await status.edit_text(
                f"⏹️ Stopping deployment <b>{name}</b> before deletion..."
            )
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
        args = message.text.split(maxsplit=2)
        if len(args) < 2:
            return await message.reply_text(
                "🔄 Usage: <code>/restart &lt;name|all&gt; [idle|force]</code>\n\n"
                "💡 Use <code>/list</code> to check registered deployment names.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        option = args[2].strip().lower() if len(args) > 2 else ""
        force = option == "force"
        idle = option == "idle"
        if option and not force and not idle:
            return await message.reply_text(
                "❌ Unknown restart option.\n\n"
                "💡 Use <code>idle</code> for a passive unrestricted restart, or "
                "<code>force</code> only for immediate emergency maintenance.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        if args[1].strip().lower() == "all":
            return await self.restart_all(message, force=force, idle=idle)

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> was not found.\n\n"
                "💡 Use <code>/list</code> to check registered deployment names.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        status = await message.reply_text(
            (
                f"🔎 Checking whether deployment <b>{name}</b> is completely idle..."
                if idle
                else f"🔎 Checking active streams for deployment <b>{name}</b>..."
            ),
            reply_parameters=ReplyParameters(message_id=message.id),
        )
        result, detail = await self.request_restart(
            deployment,
            message.from_user.id,
            status,
            force=force,
            idle=idle,
        )
        if result == "waiting":
            if idle:
                return await status.edit_text(
                    f"💤 Passive restart queued for deployment <b>{name}</b>.\n\n"
                    f"{detail}\n\n"
                    "▶️ Playback and queueing remain fully available.\n"
                    "🕰️ The restart will happen only when there are no streams, queues, "
                    "saved requests, or playback operations.\n"
                    f"✖️ Use <code>/cancelrestart {name}</code> to cancel it."
                )
            return await status.edit_text(
                f"🛠️ Maintenance restart for deployment <b>{name}</b> is waiting.\n\n"
                f"🎵 {detail}\n"
                "▶️ Existing streams and their next queued tracks may continue during the configured grace period.\n"
                "💾 New playback requests will be saved for after the maintenance restart.\n"
                "🔄 After the grace period, each current track may finish; remaining tracks are saved and maintenance starts once streams drain.\n\n"
                f"💡 Use <code>/stop {name}</code> to cancel the queued restart and stop the deployment."
            )
        await status.edit_text(detail)

    @handler_errors
    async def cancel_restart(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text(
                "✖️ Usage: <code>/cancelrestart &lt;name|all&gt;</code>",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        target = args[1].strip().lower()
        deployments = (
            list(self.store.list().values())
            if target == "all"
            else [self.store.get(normalize_name(target))]
        )
        if not deployments or deployments == [None]:
            return await message.reply_text(
                f"❌ Deployment <b>{html.escape(target)}</b> was not found.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        cancelled = []
        untouched = []
        busy = []
        for deployment in deployments:
            with self.operations.acquire(
                deployment.name,
                "cancel queued restart",
                token=object(),
            ) as acquired:
                if not acquired:
                    busy.append(deployment.name)
                    continue
                if deployment.pending_restart:
                    self.clear_pending_restart(deployment)
                    self.store.update(deployment)
                    cancelled.append(deployment.name)
                    self.audit.record(
                        "cancel_restart",
                        issuer_id=message.from_user.id,
                        deployment=deployment.name,
                        result="success",
                    )
                else:
                    untouched.append(deployment.name)
        text = (
            f"✅ Cancelled queued restart for: <code>{', '.join(cancelled)}</code>"
            if cancelled
            else "📭 No queued restarts were found."
        )
        if untouched and target != "all":
            text += "\n\nℹ️ That deployment did not have a queued restart."
        if busy:
            text += (
                "\n\n⏳ Could not cancel while busy: <code>"
                + ", ".join(busy)
                + "</code>"
            )
        await message.reply_text(
            text, reply_parameters=ReplyParameters(message_id=message.id)
        )

    async def restart_all(
        self,
        message: Message,
        *,
        force: bool = False,
        idle: bool = False,
    ) -> None:
        deployments = list(self.store.list().values())
        if not deployments:
            return await message.reply_text(
                "📭 No deployments were found.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        status = await message.reply_text(
            (
                "🔎 Checking all deployments for complete natural idleness..."
                if idle
                else "🔎 Checking all deployments for active streams..."
            ),
            reply_parameters=ReplyParameters(message_id=message.id),
        )
        lines = ["<b>🔄 Restart All</b>"]
        counts = {"restarted": 0, "waiting": 0, "skipped": 0, "failed": 0}
        for deployment in deployments:
            if deployment.intentionally_stopped or not deployment.desired_running:
                counts["skipped"] += 1
                lines.append(
                    f"⚫ <b>{deployment.name}</b> — skipped; intentionally stopped"
                )
                continue

            await status.edit_text(
                f"🔎 Checking deployment <b>{deployment.name}</b>...\n\n"
                f"Processed: <code>{sum(counts.values())}/{len(deployments)}</code>"
            )
            result, detail = await self.request_restart(
                deployment,
                message.from_user.id,
                status,
                bulk=True,
                force=force,
                idle=idle,
            )
            if result in counts:
                counts[result] += 1
            else:
                counts["skipped"] += 1
            icon = {
                "restarted": "✅",
                "waiting": "⏳",
                "failed": "❌",
                "skipped": "⚫",
            }.get(result, "⚠️")
            lines.append(f"{icon} <b>{deployment.name}</b> — {detail}")

        lines.extend(
            [
                "",
                f"✅ Restarted immediately: <code>{counts['restarted']}</code>",
                f"⏳ Waiting: <code>{counts['waiting']}</code>",
                f"⚫ Skipped: <code>{counts['skipped']}</code>",
                f"❌ Failed: <code>{counts['failed']}</code>",
                "",
                (
                    "💤 Passive restarts do not restrict new playback and may wait indefinitely."
                    if idle
                    else "🔔 You will be notified when each waiting maintenance restart begins and completes."
                ),
            ]
        )
        await status.edit_text("\n".join(lines))

    async def request_restart(
        self,
        deployment: DeploymentMeta,
        requested_by: int,
        status: Message,
        *,
        bulk: bool = False,
        force: bool = False,
        idle: bool = False,
    ) -> tuple[str, str]:
        name = deployment.name
        if name in self.recovering:
            return (
                "skipped",
                "automatic recovery is already in progress",
            )
        if name in self.restarting:
            return (
                "skipped",
                "restart is already underway",
            )

        operation = (
            "force restart"
            if force
            else ("passive idle restart" if idle else "restart")
        )
        with self.operations.acquire(name, operation, token=object()) as acquired:
            if not acquired:
                return (
                    "skipped",
                    f"{self.operations.current(name)} is already in progress",
                )
            return await self._request_restart_locked(
                deployment,
                requested_by,
                status,
                bulk=bulk,
                force=force,
                idle=idle,
            )

    async def _request_restart_locked(
        self,
        deployment: DeploymentMeta,
        requested_by: int,
        status: Message,
        *,
        bulk: bool = False,
        force: bool = False,
        idle: bool = False,
    ) -> tuple[str, str]:
        name = deployment.name
        self.audit.record(
            "restart",
            issuer_id=requested_by,
            deployment=name,
            detail="force" if force else ("idle" if idle else "safe"),
        )
        if deployment.is_running and not force:
            deployment.pending_restart = True
            deployment.restart_requested_at = datetime.now(timezone.utc).isoformat()
            deployment.restart_requested_by = requested_by
            deployment.pending_restart_mode = "idle" if idle else "drain"
            deployment.pending_restart_reason = (
                "passive restart requested for the next completely idle moment"
                if idle
                else "scheduled maintenance requested manually"
            )
            if idle:
                try:
                    self.restart_marker_path(deployment).unlink(missing_ok=True)
                except OSError:
                    logger.exception(
                        "Could not remove maintenance marker for passive restart %s",
                        name,
                    )
                    self.clear_pending_restart(deployment)
                    self.store.update(deployment)
                    return (
                        "failed",
                        "could not switch to passive restart mode; check directory permissions",
                    )
            else:
                try:
                    self.write_restart_marker(deployment)
                except OSError:
                    self.clear_pending_restart(deployment)
                    self.store.update(deployment)
                    logger.exception(
                        "Could not create restart marker for deployment %s", name
                    )
                    return (
                        "failed",
                        "could not safely prepare the deployment for restart; check directory permissions",
                    )
            self.store.update(deployment)
            ready, detail = self.restart_ready(deployment)
            if not ready:
                return (
                    "waiting",
                    detail,
                )

        await status.edit_text(
            (
                f"🚨 Force restarting deployment <b>{name}</b> immediately for emergency maintenance..."
                if force
                else (
                    f"💤 Deployment <b>{name}</b> is completely idle.\n\n"
                    "🔄 Starting the passive restart now..."
                    if idle
                    else f"⚡ Deployment <b>{name}</b> has no active streams.\n\n🛠️ Restarting immediately for maintenance..."
                )
            )
        )
        if deployment.is_running:
            await status.edit_text(f"⏹️ Stopping deployment <b>{name}</b>...")
            stopped, error = self.stop_process(deployment)
            if not stopped:
                self.clear_pending_restart(deployment)
                self.store.update(deployment)
                logger.error(
                    "Failed to stop deployment %s for restart: %s", name, error
                )
                return (
                    "failed",
                    "could not be stopped, so it was not restarted",
                )
            self.store.update(deployment)
        elif deployment.pid:
            deployment.pid = None
            self.processes.pop(deployment.name, None)
            self.store.update(deployment)

        await status.edit_text(
            f"🚀 Starting deployment <b>{name}</b> after maintenance..."
        )
        started, error = self.start_process(deployment)
        if not started:
            self.clear_pending_restart(deployment)
            self.store.update(deployment)
            logger.error("Failed to restart deployment %s: %s", name, error)
            return (
                "failed",
                "stopped but could not start again; review its logs",
            )

        deployment.started_at = datetime.now(timezone.utc).isoformat()
        self.clear_pending_restart(deployment)
        self.store.update(deployment)
        self.audit.record(
            "restart",
            issuer_id=requested_by,
            deployment=name,
            result="success",
            detail="force" if force else "safe",
        )
        return (
            "restarted",
            (
                f"completed maintenance restart immediately; PID <code>{deployment.pid}</code>"
                if bulk
                else f"✅ Deployment <b>{name}</b> completed its maintenance restart.\nPID: <code>{deployment.pid}</code>"
            ),
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

        status = await message.reply_text(
            f"🔎 Verifying the bot token for <b>{name}</b>...",
            reply_parameters=ReplyParameters(message_id=message.id),
        )

        try:
            bot_user = await self.verify_bot_token(bot_token)
            logger.info(
                "Verified bot token for %s (%s)",
                name,
                bot_user.username or bot_user.first_name,
            )
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
        if manager_downloads_path:
            manager_downloads_path = resolve_manager_path(manager_downloads_path)
        manager_cookies_path = os.getenv("MANAGER_COOKIES_PATH", "")
        if manager_cookies_path:
            manager_cookies_path = resolve_manager_path(manager_cookies_path)
        manager_cookies_url = os.getenv("MANAGER_COOKIES_URL", "")

        deployment_id = uuid.uuid4().hex
        db_name = requested_db_name or normalize_name(
            f"{name}_{bot_user.id}_{deployment_id[:8]}"
        )
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
            cookies_path=manager_cookies_path,
            cookies_url=manager_cookies_url,
            api_key=self.config.api_key,
            owner_id=owner_id,
        )
        env_path.write_text(
            "\n".join(f"{key}={value}" for key, value in env_vars.items()),
            encoding="utf-8",
        )

        deployment = DeploymentMeta(
            name=name,
            bot_id=bot_user.id,
            username=f"@{bot_user.username}"
            if bot_user.username
            else bot_user.first_name,
            created_at=datetime.now(timezone.utc).isoformat(),
            path=str(deployment_dir.relative_to(ROOT)),
            db_name=db_name,
            deployment_id=deployment_id,
        )

        started, error = self.start_process(deployment)
        if started:
            deployment.started_at = datetime.now(timezone.utc).isoformat()
            self.store.add(deployment)
            logger.info(
                "Created and started deployment %s pid=%s", name, deployment.pid
            )
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
                created_text += (
                    "➡️ Next: send /start to the deployed bot in private chat."
                )
            await status.edit_text(created_text)
        else:
            logger.error("Deployment %s creation failed to start: %s", name, error)
            await status.edit_text(
                f"⚠️ Deployment <b>{name}</b> was created, but it could not start.\n\n"
                "💡 Check its <code>run.log</code>, dependencies, and generated <code>.env</code>, then use /deploy."
            )

    @handler_errors
    @deployment_operation("reconfigure")
    async def reconfigure(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=3)
        if len(args) < 2:
            return await message.reply_text(
                "🧰 Usage: /reconfigure &lt;name&gt; [bot_token] [owner_id]\n\n"
                "🔐 If no bot token is provided, I will reuse the token already stored in the deployment <code>.env</code>.\n"
                "💾 The existing database, deployment identity, and stored bot setup will be preserved.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        name = normalize_name(args[1])
        bot_token_override = ""
        owner_id = ""
        if len(args) > 2:
            first_value = args[2].strip()
            if re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", first_value):
                bot_token_override = first_value
                owner_id = args[3].strip() if len(args) > 3 else ""
            else:
                owner_id = first_value
                if len(args) > 3:
                    return await message.reply_text(
                        "❌ I could not understand those arguments.\n\n"
                        "💡 Use <code>/reconfigure &lt;name&gt; [owner_id]</code> to reuse the stored token, "
                        "or <code>/reconfigure &lt;name&gt; &lt;bot_token&gt; [owner_id]</code> to replace it.",
                        reply_parameters=ReplyParameters(message_id=message.id),
                    )
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

        env_path = deployment.deployment_path / ".env"
        if not env_path.exists():
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> is missing its <code>.env</code> file.\n\n"
                "💡 Restore the deployment files before reconfiguring it. Its database was not changed.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        env_vars = self.load_deployment_env(env_path)
        bot_token = bot_token_override or env_vars.get("BOT_TOKEN", "").strip()
        if not bot_token:
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> does not have a stored bot token.\n\n"
                "💡 Re-run the command with a token once: "
                f"<code>/reconfigure {name} &lt;bot_token&gt;</code>.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        mongo_url = env_vars.get("MONGO_URL")
        if not mongo_url:
            return await message.reply_text(
                f"❌ Deployment <b>{name}</b> does not have a stored MongoDB connection.\n\n"
                "💡 Restore <code>MONGO_URL</code> in its <code>.env</code>, then try again. Its database was not changed.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        status = await message.reply_text(
            f"🔎 Verifying the {'new' if bot_token_override else 'stored'} bot token for <b>{name}</b>...",
            reply_parameters=ReplyParameters(message_id=message.id),
        )
        try:
            bot_user = await self.verify_bot_token(bot_token)
        except Exception:
            logger.exception("Bot token verification failed for %s", name)
            return await status.edit_text(
                "❌ I could not verify that bot token.\n\n"
                "💡 If this was the stored token, copy a fresh token from @BotFather and run "
                f"<code>/reconfigure {name} &lt;bot_token&gt;</code> once."
            )

        await status.edit_text(f"⏹️ Stopping deployment <b>{name}</b> safely...")
        if deployment.is_running:
            stopped, error = self.stop_process(deployment)
            if not stopped:
                logger.error(
                    "Could not stop deployment %s before reconfiguration: %s",
                    name,
                    error,
                )
                return await status.edit_text(
                    f"❌ Deployment <b>{name}</b> could not be stopped, so no settings were changed.\n\n"
                    f"💡 Run <code>/stop {name}</code>, then try <code>/reconfigure</code> again."
                )
            self.store.update(deployment)
        elif deployment.pid:
            deployment.pid = None
            self.processes.pop(deployment.name, None)
            self.store.update(deployment)

        await status.edit_text(
            f"💾 Updating deployment <b>{name}</b> while preserving its database..."
        )
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

        if bot_token_override:
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
        deployment.username = (
            f"@{bot_user.username}" if bot_user.username else bot_user.first_name
        )
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
    @deployment_operation("change database")
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
                logger.error(
                    "Could not stop deployment %s before database switch: %s",
                    name,
                    error,
                )
                return await status.edit_text(
                    f"❌ Deployment <b>{name}</b> could not be stopped, so its database was not changed.\n\n"
                    f"💡 Run <code>/stop {name}</code>, then try again."
                )
            self.store.update(deployment)
        elif deployment.pid:
            deployment.pid = None
            self.processes.pop(deployment.name, None)
            self.store.update(deployment)

        await status.edit_text(
            f"💾 Switching deployment <b>{name}</b> to <code>{database_name}</code>..."
        )
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
        await status.edit_text(
            f"🚀 Starting deployment <b>{name}</b> with its new database..."
        )
        started, error = self.start_process(deployment)
        if not started:
            self.store.update(deployment)
            logger.error(
                "Deployment %s could not start after database switch: %s", name, error
            )
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

    async def _send_user_with_retry(self, user_id: int, text: str) -> None:
        for attempt in range(1, 4):
            try:
                await self.app.send_message(user_id, text)
                return
            except Exception as exc:
                logger.warning(
                    "Notification to user %s attempt %s failed: %s",
                    user_id,
                    attempt,
                    exc,
                )
                if attempt < 3:
                    await asyncio.sleep(attempt * 2)
        logger.error(
            "Notification to user %s could not be delivered after 3 attempts.", user_id
        )

    async def _send_owner_with_retry(self, text: str) -> None:
        await self._send_user_with_retry(self.config.owner_id, text)

    def _notify_owner(self, text: str) -> None:
        self._run_on_app_loop(self._send_owner_with_retry, text)

    def _notify_user(self, user_id: Optional[int], text: str) -> None:
        self._run_on_app_loop(
            self._send_user_with_retry,
            user_id or self.config.owner_id,
            text,
        )

    def process_matches(self, deployment: DeploymentMeta) -> bool:
        if not deployment.pid:
            return False
        try:
            process = psutil.Process(deployment.pid)
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return False
            if (
                deployment.process_created_at
                and abs(process.create_time() - deployment.process_created_at) > 2
            ):
                return False
            try:
                return (
                    Path(process.cwd()).resolve()
                    == deployment.deployment_path.resolve()
                )
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

    def active_stream_count(self, deployment: DeploymentMeta) -> Optional[int]:
        health = self.read_health(deployment)
        if not health:
            return None
        try:
            if (
                time.time() - float(health.get("timestamp", 0))
                > self.health_stale_after
            ):
                return None
            return max(0, int(health.get("active_voice_chats", 0)))
        except (TypeError, ValueError):
            return None

    def restart_ready(self, deployment: DeploymentMeta) -> tuple[bool, str]:
        health = self.read_health(deployment)
        if not health:
            return False, "waiting for a fresh heartbeat"
        try:
            if (
                time.time() - float(health.get("timestamp", 0))
                > self.health_stale_after
            ):
                return False, "waiting for a fresh heartbeat"
            streams = max(0, int(health.get("active_voice_chats", 0)))
        except (TypeError, ValueError):
            return False, "waiting for valid workload information"

        if deployment.pending_restart_mode != "idle":
            if streams:
                return (
                    False,
                    f"waiting for <code>{streams}</code> active stream"
                    f"{'s' if streams != 1 else ''} to finish",
                )
            return True, "no active streams remain"

        if (
            deployment.pending_restart_mode == "idle"
            and "live_queued_requests" not in health
        ):
            return (
                False,
                "waiting for the deployed bot to report complete queue activity",
            )
        try:
            live_queue = max(0, int(health.get("live_queued_requests", 0)))
            deferred = max(0, int(health.get("maintenance_queued_requests", 0)))
            play_requests = max(0, int(health.get("active_play_requests", 0)))
        except (TypeError, ValueError):
            return False, "waiting for valid queue information"
        operations = health.get("playback_operations") or {}
        operation_count = (
            len(operations) if isinstance(operations, dict) else int(bool(operations))
        )
        blockers = []
        if streams:
            blockers.append(f"{streams} active stream{'s' if streams != 1 else ''}")
        if live_queue:
            blockers.append(
                f"{live_queue} live queue item{'s' if live_queue != 1 else ''}"
            )
        if deferred:
            blockers.append(f"{deferred} saved request{'s' if deferred != 1 else ''}")
        if operation_count:
            blockers.append(
                f"{operation_count} playback operation{'s' if operation_count != 1 else ''}"
            )
        if play_requests:
            blockers.append(
                f"{play_requests} play request{'s' if play_requests != 1 else ''} being prepared"
            )
        if health.get("broadcast_active"):
            blockers.append("an active broadcast")
        if blockers:
            return False, "currently busy with " + ", ".join(blockers)
        return True, "the deployment is completely idle"

    @staticmethod
    def restart_marker_path(deployment: DeploymentMeta) -> Path:
        return deployment.deployment_path / ".restart-when-idle"

    def write_restart_marker(self, deployment: DeploymentMeta) -> None:
        marker = self.restart_marker_path(deployment)
        if marker.exists():
            return
        marker.write_text(
            deployment.restart_requested_at or datetime.now(timezone.utc).isoformat(),
            encoding="ascii",
        )

    def clear_pending_restart(self, deployment: DeploymentMeta) -> None:
        deployment.pending_restart = False
        deployment.restart_requested_at = None
        deployment.restart_requested_by = None
        deployment.pending_restart_reason = None
        deployment.pending_restart_mode = "drain"
        try:
            self.restart_marker_path(deployment).unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "Could not remove restart marker for deployment %s.", deployment.name
            )

    def run_pending_restart(self, deployment: DeploymentMeta) -> None:
        with self.operations.acquire(deployment.name, "queued restart") as acquired:
            if not acquired:
                logger.info(
                    "Queued restart for %s is waiting for operation %s.",
                    deployment.name,
                    self.operations.current(deployment.name),
                )
                return
            self._run_pending_restart_locked(deployment)

    def queue_heartbeat_restart(
        self, deployment: DeploymentMeta, health: Optional[dict]
    ) -> bool:
        if (
            not health
            or deployment.pending_restart
            or deployment.intentionally_stopped
            or not deployment.desired_running
            or not self.process_matches(deployment)
        ):
            return False
        try:
            if (
                time.time() - float(health.get("timestamp", 0))
                > self.health_stale_after
            ):
                return False
        except (TypeError, ValueError):
            return False

        request = health.get("restart_request")
        if not isinstance(request, dict):
            return False
        reason = str(request.get("reason") or "deployed bot requested a safe restart")[
            :200
        ]
        requested_at = request.get("requested_at")
        try:
            requested = datetime.fromtimestamp(
                float(requested_at), timezone.utc
            ).isoformat()
        except (TypeError, ValueError, OSError):
            requested = datetime.now(timezone.utc).isoformat()

        deployment.pending_restart = True
        deployment.restart_requested_at = requested
        deployment.restart_requested_by = self.config.owner_id
        deployment.pending_restart_reason = reason
        deployment.pending_restart_mode = "drain"
        deployment.last_failure = reason
        try:
            self.write_restart_marker(deployment)
        except OSError:
            self.clear_pending_restart(deployment)
            deployment.last_failure = (
                f"Could not queue playback recovery restart: {reason}"
            )
            self.store.update(deployment)
            logger.exception(
                "Could not create playback recovery marker for %s.", deployment.name
            )
            self._notify_owner(
                f"❌ Deployment <b>{deployment.name}</b> requested playback recovery, but its "
                "safe-restart marker could not be created.\n\n"
                f"💡 Review <code>/logs {deployment.name}</code> and use "
                f"<code>/restart {deployment.name}</code>."
            )
            return False

        self.store.update(deployment)
        streams = self.active_stream_count(deployment)
        waiting = (
            "waiting for a fresh heartbeat before restarting"
            if streams is None
            else (
                "restarting as soon as the watcher confirms the deployment is idle"
                if streams == 0
                else f"waiting for {streams} active stream{'s' if streams != 1 else ''} to finish"
            )
        )
        chat_id = request.get("chat_id", "unknown")
        assistant_slot = request.get("assistant_slot", "unknown")
        self.audit.record(
            "playback_recovery_restart",
            issuer_id=self.config.owner_id,
            deployment=deployment.name,
            result="queued",
            detail=f"{reason}; chat={chat_id}; assistant={assistant_slot}",
        )
        self._notify_owner(
            f"⚠️ <b>Playback recovery maintenance restart queued for {deployment.name}</b>\n\n"
            f"Reason: <code>{html.escape(reason)}</code>\n"
            f"Affected chat: <code>{html.escape(str(chat_id))}</code>\n"
            f"Assistant slot: <code>{html.escape(str(assistant_slot))}</code>\n\n"
            f"🔄 The manager is {waiting}. This graceful recovery does not count against "
            "the frozen-deployment recovery limit."
        )
        return True

    def _run_pending_restart_locked(self, deployment: DeploymentMeta) -> None:
        with self.recovery_guard:
            if (
                deployment.name in self.restarting
                or deployment.name in self.recovering
                or not deployment.pending_restart
            ):
                return
            self.restarting.add(deployment.name)

        requested_by = deployment.restart_requested_by
        restart_reason = deployment.pending_restart_reason
        try:
            if (
                not deployment.desired_running
                or deployment.intentionally_stopped
                or not self.process_matches(deployment)
            ):
                self.clear_pending_restart(deployment)
                self.store.update(deployment)
                return

            ready, _ = self.restart_ready(deployment)
            if not ready:
                return

            passive = deployment.pending_restart_mode == "idle"
            logger.info(
                "Running queued %s restart for deployment %s.",
                "passive idle" if passive else "maintenance",
                deployment.name,
            )
            self._notify_user(
                requested_by,
                (
                    f"💤 Passive restart for deployment <b>{deployment.name}</b> is now underway.\n\n"
                    "✅ The deployment naturally reached a completely idle moment. No playback "
                    "or queue requests were blocked while it waited."
                    if passive
                    else (
                        f"🛠️ Maintenance restart for deployment <b>{deployment.name}</b> is now underway.\n\n"
                        "✅ All active streams have finished. Saved maintenance requests will resume "
                        "after the deployment starts again."
                    )
                )
                + (
                    f"\nReason: <code>{html.escape(deployment.pending_restart_reason)}</code>"
                    if deployment.pending_restart_reason
                    else ""
                ),
            )
            stopped, error = self.stop_process(deployment)
            if not stopped:
                self.clear_pending_restart(deployment)
                deployment.last_failure = (
                    f"Queued restart could not stop deployment: {error}"
                )
                self.store.update(deployment)
                self._notify_user(
                    requested_by,
                    f"❌ Maintenance restart for deployment <b>{deployment.name}</b> failed because "
                    "the process could not be stopped.\n\n"
                    f"💡 Review <code>/logs {deployment.name}</code>, then request the restart again.",
                )
                return

            started, error = self.start_process(deployment, mark_desired=False)
            self.clear_pending_restart(deployment)
            if not started:
                deployment.last_failure = (
                    f"Queued restart could not start deployment: {error}"
                )
                self.store.update(deployment)
                self._notify_user(
                    requested_by,
                    f"❌ Deployment <b>{deployment.name}</b> stopped for maintenance after its streams finished "
                    "but could not start again.\n\n"
                    f"💡 Review <code>/logs {deployment.name}</code>, then use <code>/deploy {deployment.name}</code>.",
                )
                return

            deployment.last_failure = None
            self.store.update(deployment)
            self._notify_user(
                requested_by,
                f"✅ Deployment <b>{deployment.name}</b> completed its "
                f"{'passive idle' if passive else 'maintenance'} restart.\n"
                f"PID: <code>{deployment.pid}</code>",
            )
            self.audit.record(
                "queued_restart",
                issuer_id=requested_by,
                deployment=deployment.name,
                result="success",
                detail=restart_reason or "",
            )
        finally:
            with self.recovery_guard:
                self.restarting.discard(deployment.name)

    def deployment_health(
        self, deployment: DeploymentMeta
    ) -> tuple[str, Optional[dict], str]:
        if deployment.name in self.recovering:
            return (
                "recovering",
                self.read_health(deployment),
                "automatic recovery in progress",
            )
        if deployment.name in self.restarting:
            return (
                "recovering",
                self.read_health(deployment),
                "queued restart in progress",
            )
        if not deployment.desired_running:
            return "stopped", self.read_health(deployment), "stopped intentionally"
        if not self.process_matches(deployment):
            return (
                "stopped",
                self.read_health(deployment),
                "deployment process is not running",
            )

        health = self.read_health(deployment)
        if not health:
            if not deployment.process_created_at:
                return (
                    "healthy",
                    None,
                    "heartbeat monitoring activates after the deployment's next restart",
                )
            started = self.parse_timestamp(deployment.started_at)
            if started and time.time() - started < self.health_stale_after:
                return "starting", None, "waiting for the first heartbeat"
            return (
                "frozen",
                None,
                "heartbeat file is missing or belongs to another process",
            )

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
        for key in (
            "BOT_TOKEN",
            "API_HASH",
            "API_KEY",
            "MONGO_URL",
            "COOKIES_URL",
            "SESSION",
            "SESSION1",
            "SESSION2",
            "SESSION3",
        ):
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
                lines = log_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()[-12:]
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
                    except (
                        psutil.NoSuchProcess,
                        psutil.AccessDenied,
                        psutil.ZombieProcess,
                    ):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        system_memory = psutil.virtual_memory()
        system_cpu = psutil.cpu_percent(interval=0.2)
        started = self.parse_timestamp(deployment.started_at)
        uptime = int(time.time() - started) if started else 0
        tail = self.sanitize_text(deployment, "\n".join(lines))
        tail = (
            html.escape(tail[-1200:])[:2200]
            if tail
            else "No recent log lines were available."
        )
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
            value
            for value in (deployment.restart_history or [])
            if (self.parse_timestamp(value) or 0) >= cutoff
        ]
        deployment.restart_history = recent
        return recent

    def recover_deployment(self, deployment: DeploymentMeta, reason: str) -> None:
        with self.operations.acquire(deployment.name, "automatic recovery") as acquired:
            if not acquired:
                logger.info(
                    "Automatic recovery for %s deferred while %s is active.",
                    deployment.name,
                    self.operations.current(deployment.name),
                )
                return
            self._recover_deployment_locked(deployment, reason)

    def _recover_deployment_locked(
        self, deployment: DeploymentMeta, reason: str
    ) -> None:
        queued_restart_requester = deployment.restart_requested_by
        self.audit.record(
            "automatic_recovery",
            deployment=deployment.name,
            detail=reason,
        )
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
                    deployment.last_failure = (
                        f"Frozen process could not be stopped: {error}"
                    )
                    self.store.update(deployment)
                    self._notify_owner(
                        report
                        + "\n\n❌ Automatic recovery failed because the frozen process could not be stopped."
                    )
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

            deployment.restart_history = recent + [
                datetime.now(timezone.utc).isoformat()
            ]
            deployment.last_failure = reason
            if deployment.pending_restart:
                self.clear_pending_restart(deployment)
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
                    fresh = (
                        health
                        and time.time() - float(health.get("timestamp", 0))
                        <= self.health_stale_after
                    )
                except (TypeError, ValueError):
                    fresh = False
                if (
                    self.process_matches(deployment)
                    and fresh
                    and health.get("state") == "healthy"
                ):
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
                self.audit.record(
                    "automatic_recovery",
                    deployment=deployment.name,
                    result="success",
                    detail=reason,
                )
                self._notify_owner(
                    report
                    + f"\n\n✅ Deployment <b>{deployment.name}</b> was restarted automatically because it stopped responding."
                )
                if (
                    queued_restart_requester
                    and queued_restart_requester != self.config.owner_id
                ):
                    self._notify_user(
                        queued_restart_requester,
                        f"✅ Queued restart for deployment <b>{deployment.name}</b> was completed "
                        "as part of automatic recovery.",
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
            logger.warning(
                "Could not send deployment log for %s: %s", deployment.name, exc
            )
            await self._send_owner_with_retry(
                f"⚠️ I could not send the error log for <b>{deployment.name}</b>.\n\n"
                "💡 Check the deployment directory permissions and manager logs."
            )
        finally:
            if sanitized_path:
                Path(sanitized_path).unlink(missing_ok=True)

    def _monitor_loop(self) -> None:
        logger.info(
            "Deployment watcher started with interval %s seconds.",
            self.monitor_interval,
        )
        while not self.shutdown_event.wait(self.monitor_interval):
            for deployment in list(self.store.list().values()):
                try:
                    if (
                        deployment.pending_restart
                        and deployment.pending_restart_reason
                        == "scheduled maintenance to apply updated bot code"
                    ):
                        self.clear_pending_restart(deployment)
                        self.store.update(deployment)
                        self.audit.record(
                            "code_update_restart",
                            issuer_id=self.config.owner_id,
                            deployment=deployment.name,
                            result="cancelled",
                            detail="automatic code-update restarts were disabled",
                        )
                        logger.info(
                            "Cancelled obsolete automatic code-update restart for %s.",
                            deployment.name,
                        )
                    health = self.read_health(deployment)
                    self.queue_heartbeat_restart(deployment, health)
                    if (
                        deployment.pending_restart
                        and deployment.name not in self.restarting
                        and deployment.name not in self.recovering
                        and self.process_matches(deployment)
                        and self.restart_ready(deployment)[0]
                    ):
                        threading.Thread(
                            target=self.run_pending_restart,
                            args=(deployment,),
                            name=f"restart-when-idle-{deployment.name}",
                            daemon=True,
                        ).start()
                        continue
                    if deployment.pending_restart:
                        if deployment.pending_restart_mode == "idle":
                            try:
                                self.restart_marker_path(deployment).unlink(
                                    missing_ok=True
                                )
                            except OSError:
                                logger.warning(
                                    "Could not remove maintenance marker for passive restart %s.",
                                    deployment.name,
                                )
                        else:
                            try:
                                self.write_restart_marker(deployment)
                            except OSError:
                                logger.warning(
                                    "Could not restore restart marker for deployment %s.",
                                    deployment.name,
                                )
                    else:
                        try:
                            self.restart_marker_path(deployment).unlink(missing_ok=True)
                        except OSError:
                            logger.warning(
                                "Could not remove orphaned restart marker for deployment %s.",
                                deployment.name,
                            )

                    state, _, reason = self.deployment_health(deployment)
                    if state in {"healthy", "starting", "recovering"} or (
                        state == "stopped" and not deployment.desired_running
                    ):
                        if state == "healthy" and deployment.manager_sudoers:
                            reconcile_key = f"{deployment.name}:{deployment.process_created_at or deployment.pid or ''}"
                            if reconcile_key not in self.sudo_reconciled:
                                self.sudo_reconciled.add(reconcile_key)
                                self._run_on_app_loop(
                                    self.reconcile_manager_sudoers, deployment
                                )
                        elif state not in {"starting", "recovering"}:
                            self.sudo_reconciled = {
                                key
                                for key in self.sudo_reconciled
                                if not key.startswith(f"{deployment.name}:")
                            }
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
                        if (
                            deployment.intentionally_stopped
                            or not deployment.desired_running
                        ):
                            self.stale_health_counts.pop(deployment.name, None)
                            continue
                        threading.Thread(
                            target=self.recover_deployment,
                            args=(deployment, reason),
                            name=f"recover-{deployment.name}",
                            daemon=True,
                        ).start()
                except Exception:
                    logger.exception(
                        "Deployment health check failed unexpectedly for %s",
                        deployment.name,
                    )

    async def _send_scheduled_backup(self) -> None:
        success, error = await self.send_backup(scheduled=True)
        if not success and error != "A backup is already in progress.":
            await self._send_owner_with_retry(
                "❌ The daily manager recovery backup could not be delivered.\n\n"
                f"Reason: <code>{html.escape(error)}</code>\n\n"
                "💡 Run <code>/backup</code> to retry manually."
            )

    def _backup_loop(self) -> None:
        logger.info(
            "Daily backup scheduler started with interval %s seconds.",
            self.backup_interval,
        )
        while not self.shutdown_event.wait(60):
            state = self.read_backup_state()
            reference = state.get("last_success") or state.get("last_attempt")
            last_run = self.parse_timestamp(reference) or 0
            if time.time() - last_run < self.backup_interval:
                continue
            self._run_on_app_loop(self._send_scheduled_backup)

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
            env["DOWNLOADS_PATH"] = resolve_manager_path(manager_downloads_path)
        manager_cookies_path = os.getenv("MANAGER_COOKIES_PATH")
        if manager_cookies_path:
            env["COOKIES_PATH"] = resolve_manager_path(manager_cookies_path)
        manager_cookies_url = os.getenv("MANAGER_COOKIES_URL")
        if manager_cookies_url:
            env["COOKIES_URL"] = manager_cookies_url
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
            logger.warning(
                "Could not remove stale launch files for %s", deployment.name
            )
        logger.info(
            "Starting deployment %s at %s", deployment.name, deployment.deployment_path
        )
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
                deployment.pid = int(
                    launch_pid_file.read_text(encoding="ascii").strip()
                )
                launch_pid_file.unlink(missing_ok=True)
            else:
                with (
                    open(os.devnull, "rb") as stdin,
                    log_file.open("a", encoding="utf-8") as stdout,
                ):
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
            logger.info(
                "Deployment %s started with pid=%s", deployment.name, deployment.pid
            )
            return True, None
        except Exception as exc:
            try:
                launch_pid_file.unlink(missing_ok=True)
            except OSError:
                pass
            error_text = str(exc)
            logger.error(
                "Failed to start deployment %s: %s", deployment.name, error_text
            )
            return False, error_text

    def stop_process(self, deployment: DeploymentMeta) -> tuple[bool, Optional[str]]:
        if not deployment.pid:
            logger.warning("Deployment %s has no pid to stop.", deployment.name)
            return False, "No pid found for deployment."
        if not self.process_matches(deployment):
            logger.warning(
                "Deployment %s has a stale or mismatched pid; clearing it without sending a signal.",
                deployment.name,
            )
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
                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
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
                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    pass
            if process and process.poll() is None:
                process.terminate()
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception as exc:
            logger.warning(
                "Failed graceful terminate for %s pid=%s: %s", deployment.name, pid, exc
            )
        try:
            if process:
                process.wait(timeout=10)
            else:
                psutil.Process(pid).wait(timeout=10)
            return stopped()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return stopped()
        except (psutil.TimeoutExpired, subprocess.TimeoutExpired) as exc:
            logger.warning(
                "Graceful stop timed out for %s pid=%s: %s", deployment.name, pid, exc
            )
            try:
                if process:
                    process.kill()
                else:
                    os.killpg(
                        os.getpgid(pid), getattr(signal, "SIGKILL", signal.SIGTERM)
                    )
                for child in child_processes:
                    try:
                        child.kill()
                    except (
                        psutil.NoSuchProcess,
                        psutil.AccessDenied,
                        psutil.ZombieProcess,
                    ):
                        pass
            except (ProcessLookupError, psutil.NoSuchProcess):
                return stopped()
            except Exception as kill_error:
                logger.error(
                    "Failed to kill deployment %s pid=%s: %s",
                    deployment.name,
                    pid,
                    kill_error,
                )
                return False, str(kill_error)

            try:
                if process:
                    process.wait(timeout=5)
                else:
                    psutil.Process(pid).wait(timeout=5)
                return stopped()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                return stopped()
            except (
                psutil.TimeoutExpired,
                subprocess.TimeoutExpired,
                psutil.AccessDenied,
            ) as wait_error:
                logger.error(
                    "Deployment %s pid=%s still exists after force-stop: %s",
                    deployment.name,
                    pid,
                    wait_error,
                )
                return False, str(wait_error)
        except psutil.AccessDenied as exc:
            logger.error(
                "Permission denied while stopping deployment %s pid=%s: %s",
                deployment.name,
                pid,
                exc,
            )
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
        if os.getenv("PM2_HOME") and os.getenv("MANAGER_PM2_TREEKILL_DISABLED") != "1":
            logger.warning(
                "Manager is running under PM2 without the protected ecosystem configuration. "
                "PM2 may kill deployed bots when restarting the manager. "
                "Start it with ecosystem.config.cjs."
            )
        signal.signal(signal.SIGINT, self._shutdown_handler)
        signal.signal(signal.SIGTERM, self._shutdown_handler)
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.backup_thread = threading.Thread(target=self._backup_loop, daemon=True)
        try:
            # Start the bot client so the event loop is available for notifications
            self.app.start()

            # start monitor thread after app started
            self.monitor_thread.start()
            self.backup_thread.start()

            # Block until stop signal; idle keeps the client running.
            idle()
        finally:
            # Shutdown sequence
            self.shutdown_event.set()
            if self.monitor_thread.is_alive():
                self.monitor_thread.join(timeout=2)
            if self.backup_thread.is_alive():
                self.backup_thread.join(timeout=2)
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
