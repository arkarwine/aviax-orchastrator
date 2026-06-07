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
        self.rect = (914, 514)
        self.fill = (255, 255, 255)
        self.mask = Image.new("L", self.rect, 0)

        fonts_dir = Path(__file__).resolve().parent
        try:
            self.font1 = ImageFont.truetype(fonts_dir / "Raleway-Bold.ttf", 30)
            self.font2 = ImageFont.truetype(fonts_dir / "Inter-Light.ttf", 30)
        except OSError:
            self.font1 = ImageFont.load_default()
            self.font2 = ImageFont.load_default()

        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession()
    async def close(self) -> None:
        await self.session.close()

    async def save_thumb(self, output_path: str, url: str) -> str:
        async with self.session.get(url) as resp:
            with open(output_path, "wb") as f: f.write(await resp.read())
        return output_path

    async def generate(self, song: Track, size=(1280, 720)) -> str:
        try:
            cache_dir = Path.cwd() / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            temp = cache_dir / f"temp_{song.id}.jpg"
            output = cache_dir / f"{song.id}.png"
            if output.exists():
                return str(output)

            await self.save_thumb(str(temp), song.thumbnail)
            thumb = Image.open(temp).convert("RGBA").resize(
                size, Image.Resampling.LANCZOS,
            )
            blur = thumb.filter(ImageFilter.GaussianBlur(25))
            image = ImageEnhance.Brightness(blur).enhance(.40)

            _rect = ImageOps.fit(
                thumb, self.rect,
                method=Image.LANCZOS, centering=(0.5, 0.5),
            )
            ImageDraw.Draw(self.mask).rounded_rectangle(
                (0, 0, self.rect[0], self.rect[1]),
                radius=15,
                fill=255,
            )
            _rect.putalpha(self.mask)
            image.paste(_rect, (183, 30), _rect)

            draw = ImageDraw.Draw(image)
            draw.text(
                xy=(50, 560),
                text=f"{song.channel_name[:25]} | {song.view_count}",
                font=self.font2, fill=self.fill,
            )
            draw.text((50, 600), song.title[:50], font=self.font1, fill=self.fill)
            draw.text((40, 650), "0:01", font=self.font1)
            draw.line([(140, 670), (1160, 670)], fill=self.fill, width=5, joint="curve")
            draw.text((1185, 650), song.duration, font=self.font1, fill=self.fill)

            image.save(output)
            try:
                temp.unlink(missing_ok=True)
            except Exception:
                pass
            return str(output)
        except Exception:
            config.DEFAULT_THUMB
