#!/usr/bin/env python3
import asyncio
import ctypes
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.errors import RPCError
from pyrogram.types import Message, ReplyParameters
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
    api_key: Optional[str]

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
            default_logger_id=int(default_logger_id) if default_logger_id else None,
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
    return env


def handler_errors(func):
    async def wrapper(self, client: Client, message: Message):
        try:
            await func(self, client, message)
        except Exception as exc:
            logger.exception("Handler %s failed: %s", func.__name__, exc)
            await message.reply_text(
                "⚠️ An internal error occurred while processing your request. Check the manager logs for details.",
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
        self.app.on_message(filters.private & filters.command("list") & filters.user(self.config.owner_id))(self.list_bots)
        self.app.on_message(filters.private & filters.command("status") & filters.user(self.config.owner_id))(self.status)
        self.app.on_message(filters.private & filters.command("deploy") & filters.user(self.config.owner_id))(self.deploy)
        self.app.on_message(filters.private & filters.command("stop") & filters.user(self.config.owner_id))(self.stop)

    @handler_errors
    async def start(self, client: Client, message: Message) -> None:
        await message.reply_text(
            "<b>Music Bot Deployment Manager</b>\n"
            "Use /help to see available commands.",
            reply_parameters=ReplyParameters(message_id=message.id),
        )

    @handler_errors
    async def help(self, client: Client, message: Message) -> None:
        await message.reply_text(
            "<b>Manager Commands</b>\n"
            "/newbot &lt;name&gt; &lt;bot_token&gt; &lt;session_string&gt; [mongo_url] - Create and start a new deployment.\n"
            "/list - Show all deployments.\n"
            "/status &lt;name&gt; - Show deployment status.\n"
            "/deploy &lt;name&gt; - Start a stopped deployment.\n"
            "/stop &lt;name&gt; - Stop a running deployment.\n",
            reply_parameters=ReplyParameters(message_id=message.id),
        )

    @handler_errors
    async def list_bots(self, client: Client, message: Message) -> None:
        if not self.store.list():
            return await message.reply_text("No deployments found.", reply_parameters=ReplyParameters(message_id=message.id))

        lines = ["<b>Deployments</b>"]
        for name, deployment in self.store.list().items():
            status = "running" if deployment.is_running else "stopped"
            lines.append(
                f"<b>{deployment.username}</b> ({name}) — <code>{status}</code>"
            )
        await message.reply_text("\n".join(lines), reply_parameters=ReplyParameters(message_id=message.id))

    @handler_errors
    async def status(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text("Usage: /status &lt;name&gt;", reply_parameters=ReplyParameters(message_id=message.id))

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(f"Deployment <b>{name}</b> not found.", reply_parameters=ReplyParameters(message_id=message.id))

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
        await message.reply_text(text, reply_parameters=ReplyParameters(message_id=message.id))

    @handler_errors
    async def deploy(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text("Usage: /deploy &lt;name&gt;", reply_parameters=ReplyParameters(message_id=message.id))

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(f"Deployment <b>{name}</b> not found.", reply_parameters=ReplyParameters(message_id=message.id))

        if deployment.is_running:
            return await message.reply_text(f"Deployment <b>{name}</b> is already running.", reply_parameters=ReplyParameters(message_id=message.id))

        await message.reply_text(f"Starting deployment <b>{name}</b>...", reply_parameters=ReplyParameters(message_id=message.id))
        started, error = self.start_process(deployment)
        if started:
            self.store.update(deployment)
            await message.reply_text(f"Deployment <b>{name}</b> started.", reply_parameters=ReplyParameters(message_id=message.id))
        else:
            logger.error("Failed to start deployment %s: %s", name, error)
            await message.reply_text(
                f"Failed to start deployment <b>{name}</b>: {error}",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

    @handler_errors
    async def stop(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text("Usage: /stop &lt;name&gt;", reply_parameters=ReplyParameters(message_id=message.id))

        name = normalize_name(args[1])
        deployment = self.store.get(name)
        if not deployment:
            return await message.reply_text(f"Deployment <b>{name}</b> not found.", reply_parameters=ReplyParameters(message_id=message.id))

        if not deployment.is_running:
            return await message.reply_text(f"Deployment <b>{name}</b> is not running.", reply_parameters=ReplyParameters(message_id=message.id))

        stopped, error = self.stop_process(deployment)
        if stopped:
            self.store.update(deployment)
            await message.reply_text(f"Deployment <b>{name}</b> stopped.", reply_parameters=ReplyParameters(message_id=message.id))
        else:
            logger.error("Failed to stop deployment %s: %s", name, error)
            await message.reply_text(
                f"Failed to stop deployment <b>{name}</b>: {error}",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

    @handler_errors
    async def newbot(self, client: Client, message: Message) -> None:
        args = message.text.split(maxsplit=4)
        if len(args) < 4:
            return await message.reply_text(
                "Usage: /newbot &lt;name&gt; &lt;bot_token&gt; &lt;session_string&gt; [mongo_url]",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        name = normalize_name(args[1])
        bot_token = args[2].strip()
        session_string = args[3].strip()
        mongo_url = args[4].strip() if len(args) > 4 else self.config.default_mongo_url
        logger.info("Received newbot request for %s", name)

        if not mongo_url:
            logger.warning("No MongoDB URL provided for new deployment %s", name)
            return await message.reply_text(
                "A MongoDB connection is required. Add MANAGER_DEFAULT_MONGO_URL or pass the value as the fourth argument.",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

        if name in self.store.list():
            return await message.reply_text(f"A deployment named <b>{name}</b> already exists.", reply_parameters=ReplyParameters(message_id=message.id))

        await message.reply_text(f"Creating deployment <b>{name}</b>...", reply_parameters=ReplyParameters(message_id=message.id))

        try:
            bot_user = await self.verify_bot_token(bot_token)
            logger.info("Verified bot token for %s (%s)", name, bot_user.username or bot_user.first_name)
        except RPCError as exc:
            logger.error("Bot token verification failed for %s: %s", name, exc)
            return await message.reply_text(f"Failed to verify bot token: {exc}", reply_parameters=ReplyParameters(message_id=message.id))

        deployment_dir = self.config.deployments_dir / name
        deployment_dir.mkdir(parents=True, exist_ok=False)

        env_path = deployment_dir / ".env"
        manager_downloads_path = os.getenv("MANAGER_DOWNLOADS_PATH", "")
        if manager_downloads_path and not Path(manager_downloads_path).is_absolute():
            manager_downloads_path = str((ROOT / manager_downloads_path).resolve())

        env_vars = env_from_template(
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            bot_token=bot_token,
            mongo_url=mongo_url,
            logger_id=self.config.default_logger_id or self.config.owner_id,
            owner_id=self.config.owner_id,
            session=session_string,
            session_path=str(deployment_dir),
            name=name,
            api_url=os.getenv("DEFAULT_API_URL", ""),
            video_api_url=os.getenv("DEFAULT_VIDEO_API_URL", ""),
            downloads_path=manager_downloads_path,
            api_key=self.config.api_key,
        )
        env_path.write_text("\n".join(f"{key}={value}" for key, value in env_vars.items()), encoding="utf-8")

        deployment = DeploymentMeta(
            name=name,
            bot_id=bot_user.id,
            username=f"@{bot_user.username}" if bot_user.username else bot_user.first_name,
            created_at=datetime.now(timezone.utc).isoformat(),
            path=str(deployment_dir.relative_to(ROOT)),
        )

        started, error = self.start_process(deployment)
        if started:
            deployment.started_at = datetime.now(timezone.utc).isoformat()
            self.store.add(deployment)
            logger.info("Created and started deployment %s pid=%s", name, deployment.pid)
            await message.reply_text(
                f"Deployment <b>{name}</b> created and started.\n"
                f"Bot: <code>{deployment.username}</code>\n"
                f"Path: <code>{deployment.deployment_path}</code>",
                reply_parameters=ReplyParameters(message_id=message.id),
            )
        else:
            logger.error("Deployment %s creation failed to start: %s", name, error)
            await message.reply_text(
                f"Deployment <b>{name}</b> created, but failed to start: {error}",
                reply_parameters=ReplyParameters(message_id=message.id),
            )

    async def verify_bot_token(self, bot_token: str):
        logger.info("Verifying bot token with temporary client.")
        temp = Client(
            name="verify-bot",
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            bot_token=bot_token,
        )
        await temp.start()
        try:
            bot = await temp.get_me()
            logger.info("Bot token verified for %s", bot.username or bot.first_name)
            return bot
        finally:
            await temp.stop()

    def _prepare_child(self) -> None:
        os.setsid()
        try:
            PR_SET_PDEATHSIG = 1
            libc = ctypes.CDLL("libc.so.6")
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
        except Exception as exc:
            logger.debug("Unable to set parent death signal for child process: %s", exc)

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
                f"⚠️ Failed to send deployment log for <b>{deployment.name}</b>: {exc}"
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
            process = subprocess.Popen(
                [sys.executable, "-m", "anony"],
                cwd=deployment.deployment_path,
                env=env,
                stdout=log_file.open("a", encoding="utf-8"),
                stderr=subprocess.STDOUT,
                preexec_fn=self._prepare_child,
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
        logger.info("Stopping deployment %s pid=%s", deployment.name, deployment.pid)
        process = self.processes.get(deployment.name)
        try:
            if process and process.poll() is None:
                process.terminate()
            else:
                os.killpg(os.getpgid(deployment.pid), signal.SIGTERM)
        except Exception as exc:
            logger.warning("Failed graceful terminate for %s pid=%s: %s", deployment.name, deployment.pid, exc)
        try:
            if process:
                process.wait(timeout=10)
            else:
                psutil.Process(deployment.pid).wait(timeout=10)
            logger.info("Deployment %s stopped.", deployment.name)
            deployment.pid = None
            self.processes.pop(deployment.name, None)
            return True, None
        except (psutil.NoSuchProcess, subprocess.TimeoutExpired, subprocess.TimeoutExpired, psutil.AccessDenied) as exc:
            logger.warning("Graceful stop failed for %s pid=%s: %s", deployment.name, deployment.pid, exc)
            try:
                if process:
                    process.kill()
                else:
                    os.killpg(os.getpgid(deployment.pid), signal.SIGKILL)
            except Exception as exc2:
                logger.error("Failed to kill deployment %s pid=%s: %s", deployment.name, deployment.pid, exc2)
            deployment.pid = None
            self.processes.pop(deployment.name, None)
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
        self.monitor_thread.start()
        try:
            self.app.run()
        finally:
            self.shutdown_event.set()
            if self.monitor_thread.is_alive():
                self.monitor_thread.join(timeout=2)

    def _shutdown_handler(self, signum, frame):
        logger.info("Manager received signal %s, stopping deployments...", signum)
        self.shutdown_event.set()
        self.stop_all()
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

