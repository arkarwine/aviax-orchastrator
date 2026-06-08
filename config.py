from os import getenv
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path.cwd() / ".env")


def env_bool(key: str, default: str = "False") -> bool:
    return getenv(key, default).lower() == "true"


class Config:
    def __init__(self):
        self.API_ID = int(getenv("API_ID", 0))
        self.API_HASH = getenv("API_HASH")

        self.NAME = getenv("NAME", "Aviax Music")
        self.DB_NAME = getenv("DB_NAME", self.NAME)
        self.DEPLOYMENT_ID = getenv("DEPLOYMENT_ID", "")
        self.MANAGED_SETUP = env_bool("MANAGED_SETUP")
        self.BOT_TOKEN = getenv("BOT_TOKEN")
        self.MONGO_URL = getenv("MONGO_URL")

        self.OWNER_ID = int(getenv("OWNER_ID", 0))
        self.LOGGER_ID = self.managed_int("LOGGER_ID")

        self.DURATION_LIMIT = int(getenv("DURATION_LIMIT", 60)) * 60
        self.QUEUE_LIMIT = int(getenv("QUEUE_LIMIT", 20))
        self.PLAYLIST_LIMIT = int(getenv("PLAYLIST_LIMIT", 20))

        self.SESSION1 = self.managed_value("SESSION")
        self.SESSION2 = self.managed_value("SESSION2")
        self.SESSION3 = self.managed_value("SESSION3")
        self.SESSION_PATH = getenv("SESSION_PATH", None)
        downloads_path = getenv("DOWNLOADS_PATH", "")
        self.DOWNLOADS_PATH = Path(downloads_path).resolve() if downloads_path else None

        self.SUPPORT_CHANNEL = getenv("SUPPORT_CHANNEL", "https://t.me/fallenx")
        self.SUPPORT_CHAT = getenv("SUPPORT_CHAT", "https://t.me/DevilsHeavenMF")

        self.API_URL = getenv("API_URL", "https://pvtz.nexgenbots.xyz")
        self.VIDEO_API_URL = getenv("VIDEO_API_URL", "https://api.video.nexgenbots.xyz")
        self.API_KEY = getenv("API_KEY", None) # Get this value from https://console.nexgenbots.xyz

        self.AUTO_LEAVE: bool = env_bool("AUTO_LEAVE")
        self.AUTO_END: bool = env_bool("AUTO_END")

        self.THUMB_GEN: bool = env_bool("THUMB_GEN", "True")
        self.VIDEO_PLAY: bool = env_bool("VIDEO_PLAY", "True")

        self.LANG_CODE = getenv("LANG_CODE", "en")

        self.COOKIES_URL = [
            url for url in getenv("COOKIES_URL", "").split(" ")
            if url and "batbin.me" in url
        ]
        self.DEFAULT_THUMB = getenv("DEFAULT_THUMB", "https://te.legra.ph/file/3e40a408286d4eda24191.jpg")
        self.PING_IMG = getenv("PING_IMG", "https://files.catbox.moe/haagg2.png")
        self.START_IMG = getenv("START_IMG", "https://files.catbox.moe/zvziwk.jpg")

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

    def managed_value(self, key: str):
        return None if self.MANAGED_SETUP else getenv(key, None)

    def managed_int(self, key: str) -> int:
        return 0 if self.MANAGED_SETUP else int(getenv(key, 0))

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
                self.__dict__[key] = bool(value)
            elif key == "API_KEY":
                self.API_KEY = value if value else None
            elif key in {"LOGGER_ID", "OWNER_ID"}:
                self.__dict__[key] = int(value or 0)
            elif hasattr(self, key):
                setattr(self, key, value)

    def reset_runtime_config(self, key: str) -> None:
        if key in self._runtime_defaults:
            default_value = self._runtime_defaults[key]
            setattr(self, key, default_value)
