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

    async def generate(self, song: Track, size=(960, 540)) -> str:
        try:
            cache_dir = Path.cwd() / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            temp = cache_dir / f"temp_{song.id}.jpg"
            output = cache_dir / f"{song.id}_nowplaying_v3.gif"
            if output.exists():
                return str(output)

            await self.save_thumb(str(temp), song.thumbnail)
            source = Image.open(temp).convert("RGB")
            backdrop = ImageOps.fit(source, size, method=Image.Resampling.LANCZOS)
            backdrop = backdrop.filter(ImageFilter.GaussianBlur(32))
            backdrop = ImageEnhance.Brightness(backdrop).enhance(0.48).convert("RGBA")

            overlay = Image.new("RGBA", size, (8, 12, 22, 105))
            base = Image.alpha_composite(backdrop, overlay)
            draw = ImageDraw.Draw(base)

            art_size = (380, 380)
            art = ImageOps.fit(source, art_size, method=Image.Resampling.LANCZOS).convert("RGBA")
            mask = Image.new("L", art_size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, *art_size), radius=24, fill=255)
            art.putalpha(mask)
            base.paste(art, (54, 80), art)

            draw.rounded_rectangle((468, 80, 912, 460), radius=25, fill=(9, 15, 27, 178))
            draw.rounded_rectangle((500, 108, 690, 150), radius=21, fill=(242, 193, 78, 245))
            draw.text((523, 116), "NOW PLAYING", font=self.badge_font, fill=(35, 27, 8, 255))

            title_one, title_two = self.title_lines(song.title)
            channel = self.shorten(song.channel_name, 28)
            views = self.shorten(song.view_count, 18)
            draw.text((500, 182), title_one, font=self.title_font, fill=(250, 252, 255, 255))
            if title_two:
                draw.text((500, 236), title_two, font=self.title_font, fill=(250, 252, 255, 255))
            draw.text((500, 308), channel, font=self.meta_font, fill=(139, 218, 255, 255))
            draw.text((500, 344), f"{views} views  •  {song.duration}", font=self.meta_font, fill=(205, 212, 224, 255))
            draw.text(
                (500, 407),
                self.shorten(config.NAME, 28),
                font=self.badge_font,
                fill=(255, 215, 116, 255),
            )

            patterns = (
                (18, 34, 22, 46, 28, 38, 16),
                (30, 18, 42, 24, 48, 20, 34),
                (22, 45, 28, 38, 18, 46, 26),
                (42, 25, 18, 48, 30, 22, 40),
                (26, 38, 46, 20, 34, 44, 18),
                (18, 28, 36, 48, 22, 32, 44),
                (38, 44, 20, 30, 46, 26, 18),
                (28, 20, 48, 34, 24, 42, 30),
            )
            frames = []
            for heights in patterns:
                frame = base.copy()
                frame_draw = ImageDraw.Draw(frame)
                for index, height in enumerate(heights):
                    left = 770 + index * 17
                    frame_draw.rounded_rectangle(
                        (left, 426 - height, left + 9, 426),
                        radius=4,
                        fill=(255, 218, 123, 245),
                    )
                frames.append(frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=128))

            frames[0].save(
                output,
                save_all=True,
                append_images=frames[1:],
                duration=140,
                loop=0,
                optimize=True,
                disposal=2,
            )
            try:
                temp.unlink(missing_ok=True)
            except Exception:
                pass
            return str(output)
        except Exception:
            return config.DEFAULT_THUMB
