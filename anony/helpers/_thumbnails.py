# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import colorsys
import math
from pathlib import Path
import aiohttp
from PIL import (Image, ImageDraw, ImageEnhance,
                 ImageFilter, ImageFont, ImageOps)

from anony import config
from anony.helpers import Track


class Thumbnail:
    def __init__(self):
        self.fonts_dir = Path(__file__).resolve().parent
        try:
            self.fonts(1)
        except OSError:
            self.fonts_dir = None

        self.session: aiohttp.ClientSession | None = None

    def fonts(self, scale: float):
        if not self.fonts_dir:
            fallback = ImageFont.load_default()
            return fallback, fallback, fallback
        return (
            ImageFont.truetype(self.fonts_dir / "Raleway-Bold.ttf", round(48 * scale)),
            ImageFont.truetype(self.fonts_dir / "Inter-Light.ttf", round(25 * scale)),
            ImageFont.truetype(self.fonts_dir / "Raleway-Bold.ttf", round(22 * scale)),
        )

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

    @staticmethod
    def artwork_accent(source: Image.Image) -> tuple[int, int, int]:
        sample = source.convert("RGB").resize((64, 64), Image.Resampling.LANCZOS)
        palette = sample.quantize(colors=12, method=Image.Quantize.MEDIANCUT)
        candidates = palette.getcolors() or []
        palette_colors = palette.getpalette() or []
        best_rgb = (242, 193, 78)
        best_score = -1.0
        for count, index in candidates:
            rgb = tuple(palette_colors[index * 3:index * 3 + 3])
            hue, saturation, value = colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))
            if value < 0.18 or value > 0.96:
                continue
            score = count * (0.35 + saturation) * (0.45 + value)
            if score > best_score:
                best_score = score
                best_rgb = rgb
        hue, saturation, value = colorsys.rgb_to_hsv(*(channel / 255 for channel in best_rgb))
        saturation = max(0.58, min(saturation, 0.86))
        value = max(0.76, min(value, 0.94))
        return tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(hue, saturation, value))

    async def generate(self, song: Track, size=(1280, 720)) -> str:
        try:
            cache_dir = Path.cwd() / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            temp = cache_dir / f"temp_{song.id}.jpg"
            output = cache_dir / f"{song.id}_nowplaying_v5.gif"
            if output.exists():
                return str(output)

            await self.save_thumb(str(temp), song.thumbnail)
            source = Image.open(temp).convert("RGB")
            scale = size[0] / 960
            point = lambda x, y: (round(x * scale), round(y * scale))
            box = lambda left, top, right, bottom: (
                round(left * scale),
                round(top * scale),
                round(right * scale),
                round(bottom * scale),
            )
            title_font, meta_font, badge_font = self.fonts(scale)
            accent = self.artwork_accent(source)
            accent_soft = tuple(round(channel * 0.72 + 255 * 0.28) for channel in accent)
            accent_luma = sum(channel * weight for channel, weight in zip(accent, (0.299, 0.587, 0.114)))
            accent_text = (13, 18, 27, 255) if accent_luma > 150 else (255, 255, 255, 255)

            backdrop = ImageOps.fit(source, size, method=Image.Resampling.LANCZOS)
            backdrop = backdrop.filter(ImageFilter.GaussianBlur(round(38 * scale)))
            backdrop = ImageEnhance.Brightness(backdrop).enhance(0.42).convert("RGBA")

            overlay = Image.new("RGBA", size, (8, 12, 22, 105))
            base = Image.alpha_composite(backdrop, overlay)
            draw = ImageDraw.Draw(base)

            art_size = point(380, 380)
            art = ImageOps.fit(source, art_size, method=Image.Resampling.LANCZOS).convert("RGBA")
            mask = Image.new("L", art_size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, *art_size), radius=round(24 * scale), fill=255)
            art.putalpha(mask)
            base.paste(art, point(54, 80), art)

            draw.rounded_rectangle(box(468, 80, 912, 460), radius=round(25 * scale), fill=(9, 15, 27, 178))
            draw.rounded_rectangle(box(500, 108, 690, 150), radius=round(21 * scale), fill=(*accent, 245))
            draw.text(point(523, 116), "NOW PLAYING", font=badge_font, fill=accent_text)

            title_one, title_two = self.title_lines(song.title)
            channel = self.shorten(song.channel_name, 28)
            views = self.shorten(song.view_count, 18)
            draw.text(point(500, 182), title_one, font=title_font, fill=(250, 252, 255, 255))
            if title_two:
                draw.text(point(500, 236), title_two, font=title_font, fill=(250, 252, 255, 255))
            draw.text(point(500, 308), channel, font=meta_font, fill=(*accent_soft, 255))
            draw.text(point(500, 344), f"{views} views  •  {song.duration}", font=meta_font, fill=(205, 212, 224, 255))
            draw.text(
                point(500, 407),
                self.shorten(config.NAME, 28),
                font=badge_font,
                fill=(*accent_soft, 255),
            )

            palette = base.convert("P", palette=Image.Palette.ADAPTIVE, colors=256)
            frames = []
            for frame_index in range(24):
                frame = base.copy()
                frame_draw = ImageDraw.Draw(frame)
                for index in range(7):
                    wave = math.sin((frame_index / 24) * math.tau + index * 0.82)
                    height = round(31 + wave * 14)
                    left = round((770 + index * 17) * scale)
                    bottom = round(426 * scale)
                    frame_draw.rounded_rectangle(
                        (left, bottom - round(height * scale), left + round(9 * scale), bottom),
                        radius=round(4 * scale),
                        fill=(*accent_soft, 245),
                    )
                frames.append(
                    frame.convert("RGB").quantize(
                        palette=palette,
                        dither=Image.Dither.NONE,
                    )
                )

            frames[0].save(
                output,
                save_all=True,
                append_images=frames[1:],
                duration=100,
                loop=0,
                optimize=True,
                disposal=1,
            )
            try:
                temp.unlink(missing_ok=True)
            except Exception:
                pass
            return str(output)
        except Exception:
            return config.DEFAULT_THUMB
