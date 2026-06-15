# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import os
from pathlib import Path
import aiohttp
from PIL import (Image, ImageDraw, ImageEnhance,
                 ImageFilter, ImageFont, ImageOps)

from anony import config
from anony.helpers import Track


class Thumbnail:
    def __init__(self):
        fonts_dir = Path(__file__).resolve().parent
        try:
            self.title_font = ImageFont.truetype(fonts_dir / "Raleway-Bold.ttf", 48)
            self.meta_font = ImageFont.truetype(fonts_dir / "Inter-Light.ttf", 25)
            self.badge_font = ImageFont.truetype(fonts_dir / "Raleway-Bold.ttf", 22)
            self.time_font = ImageFont.truetype(fonts_dir / "Raleway-Bold.ttf", 24)
        except OSError:
            self.title_font = self.meta_font = self.badge_font = self.time_font = ImageFont.load_default()

        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession()
    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def save_thumb(self, output_path: str, url: str) -> str:
        async with self.session.get(url) as resp:
            resp.raise_for_status()
            with open(output_path, "wb") as f: f.write(await resp.read())
        return output_path

    @staticmethod
    def shorten(text: str | None, limit: int) -> str:
        text = text or "Unknown"
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    @classmethod
    def title_lines(cls, text: str | None) -> tuple[str, str]:
        words = (text or "Unknown").split()
        first, second = [], []
        for word in words:
            target = first if len(" ".join(first + [word])) <= 21 else second
            target.append(word)
        return cls.shorten(" ".join(first), 23), cls.shorten(" ".join(second), 23)

    async def generate(self, song: Track, size=(1280, 720)) -> str:
        try:
            cache_dir = Path.cwd() / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            temp = cache_dir / f"temp_{song.id}.jpg"
            output = cache_dir / f"{song.id}_nowplaying_v2.png"
            if output.exists():
                return str(output)

            await self.save_thumb(str(temp), song.thumbnail)
            source = Image.open(temp).convert("RGB")
            backdrop = ImageOps.fit(source, size, method=Image.Resampling.LANCZOS)
            backdrop = backdrop.filter(ImageFilter.GaussianBlur(42))
            backdrop = ImageEnhance.Brightness(backdrop).enhance(0.22).convert("RGBA")

            overlay = Image.new("RGBA", size, (10, 14, 24, 185))
            image = Image.alpha_composite(backdrop, overlay)
            draw = ImageDraw.Draw(image)

            art_size = (500, 500)
            art = ImageOps.fit(source, art_size, method=Image.Resampling.LANCZOS).convert("RGBA")
            mask = Image.new("L", art_size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, *art_size), radius=28, fill=255)
            art.putalpha(mask)
            draw.rounded_rectangle((54, 106, 570, 622), radius=34, fill=(0, 0, 0, 95))
            image.paste(art, (62, 98), art)

            draw.rounded_rectangle((620, 100, 1215, 620), radius=28, fill=(14, 20, 32, 225))
            draw.rounded_rectangle((660, 140, 865, 184), radius=22, fill=(27, 194, 125, 255))
            draw.text((687, 149), "NOW PLAYING", font=self.badge_font, fill=(5, 30, 22, 255))

            title_one, title_two = self.title_lines(song.title)
            channel = self.shorten(song.channel_name, 28)
            views = self.shorten(song.view_count, 18)
            draw.text((660, 222), title_one, font=self.title_font, fill=(250, 252, 255, 255))
            if title_two:
                draw.text((660, 278), title_two, font=self.title_font, fill=(250, 252, 255, 255))
            draw.text((660, 354), channel, font=self.meta_font, fill=(129, 211, 255, 255))
            draw.text((660, 394), f"{views} views", font=self.meta_font, fill=(178, 187, 204, 255))

            draw.rounded_rectangle((660, 472, 1165, 480), radius=4, fill=(76, 88, 108, 255))
            draw.rounded_rectangle((660, 472, 715, 480), radius=4, fill=(38, 211, 142, 255))
            draw.ellipse((704, 463, 722, 489), fill=(245, 249, 255, 255))
            draw.text((660, 504), "0:01", font=self.time_font, fill=(213, 220, 232, 255))
            duration_width = draw.textbbox((0, 0), song.duration, font=self.time_font)[2]
            draw.text((1165 - duration_width, 504), song.duration, font=self.time_font, fill=(213, 220, 232, 255))
            draw.text((660, 562), "AVIAX  •  MUSIC", font=self.badge_font, fill=(255, 203, 92, 255))

            image.save(output)
            try:
                temp.unlink(missing_ok=True)
            except Exception:
                pass
            return str(output)
        except Exception:
            return config.DEFAULT_THUMB
