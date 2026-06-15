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
        return cls.shorten(" ".join(first), 23), cls.shorten(" ".join(second), 23) if second else ""

    @staticmethod
    def fitted_title_lines(
        text: str | None,
        font: ImageFont.ImageFont,
        max_width: int,
    ) -> tuple[str, str]:
        words = (text or "Unknown").split()
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if not current or font.getlength(candidate) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        truncated = len(lines) > 2
        if len(lines) > 2:
            lines[1] = " ".join(lines[1:])
            lines = lines[:2]
        while lines and font.getlength(lines[-1]) > max_width:
            lines[-1] = lines[-1][:-1].rstrip()
            truncated = True
        if truncated and lines and font.getlength(lines[-1] + "…") <= max_width:
            lines[-1] += "…"
        return lines[0] if lines else "Unknown", lines[1] if len(lines) > 1 else ""

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

    @staticmethod
    def complementary_accent(accent: tuple[int, int, int]) -> tuple[int, int, int]:
        hue, saturation, value = colorsys.rgb_to_hsv(*(channel / 255 for channel in accent))
        hue = (hue + 0.42) % 1
        saturation = max(0.48, min(saturation * 0.82, 0.76))
        value = max(0.78, min(value * 1.08, 0.96))
        return tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(hue, saturation, value))

    @staticmethod
    def animated_art(
        source: Image.Image,
        art_size: tuple[int, int],
        radius: int,
        phase: float,
        accent: tuple[int, int, int],
    ) -> Image.Image:
        width, height = art_size
        breathing = (1 - math.cos(phase * math.tau)) / 2
        zoom = 1.0 + breathing * 0.014
        drift_x = round(math.sin(phase * math.tau) * width * 0.006)
        drift_y = round(math.sin(phase * math.tau + math.pi / 2) * height * 0.004)
        fitted = ImageOps.fit(
            source,
            (round(width * zoom), round(height * zoom)),
            method=Image.Resampling.LANCZOS,
        )
        left = max(0, (fitted.width - width) // 2 + drift_x)
        top = max(0, (fitted.height - height) // 2 + drift_y)
        left = min(left, fitted.width - width)
        top = min(top, fitted.height - height)
        art = fitted.crop((left, top, left + width, top + height)).convert("RGBA")
        art = ImageEnhance.Sharpness(art).enhance(1.1)

        sweep = Image.new("RGBA", art_size, (0, 0, 0, 0))
        sweep_draw = ImageDraw.Draw(sweep)
        sweep_width = max(14, round(width * 0.09))
        sweep_center = round((-sweep_width * 2) + phase * (width + sweep_width * 4))
        for offset in range(-sweep_width, sweep_width + 1):
            alpha = round(22 * (1 - abs(offset) / (sweep_width + 1)))
            x = sweep_center + offset
            sweep_draw.line(
                (x - round(height * 0.2), 0, x + round(height * 0.2), height),
                fill=(*accent, alpha),
                width=2,
            )
        art = Image.alpha_composite(art, sweep)

        mask = Image.new("L", art_size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, width, height), radius=radius, fill=255)
        art.putalpha(mask)
        return art

    @staticmethod
    def draw_waveform(
        draw: ImageDraw.ImageDraw,
        origin: tuple[int, int],
        width: int,
        phase: float,
        primary: tuple[int, int, int],
        secondary: tuple[int, int, int],
        scale: float,
    ) -> None:
        left, center = origin
        bars = 22
        gap = width / (bars - 1)
        max_height = round(25 * scale)
        bar_width = max(3, round(4 * scale))
        for index in range(bars):
            envelope = math.sin(math.pi * index / (bars - 1)) ** 0.7
            motion = (
                math.sin(phase * math.tau + index * 0.52)
                + 0.42 * math.sin(phase * math.tau * 2 - index * 0.31)
            ) / 1.42
            eased = (motion + 1) / 2
            height = max(round(3 * scale), round((5 * scale + eased * max_height) * envelope))
            x = round(left + index * gap)
            mix = index / (bars - 1)
            color = tuple(
                round(primary[channel] * (1 - mix) + secondary[channel] * mix)
                for channel in range(3)
            )
            draw.rounded_rectangle(
                (x, center - height, x + bar_width, center + height),
                radius=bar_width // 2,
                fill=(*color, 235),
            )

    async def generate(self, song: Track, size=(1280, 720)) -> str:
        try:
            cache_dir = Path.cwd() / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            temp = cache_dir / f"temp_{song.id}.jpg"
            output = cache_dir / f"{song.id}_nowplaying_v6.gif"
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
            complement = self.complementary_accent(accent)
            accent_soft = tuple(round(channel * 0.72 + 255 * 0.28) for channel in accent)
            complement_soft = tuple(round(channel * 0.72 + 255 * 0.28) for channel in complement)
            accent_luma = sum(channel * weight for channel, weight in zip(accent, (0.299, 0.587, 0.114)))
            accent_text = (13, 18, 27, 255) if accent_luma > 150 else (255, 255, 255, 255)

            backdrop = ImageOps.fit(source, size, method=Image.Resampling.LANCZOS)
            backdrop = backdrop.filter(ImageFilter.GaussianBlur(round(38 * scale)))
            backdrop = ImageEnhance.Brightness(backdrop).enhance(0.42).convert("RGBA")

            overlay = Image.new("RGBA", size, (8, 12, 22, 105))
            base = Image.alpha_composite(backdrop, overlay)
            draw = ImageDraw.Draw(base)

            art_size = point(380, 380)
            draw.rounded_rectangle(box(468, 80, 912, 460), radius=round(25 * scale), fill=(9, 15, 27, 178))
            draw.rounded_rectangle(
                box(468, 80, 912, 460),
                radius=round(25 * scale),
                outline=(*complement, 95),
                width=max(1, round(scale)),
            )
            draw.rounded_rectangle(box(500, 108, 690, 150), radius=round(21 * scale), fill=(*accent, 245))
            draw.text(point(523, 116), "NOW PLAYING", font=badge_font, fill=accent_text)

            title_one, title_two = self.fitted_title_lines(
                song.title,
                title_font,
                round(380 * scale),
            )
            channel = self.shorten(song.channel_name, 28)
            views = self.shorten(song.view_count, 18)
            draw.text(point(500, 182), title_one, font=title_font, fill=(250, 252, 255, 255))
            if title_two:
                draw.text(point(500, 236), title_two, font=title_font, fill=(250, 252, 255, 255))
            draw.text(point(500, 308), channel, font=meta_font, fill=(*complement_soft, 255))
            draw.text(point(500, 344), f"{views} views  •  {song.duration}", font=meta_font, fill=(205, 212, 224, 255))
            draw.text(
                point(500, 420),
                self.shorten(config.NAME, 28),
                font=badge_font,
                fill=(*accent_soft, 255),
            )

            preview = base.copy()
            preview_art = self.animated_art(
                source,
                art_size,
                round(24 * scale),
                0,
                complement_soft,
            )
            preview.paste(preview_art, point(54, 80), preview_art)
            palette = preview.convert("P", palette=Image.Palette.ADAPTIVE, colors=256)
            frames = []
            frame_count = 72
            for frame_index in range(frame_count):
                phase = frame_index / frame_count
                frame = base.copy()
                art = self.animated_art(
                    source,
                    art_size,
                    round(24 * scale),
                    phase,
                    complement_soft,
                )
                frame.paste(art, point(54, 80), art)
                frame_draw = ImageDraw.Draw(frame)
                self.draw_waveform(
                    frame_draw,
                    point(690, 393),
                    round(170 * scale),
                    phase,
                    accent_soft,
                    complement_soft,
                    scale,
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
                duration=50,
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
