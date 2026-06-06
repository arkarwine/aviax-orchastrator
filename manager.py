#!/usr/bin/env python3
import json
import logging
import os
import re
import signal
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.errors import RPCError
from pyrogram.types import Message
import psutil

ROOT = Path(__file__).resolve().parent
MANAGER_ENV = ROOT / "manager.env"
STORE_PATH = ROOT / "manager_deployments.json"

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
    default_logger_id: Optional[int]
    deployments_dir: Path
    template_path: Path

    @classmethod
    def load(cls) -> "ManagerConfig":
        api_id = int(os.getenv("MANAGER_API_ID", "0"))
        api_hash = os.getenv("MANAGER_API_HASH", "")
        bot_token = os.getenv("MANAGER_BOT_TOKEN", "")
        owner_id = int(os.getenv("MANAGER_OWNER_ID", "0"))
        default_mongo_url = os.getenv("MANAGER_DEFAULT_MONGO_URL")
        default_logger_id = os.getenv("MANAGER_DEFAULT_LOGGER_ID")
        deployments_dir = Path(os.getenv("DEPLOYMENTS_DIR", "deployments")).resolve()
        template_path = Path(os.getenv("TEMPLATE_PATH", ".")).resolve()

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
            default_logger_id=int(default_logger_id) if default_logger_id else None,
            deployments_dir=deployments_dir,
            template_path=template_path,
        )


@dataclass
class DeploymentMeta:
    name: str
    bot_id: int
    username: str
    created_at: str
    path: str
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
            self.deployments = {}
            return
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            self.deployments = {
                name: DeploymentMeta.from_dict(item)
                for name, item in data.get("deployments", {}).items()
            }
        except json.JSONDecodeError:
            self.deployments = {}

    def save(self) -> None:
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

    def get(self, name: str) -> Optional[DeploymentMeta]:
        return self.deployments.get(name)

    def list(self) -> Dict[str, DeploymentMeta]:
        return self.deployments


def normalize_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "_", value.strip().lower())
    return normalized.strip("_") or "bot"


def env_from_template(**values: str) -> Dict[str, str]:
    env = {
        "API_ID": str(values["api_id"]),
        "API_HASH": values["api_hash"],
        "BOT_TOKEN": values["bot_token"],
        "MONGO_URL": values["mongo_url"],
        "LOGGER_ID": str(values["logger_id"]),
        "OWNER_ID": str(values["owner_id"]),
        "SESSION": values["session"],
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
    return env


class BotManager:
    def __init__(self, config: ManagerConfig, store: DeploymentStore) -> None:
        self.config = config
        self.store = store
        self.app = Client(
            name="deploy-manager",
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            bot_token=self.config.bot_token,
        )

        self.app.on_message(filters.private & filters.command("start") & filters.user(self.config.owner_id))(self.start)
        self.app.on_message(filters.private & filters.command("help") & filters.user(self.config.owner_id))(self.help)
        self.app.on_message(filters.private & filters.command("newbot") & filters.user(self.config.owner_id))(self.newbot)
        self.app.on_message(filters.private & filters.command("list") & filters.user(self.config.owner_id))(self.list_bots)
        self.app.on_message(filters.private & filters.command("status") & filters.user(self.config.owner_id))(self.status)
        self.app.on_message(filters.private & filters.command("deploy") & filters.user(self.config.owner_id))(self.deploy)
        self.app.on_message(filters.private & filters.command("stop") & filters.user(self.config.owner_id))(self.stop)

    async def start(self, client: Client, message: Message) -> None:
        await message.reply_text(
            "<b>Music Bot Deployment Manager</b>\n"
            "Use /help to see available commands.",
            quote=True,
        )

    async def help(self, client: Client, message: Message) -> None:
        await message.reply_text(
            "<b>Manager Commands</b>\n"
            "/newbot &lt;name&gt; &lt;bot_token&gt; &lt;session_string&gt; [mongo_url] - Create and start a new deployment.\n"
            "/list - Show all deployments.\n"
            "/status &lt;name&gt; - Show deployment status.\n"
            "/deploy &lt;name&gt; - Start a stopped deployment.\n"
            "/stop &lt;name&gt; - Stop a running deployment.\n",
            quote=True,
        )

    async def list_bots(self, client: Client, message: Message) -> None:
        if not self.store.list():
            return await message.reply_text("No deployments found.", quote=True)

        lines = ["<b>Deployments</b>"]
        for name, deployment in self.store.list().items():
            status = "running" if deployment.is_running else "stopped"
            lines.append(
                f"<b>{deployment.username}</b> ({name}) — <code>{status}</code>"
            )
        await message.reply_text("\n".join(lines), quote=True)

    async def status(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text("Usage: /status &lt;name&gt;", quote=True)

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(f"Deployment <b>{name}</b> not found.", quote=True)

        state = "running" if deployment.is_running else "stopped"
        text = (
            f"<b>{deployment.username}</b>\n"
            f"Name: <code>{deployment.name}</code>\n"
            f"Bot ID: <code>{deployment.bot_id}</code>\n"
            f"Status: <code>{state}</code>\n"
            f"PID: <code>{deployment.pid or 'none'}</code>\n"
            f"Created: {deployment.created_at}\n"
            f"Started: {deployment.started_at or 'never'}\n"
            f"Path: <code>{deployment.deployment_path}</code>"
        )
        await message.reply_text(text, quote=True)

    async def deploy(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text("Usage: /deploy &lt;name&gt;", quote=True)

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(f"Deployment <b>{name}</b> not found.", quote=True)

        if deployment.is_running:
            return await message.reply_text(f"Deployment <b>{name}</b> is already running.", quote=True)

        await message.reply_text(f"Starting deployment <b>{name}</b>...", quote=True)
        success = self.start_process(deployment)
        if success:
            self.store.update(deployment)
            await message.reply_text(f"Deployment <b>{name}</b> started.", quote=True)
        else:
            await message.reply_text(f"Failed to start deployment <b>{name}</b>.", quote=True)

    async def stop(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text("Usage: /stop &lt;name&gt;", quote=True)

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(f"Deployment <b>{name}</b> not found.", quote=True)

        if not deployment.is_running:
            return await message.reply_text(f"Deployment <b>{name}</b> is not running.", quote=True)

        if self.stop_process(deployment):
            self.store.update(deployment)
            await message.reply_text(f"Deployment <b>{name}</b> stopped.", quote=True)
        else:
            await message.reply_text(f"Failed to stop deployment <b>{name}</b>.", quote=True)

    async def newbot(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=4)
        if len(args) < 4:
            return await message.reply_text(
                "Usage: /newbot &lt;name&gt; &lt;bot_token&gt; &lt;session_string&gt; [mongo_url]",
                quote=True,
            )

        name = normalize_name(args[1])
        bot_token = args[2].strip()
        session_string = args[3].strip()
        mongo_url = args[4].strip() if len(args) > 4 else self.config.default_mongo_url

        if not mongo_url:
            return await message.reply_text(
                "A MongoDB connection is required. Add MANAGER_DEFAULT_MONGO_URL or pass the value as the fourth argument.",
                quote=True,
            )

        if name in self.store.list():
            return await message.reply_text(f"A deployment named <b>{name}</b> already exists.", quote=True)

        await message.reply_text(f"Creating deployment <b>{name}</b>...", quote=True)

        try:
            bot_user = await self.verify_bot_token(bot_token)
        except RPCError as exc:
            return await message.reply_text(f"Failed to verify bot token: {exc}", quote=True)

        deployment_dir = self.config.deployments_dir / name
        deployment_dir.mkdir(parents=True, exist_ok=False)

        env_path = deployment_dir / ".env"
        env_vars = env_from_template(
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            bot_token=bot_token,
            mongo_url=mongo_url,
            logger_id=self.config.default_logger_id or self.config.owner_id,
            owner_id=self.config.owner_id,
            session=session_string,
            name=name,
            api_url=os.getenv("DEFAULT_API_URL", ""),
            video_api_url=os.getenv("DEFAULT_VIDEO_API_URL", ""),
        )
        env_path.write_text("\n".join(f"{key}={value}" for key, value in env_vars.items()), encoding="utf-8")

        deployment = DeploymentMeta(
            name=name,
            bot_id=bot_user.id,
            username=f"@{bot_user.username}" if bot_user.username else bot_user.first_name,
            created_at=datetime.utcnow().isoformat() + "Z",
            path=str(deployment_dir.relative_to(ROOT)),
        )

        started = self.start_process(deployment)
        if started:
            deployment.started_at = datetime.utcnow().isoformat() + "Z"
            self.store.add(deployment)
            await message.reply_text(
                f"Deployment <b>{name}</b> created and started.\n"
                f"Bot: <code>{deployment.username}</code>\n"
                f"Path: <code>{deployment.deployment_path}</code>",
                quote=True,
            )
        else:
            await message.reply_text(
                f"Deployment <b>{name}</b> created, but failed to start.",
                quote=True,
            )

    async def verify_bot_token(self, bot_token: str):
        temp = Client(
            name="verify-bot",
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            bot_token=bot_token,
        )
        await temp.start()
        try:
            return await temp.get_me()
        finally:
            await temp.stop()

    def start_process(self, deployment: DeploymentMeta) -> bool:
        env = os.environ.copy()
        deployment_env = self.load_deployment_env(deployment.deployment_path / ".env")
        env.update(deployment_env)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = str(self.config.template_path)

        log_file = deployment.deployment_path / "run.log"
        process = subprocess.Popen(
            [sys.executable, "-m", "anony"],
            cwd=self.config.template_path,
            env=env,
            stdout=log_file.open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        deployment.pid = process.pid
        return deployment.pid is not None

    def stop_process(self, deployment: DeploymentMeta) -> bool:
        if not deployment.pid:
            return False
        try:
            process = psutil.Process(deployment.pid)
            process.terminate()
            process.wait(timeout=10)
            deployment.pid = None
            return True
        except (psutil.NoSuchProcess, psutil.TimeoutExpired, psutil.AccessDenied):
            try:
                process.kill()
            except Exception:
                pass
            deployment.pid = None
            return False

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
        self.app.run()


def main() -> None:
    config = ManagerConfig.load()
    config.deployments_dir.mkdir(parents=True, exist_ok=True)
    store = DeploymentStore(STORE_PATH)
    bot_manager = BotManager(config, store)
    bot_manager.run()


if __name__ == "__main__":
    main()
