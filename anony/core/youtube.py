# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import os
import re
import yt_dlp
import random
import asyncio
import aiohttp
from pathlib import Path

from py_yt import Playlist, VideosSearch

from anony import config, logger
from anony.helpers import NexGenApi, Track, utils


class YouTube:
    def __init__(self):
        self.api = None
        self.base = "https://www.youtube.com/watch?v="
        self.cookies = []
        self.checked = False
        self.default_cookie_dir = Path(__file__).resolve().parent.parent / "cookies"
        self.cookie_dir = Path(config.COOKIES_PATH) if config.COOKIES_PATH else self.default_cookie_dir
        self.warned = False
        self.regex = re.compile(
            r"(https?://)?(www\.|m\.|music\.)?"
            r"(youtube\.com/(watch\?v=|shorts/|playlist\?list=)|youtu\.be/)"
            r"([A-Za-z0-9_-]{11}|PL[A-Za-z0-9_-]+)([&?][^\s]*)?"
        )
        self.iregex = re.compile(
            r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)"
            r"(?!/(watch\?v=[A-Za-z0-9_-]{11}|shorts/[A-Za-z0-9_-]{11}"
            r"|playlist\?list=PL[A-Za-z0-9_-]+|[A-Za-z0-9_-]{11}))\S*"
        )
        if config.API_URL and config.VIDEO_API_URL and config.API_KEY:
            self.api = NexGenApi(
                config.API_URL,
                config.API_KEY,
                config.VIDEO_API_URL
            )

    def refresh_cookie_dir(self) -> None:
        cookie_dir = Path(config.COOKIES_PATH) if config.COOKIES_PATH else self.default_cookie_dir
        if cookie_dir != self.cookie_dir:
            self.cookie_dir = cookie_dir
            self.cookies.clear()
            self.checked = False
            self.warned = False

    def get_cookies(self):
        self.refresh_cookie_dir()
        if not self.checked:
            if self.cookie_dir.exists():
                for file in self.cookie_dir.iterdir():
                    if file.suffix == ".txt":
                        self.cookies.append(str(file))
            self.checked = True
        if not self.cookies:
            if not self.warned:
                self.warned = True
                logger.warning("Cookies are missing; downloads might fail.")
            return None
        return random.choice(self.cookies)

    async def save_cookies(self, urls: list[str]) -> None:
        self.refresh_cookie_dir()
        logger.info("Saving cookies from urls...")
        self.cookie_dir.mkdir(parents=True, exist_ok=True)
        async with aiohttp.ClientSession() as session:
            for url in urls:
                name = url.split("/")[-1]
                link = "https://batbin.me/raw/" + name
                async with session.get(link) as resp:
                    resp.raise_for_status()
                    path = self.cookie_dir / f"{name}.txt"
                    temporary = path.with_suffix(".txt.tmp")
                    with open(temporary, "wb") as fw:
                        fw.write(await resp.read())
                    temporary.replace(path)
        logger.info(f"Cookies saved in %s.", self.cookie_dir)

    def valid(self, url: str) -> bool:
        return bool(re.match(self.regex, url))

    def invalid(self, url: str) -> bool:
        return bool(re.match(self.iregex, url))

    async def search(self, query: str, m_id: int, video: bool = False) -> Track | None:
        try:
            _search = VideosSearch(query, limit=1, with_live=False)
            results = await _search.next()
        except Exception:
            return None
        if results and results["result"]:
            data = results["result"][0]
            return Track(
                id=data.get("id"),
                channel_name=data.get("channel", {}).get("name"),
                duration=data.get("duration"),
                duration_sec=utils.to_seconds(data.get("duration")),
                message_id=m_id,
                title=data.get("title")[:25],
                thumbnail=data.get("thumbnails", [{}])[-1].get("url").split("?")[0],
                url=data.get("link"),
                view_count=data.get("viewCount", {}).get("short"),
                video=video,
            )
        return None

    async def playlist(self, limit: int, user: str, url: str, video: bool) -> list[Track | None]:
        tracks = []
        try:
            plist = await Playlist.get(url)
            for data in plist["videos"][:limit]:
                track = Track(
                    id=data.get("id"),
                    channel_name=data.get("channel", {}).get("name", ""),
                    duration=data.get("duration"),
                    duration_sec=utils.to_seconds(data.get("duration")),
                    title=data.get("title")[:25],
                    thumbnail=data.get("thumbnails")[-1].get("url").split("?")[0],
                    url=data.get("link").split("&list=")[0],
                    user=user,
                    view_count="",
                    video=video,
                )
                tracks.append(track)
        except Exception:
            pass
        return tracks

    async def download(self, video_id: str, video: bool = False) -> str | None:
        if self.api:
            if file_path := await self.api.download(video_id, video):
                return file_path
            logger.warning("NexGen API download unavailable for %s; falling back to yt-dlp.", video_id)

        url = self.base + video_id
        ext = "mp4" if video else "webm"
        downloads_dir = Path(config.DOWNLOADS_PATH) if config.DOWNLOADS_PATH else Path.cwd() / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        filename = str(downloads_dir / f"{video_id}.{ext}")

        if Path(filename).exists():
            return filename
        if not video:
            cached = next(
                (
                    path for path in downloads_dir.glob(f"{video_id}.*")
                    if path.suffix.lower() in {".webm", ".m4a", ".mp3", ".opus", ".ogg"}
                ),
                None,
            )
            if cached:
                return str(cached)

        cookie = self.get_cookies()
        base_opts = {
            "outtmpl": str(downloads_dir / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "geo_bypass": True,
            "overwrites": False,
            "nocheckcertificate": True,
            "cookiefile": cookie,
            "remote_components": {"ejs:github": {}},
            "js_runtimes": {"deno": {}},
        }

        if video:
            ydl_opts = {
                **base_opts,
                "format": "bestvideo[height<=?720][width<=?1280][ext=mp4]+bestaudio/bestvideo[height<=?720]+bestaudio/best[ext=mp4]/best",
                "merge_output_format": "mp4",
            }
        else:
            ydl_opts = {
                **base_opts,
                "format": "bestaudio[ext=webm][acodec=opus]/bestaudio[ext=webm]/bestaudio/best",
            }

        script = f"""
import yt_dlp
from pathlib import Path

ydl_opts = {repr(ydl_opts)}

with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    info = ydl.extract_info({repr(url)}, download=True)
    requested = info.get("requested_downloads") or []
    for item in requested:
        filepath = item.get("filepath")
        if filepath and Path(filepath).exists():
            print(filepath)
            raise SystemExit(0)
    filepath = info.get("filepath") or ydl.prepare_filename(info)
    if filepath and Path(filepath).exists():
        print(filepath)
        raise SystemExit(0)
raise SystemExit(1)
"""

        def _download():
            import subprocess, sys
            try:
                result = subprocess.run(
                    [sys.executable, "-c", script],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode == 0:
                    filepath = result.stdout.strip()
                    if filepath and Path(filepath).exists():
                        return filepath
                if result.stderr:
                    logger.warning("yt-dlp subprocess error for %s: %s", video_id, result.stderr[-500:])
            except subprocess.TimeoutExpired:
                logger.warning("yt-dlp download timed out for %s", video_id)
            except Exception as ex:
                logger.warning("Download subprocess failed: %s", ex)

            if Path(filename).exists():
                return filename
            cached = next(downloads_dir.glob(f"{video_id}.*"), None)
            return str(cached) if cached else None

        return await asyncio.to_thread(_download)