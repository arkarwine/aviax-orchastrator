from pathlib import Path
from os import getenv

from dotenv import dotenv_values, load_dotenv

ENV_PATH = Path.cwd() / ".env"
load_dotenv(dotenv_path=ENV_PATH)


def parse_bool(value, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "on", "yes", "1"}:
        return True
    if normalized in {"false", "off", "no", "0"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


class Config:
    KEYS = {
        "API_ID", "API_HASH", "NAME", "DB_NAME", "DEPLOYMENT_ID", "MANAGED_SETUP",
        "BOT_TOKEN", "MONGO_URL", "OWNER_ID", "LOGGER_ID", "DURATION_LIMIT",
        "QUEUE_LIMIT", "PLAYLIST_LIMIT", "SESSION1", "SESSION2", "SESSION3",
        "SESSION_PATH", "DOWNLOADS_PATH", "SUPPORT_CHANNEL", "SUPPORT_CHAT",
        "API_URL", "VIDEO_API_URL", "API_KEY", "AUTO_LEAVE", "AUTO_END",
        "THUMB_GEN", "VIDEO_PLAY", "LANG_CODE", "COOKIES_URL", "DEFAULT_THUMB",
        "PING_IMG", "START_IMG",
    }

    def __init__(self, values: dict | None = None):
        self._values = values

        self.API_ID = self.integer("API_ID", 0)
        self.API_HASH = self.value("API_HASH")

        self.NAME = self.value("NAME", "Aviax Music")
        self.DB_NAME = self.value("DB_NAME", self.NAME)
        self.DEPLOYMENT_ID = self.value("DEPLOYMENT_ID", "")
        self.MANAGED_SETUP = self.boolean("MANAGED_SETUP")
        self.BOT_TOKEN = self.value("BOT_TOKEN")
        self.MONGO_URL = self.value("MONGO_URL")

        self.OWNER_ID = self.integer("OWNER_ID", 0)
        self.LOGGER_ID = self.managed_int("LOGGER_ID")

        self.DURATION_LIMIT = self.integer("DURATION_LIMIT", 60) * 60
        self.QUEUE_LIMIT = self.integer("QUEUE_LIMIT", 20)
        self.PLAYLIST_LIMIT = self.integer("PLAYLIST_LIMIT", 20)

        self.SESSION1 = self.managed_value("SESSION")
        self.SESSION2 = self.managed_value("SESSION2")
        self.SESSION3 = self.managed_value("SESSION3")
        self.SESSION_PATH = self.value("SESSION_PATH")
        downloads_path = self.value("DOWNLOADS_PATH", "")
        self.DOWNLOADS_PATH = Path(downloads_path).resolve() if downloads_path else None

        self.SUPPORT_CHANNEL = self.value("SUPPORT_CHANNEL", "https://t.me/fallenx")
        self.SUPPORT_CHAT = self.value("SUPPORT_CHAT", "https://t.me/DevilsHeavenMF")

        self.API_URL = self.value("API_URL", "https://pvtz.nexgenbots.xyz")
        self.VIDEO_API_URL = self.value("VIDEO_API_URL", "https://api.video.nexgenbots.xyz")
        self.API_KEY = self.value("API_KEY") or None

        self.AUTO_LEAVE = self.boolean("AUTO_LEAVE")
        self.AUTO_END = self.boolean("AUTO_END")

        self.THUMB_GEN = self.boolean("THUMB_GEN", True)
        self.VIDEO_PLAY = self.boolean("VIDEO_PLAY", True)

        self.LANG_CODE = self.value("LANG_CODE", "en")

        self.COOKIES_URL = [
            url for url in self.value("COOKIES_URL", "").split(" ")
            if url and "batbin.me" in url
        ]
        self.DEFAULT_THUMB = self.value("DEFAULT_THUMB", "https://te.legra.ph/file/3e40a408286d4eda24191.jpg")
        self.PING_IMG = self.value("PING_IMG", "https://files.catbox.moe/haagg2.png")
        self.START_IMG = self.value("START_IMG", "https://files.catbox.moe/zvziwk.jpg")

        self._runtime_defaults = {
            "API_URL": self.API_URL,
            "VIDEO_API_URL": self.VIDEO_API_URL,
            "API_KEY": self.API_KEY,
            "AUTO_LEAVE": self.AUTO_LEAVE,
            "AUTO_END": self.AUTO_END,
            "THUMB_GEN": self.THUMB_GEN,
            "VIDEO_PLAY": self.VIDEO_PLAY,
            "LANG_CODE": self.LANG_CODE,
            "DEFAULT_THUMB": self.DEFAULT_THUMB,
            "PING_IMG": self.PING_IMG,
            "START_IMG": self.START_IMG,
            "COOKIES_URL": self.COOKIES_URL,
            "DOWNLOADS_PATH": self.DOWNLOADS_PATH,
        }
        del self._values

    @classmethod
    def from_disk(cls, path: Path | None = None) -> "Config":
        path = path or ENV_PATH
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {path}")
        values = dict(dotenv_values(path))
        if "SESSION" in values:
            values["SESSION1"] = values["SESSION"]
        return cls(values)

    def value(self, key: str, default=None):
        if self._values is None:
            return getenv(key, default)
        value = self._values.get(key, default)
        return default if value is None else value

    def integer(self, key: str, default: int = 0) -> int:
        value = self.value(key, default)
        return int(value or 0)

    def boolean(self, key: str, default: bool = False) -> bool:
        return parse_bool(self.value(key), default)

    def managed_value(self, key: str):
        key = "SESSION1" if key == "SESSION" else key
        return None if self.MANAGED_SETUP else (self.value(key) or None)

    def managed_int(self, key: str) -> int:
        return 0 if self.MANAGED_SETUP else self.integer(key, 0)

    def snapshot(self) -> dict:
        return {key: getattr(self, key) for key in self.KEYS}

    def check(self):
        missing = [
            var
            for var in ["API_ID", "API_HASH", "BOT_TOKEN", "MONGO_URL", "NAME"]
            if not getattr(self, var)
        ]
        if missing:
            raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")

    def apply_runtime_config(self, config_values: dict) -> None:
        for key, value in config_values.items():
            if key == "DOWNLOADS_PATH":
                self.DOWNLOADS_PATH = Path(value).resolve() if value else None
            elif key == "COOKIES_URL":
                if isinstance(value, str):
                    self.COOKIES_URL = [
                        url for url in value.split(" ") if url and "batbin.me" in url
                    ]
                else:
                    self.COOKIES_URL = value or []
            elif key in {"AUTO_LEAVE", "AUTO_END", "THUMB_GEN", "VIDEO_PLAY"}:
                self.__dict__[key] = parse_bool(value)
            elif key == "API_KEY":
                self.API_KEY = value if value else None
            elif key in {"LOGGER_ID", "OWNER_ID"}:
                self.__dict__[key] = int(value or 0)
            elif key in {"QUEUE_LIMIT", "PLAYLIST_LIMIT", "DURATION_LIMIT"}:
                self.__dict__[key] = int(value)
            elif hasattr(self, key):
                setattr(self, key, value)

    def reset_runtime_config(self, key: str) -> None:
        if key in self._runtime_defaults:
            default_value = self._runtime_defaults[key]
            setattr(self, key, default_value)
