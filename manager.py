#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict, dataclass
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


@dataclass
class ManagerConfig:
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int
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
            default_mongo_url=default_mongo_url,
            deployments_dir=deployments_dir,
            template_path=template_path,
            api_key=api_key,
        )


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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeploymentMeta":
        return cls(**data)

    @property
    def deployment_path(self) -> Path:
        return ROOT / self.path

    @property
    def is_running(self) -> bool:
        if not self.pid:
            return False
        try:
            process = psutil.Process(self.pid)
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False


class DeploymentStore:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self.deployments: Dict[str, DeploymentMeta] = {}
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
        logger.info("Saving %d deployments to %s", len(self.deployments), self.store_path)
        self.store_path.write_text(
            json.dumps(
                {"deployments": {name: deployment.to_dict() for name, deployment in self.deployments.items()}},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def add(self, deployment: DeploymentMeta) -> None:
        self.deployments[deployment.name] = deployment
        self.save()

    def update(self, deployment: DeploymentMeta) -> None:
        self.deployments[deployment.name] = deployment
        self.save()

    def remove(self, name: str) -> None:
        self.deployments.pop(name, None)
        self.save()

    def get(self, name: str) -> Optional[DeploymentMeta]:
        return self.deployments.get(name)

    def list(self) -> Dict[str, DeploymentMeta]:
        return self.deployments


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
        self.monitor_interval = int(os.getenv("MANAGER_MONITOR_INTERVAL", "20"))
        self.processes: Dict[str, subprocess.Popen] = {}
        self.failed_restarts: set[str] = set()
        self.app = Client(
            name="deploy-manager",
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            bot_token=self.config.bot_token,
        )
        logger.info("Manager bot configured for owner_id=%s and deployments_dir=%s", self.config.owner_id, self.config.deployments_dir)

        self.app.on_message(filters.private & filters.command("start") & filters.user(self.config.owner_id))(self.start)
        self.app.on_message(filters.private & filters.command("help") & filters.user(self.config.owner_id))(self.help)
        self.app.on_message(filters.private & filters.command("newbot") & filters.user(self.config.owner_id))(self.newbot)
        self.app.on_message(filters.private & filters.command(["reconfigure", "rebuild"]) & filters.user(self.config.owner_id))(self.reconfigure)
        self.app.on_message(filters.private & filters.command("list") & filters.user(self.config.owner_id))(self.list_bots)
        self.app.on_message(filters.private & filters.command("status") & filters.user(self.config.owner_id))(self.status)
        self.app.on_message(filters.private & filters.command("deploy") & filters.user(self.config.owner_id))(self.deploy)
        self.app.on_message(filters.private & filters.command("stop") & filters.user(self.config.owner_id))(self.stop)
        self.app.on_message(filters.private & filters.command("delete") & filters.user(self.config.owner_id))(self.delete)
        self.app.on_message(filters.private & filters.command("restart") & filters.user(self.config.owner_id))(self.restart)

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
            "📋 /list - Show all deployments.\n"
            "🔎 /status &lt;name&gt; - Show deployment status.\n"
            "▶️ /deploy &lt;name&gt; - Start a stopped deployment.\n"
            "⏹️ /stop &lt;name&gt; - Stop a running deployment.\n"
            "🗑️ /delete &lt;name&gt; - Permanently delete a deployment.\n"
            "🔄 /restart &lt;name&gt; - Restart a deployed bot.\n",
            reply_parameters=ReplyParameters(message_id=message.id),
        )

    @handler_errors
    async def list_bots(self, client: Client, message: Message) -> None:
        if not self.store.list():
            return await message.reply_text("📭 No deployments found.", reply_parameters=ReplyParameters(message_id=message.id))

        lines = ["<b>📋 Deployments</b>"]
        for name, deployment in self.store.list().items():
            status = "🟢 running" if deployment.is_running else "⚫ stopped"
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

        state = "running" if deployment.is_running else "stopped"
        text = (
            f"<b>{deployment.username}</b>\n"
            f"Name: <code>{deployment.name}</code>\n"
            f"Bot ID: <code>{deployment.bot_id}</code>\n"
            f"DB: <code>{deployment.db_name or 'legacy'}</code>\n"
            f"Deployment ID: <code>{deployment.deployment_id or 'legacy'}</code>\n"
            f"Status: <code>{state}</code>\n"
            f"PID: <code>{deployment.pid or 'none'}</code>\n"
            f"Created: {deployment.created_at}\n"
            f"Started: {deployment.started_at or 'never'}\n"
            f"Path: <code>{deployment.deployment_path}</code>"
        )
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
            return await message.reply_text(f"⚫ Deployment <b>{name}</b> is already stopped.", reply_parameters=ReplyParameters(message_id=message.id))

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

    def _notify_owner(self, text: str) -> None:
        self._run_on_app_loop(self.app.send_message, self.config.owner_id, text)

    def _send_deployment_log(self, deployment: DeploymentMeta) -> None:
        log_path = deployment.deployment_path / "run.log"
        if not log_path.exists():
            self._notify_owner(
                f"⚠️ Log file for deployment <b>{deployment.name}</b> was not found."
            )
            return

        try:
            self._run_on_app_loop(
                self.app.send_document,
                self.config.owner_id,
                str(log_path),
                caption=f"Deployment <b>{deployment.name}</b> error log",
            )
        except Exception as exc:
            logger.warning("Could not send deployment log for %s: %s", deployment.name, exc)
            self._notify_owner(
                f"⚠️ I could not send the error log for <b>{deployment.name}</b>.\n\n"
                "💡 Check the deployment directory permissions and manager logs."
            )

    def _monitor_loop(self) -> None:
        logger.info("Deployment watcher started with interval %s seconds.", self.monitor_interval)
        while not self.shutdown_event.wait(self.monitor_interval):
            for deployment in list(self.store.list().values()):
                if deployment.pid and not deployment.is_running:
                    if deployment.name in self.failed_restarts:
                        continue

                    logger.warning(
                        "Deployment %s exited unexpectedly (pid=%s); sending logs instead of restarting.",
                        deployment.name,
                        deployment.pid,
                    )
                    self.failed_restarts.add(deployment.name)
                    self._notify_owner(
                        f"⚠️ Deployment <b>{deployment.name}</b> exited unexpectedly and will not be auto-restarted. Sending logs now."
                    )
                    self._send_deployment_log(deployment)

    def start_process(self, deployment: DeploymentMeta) -> tuple[bool, Optional[str]]:
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
        logger.info("Starting deployment %s at %s", deployment.name, deployment.deployment_path)
        try:
            if os.name == "posix":
                read_fd, write_fd = os.pipe()
                pid = os.fork()
                if pid == 0:
                    try:
                        os.close(read_fd)
                        os.setsid()
                        pid2 = os.fork()
                        if pid2 > 0:
                            os.write(write_fd, str(pid2).encode("utf-8"))
                            os.close(write_fd)
                            os._exit(0)

                        os.close(write_fd)
                        with open(os.devnull, "rb") as stdin, log_file.open("a", encoding="utf-8") as stdout:
                            os.dup2(stdin.fileno(), 0)
                            os.dup2(stdout.fileno(), 1)
                            os.dup2(stdout.fileno(), 2)
                            os.chdir(deployment.deployment_path)
                            os.execvpe(sys.executable, [sys.executable, "-m", "anony"], env)
                    except Exception as exc:
                        logger.error("Daemon launch failed for %s: %s", deployment.name, exc)
                        os._exit(1)

                os.close(write_fd)
                child_pid_bytes = os.read(read_fd, 32)
                os.close(read_fd)
                if not child_pid_bytes:
                    os.waitpid(pid, 0)
                    return False, "Failed to capture deployment pid."

                os.waitpid(pid, 0)
                deployment.pid = int(child_pid_bytes.decode("utf-8").strip())
                self.processes[deployment.name] = None
            else:
                process = subprocess.Popen(
                    [sys.executable, "-m", "anony"],
                    cwd=deployment.deployment_path,
                    env=env,
                    stdout=log_file.open("a", encoding="utf-8"),
                    stderr=subprocess.STDOUT,
                )
                deployment.pid = process.pid
                self.processes[deployment.name] = process
            self.failed_restarts.discard(deployment.name)
            logger.info("Deployment %s started with pid=%s", deployment.name, deployment.pid)
            return True, None
        except Exception as exc:
            error_text = str(exc)
            logger.error("Failed to start deployment %s: %s", deployment.name, error_text)
            return False, error_text

    def stop_process(self, deployment: DeploymentMeta) -> tuple[bool, Optional[str]]:
        if not deployment.pid:
            logger.warning("Deployment %s has no pid to stop.", deployment.name)
            return False, "No pid found for deployment."
        pid = deployment.pid
        logger.info("Stopping deployment %s pid=%s", deployment.name, pid)
        process = self.processes.get(deployment.name)

        def stopped() -> tuple[bool, Optional[str]]:
            logger.info("Deployment %s stopped.", deployment.name)
            deployment.pid = None
            self.processes.pop(deployment.name, None)
            return True, None

        try:
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

