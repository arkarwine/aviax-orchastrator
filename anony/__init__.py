# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import time
import asyncio
import logging
import re
import traceback
from logging.handlers import RotatingFileHandler


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


redaction_filter = SecretRedactionFilter()
file_handler = RotatingFileHandler("log.txt", maxBytes=10485760, backupCount=5)
stream_handler = logging.StreamHandler()
file_handler.addFilter(redaction_filter)
stream_handler.addFilter(redaction_filter)

logging.basicConfig(
    format="[%(asctime)s - %(levelname)s] - %(name)s: %(message)s",
    datefmt="%d-%b-%y %H:%M:%S",
    handlers=[
        file_handler,
        stream_handler,
    ],
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("ntgcalls").setLevel(logging.INFO)
logging.getLogger("pymongo").setLevel(logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.INFO)
logging.getLogger("pytgcalls").setLevel(logging.INFO)
logger = logging.getLogger(__name__)


__version__ = "3.0.2"

from config import Config

config = Config()
config.check()
tasks = []
boot = time.time()

from anony.core.bot import Bot
app = Bot()

from anony.core.dir import ensure_dirs
ensure_dirs()

from anony.core.userbot import Userbot
userbot = Userbot()

from anony.core.mongo import MongoDB
db = MongoDB()

from anony.core.lang import Language
lang = Language()

from anony.core.telegram import Telegram
from anony.core.youtube import YouTube
tg = Telegram()
yt = YouTube()

from anony.helpers import Queue, Thumbnail
queue = Queue()
thumb = Thumbnail()

from anony.core.calls import TgCall
anon = TgCall()


async def stop(reason: str = "graceful shutdown") -> None:
    from anony.core.health import health

    logger.info("Stopping...")
    await health.stop(reason)
    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.exceptions.CancelledError:
            pass

    await app.exit()
    await userbot.exit()
    await db.close()
    await thumb.close()
    if yt.api: await yt.api.session.close()

    logger.info("Stopped.\n")
