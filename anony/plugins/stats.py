# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import asyncio
import os
import platform
import sys
import time
from pathlib import Path

import psutil
from pyrogram import __version__, errors, filters, types
from pytgcalls import __version__ as pytgver

from anony import app, boot, config, db, lang, userbot
from anony.helpers import buttons
from anony.plugins import all_modules


def human_duration(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def system_metrics() -> dict:
    process = psutil.Process(os.getpid())
    storage = psutil.disk_usage(Path.cwd().anchor or "/")
    memory = psutil.virtual_memory()
    return {
        "process_mb": process.memory_info().rss / 1024**2,
        "memory_used_gb": memory.used / 1024**3,
        "memory_total_gb": memory.total / 1024**3,
        "memory_percent": memory.percent,
        "cpu_percent": psutil.cpu_percent(interval=0.25),
        "cpu_count": psutil.cpu_count() or 0,
        "storage_used_gb": storage.used / 1024**3,
        "storage_total_gb": storage.total / 1024**3,
        "storage_percent": storage.percent,
    }


async def collect_stats(sudo: bool) -> tuple[str, list[str]]:
    warnings = []
    chats_task = asyncio.create_task(db.get_chats())
    users_task = asyncio.create_task(db.get_users())
    metrics_task = asyncio.create_task(asyncio.to_thread(system_metrics)) if sudo else None

    chats_result, users_result = await asyncio.gather(
        chats_task,
        users_task,
        return_exceptions=True,
    )
    if isinstance(chats_result, Exception):
        warnings.append("served chat count")
        served_chats = "unavailable"
    else:
        served_chats = str(len(chats_result))
    if isinstance(users_result, Exception):
        warnings.append("served user count")
        served_users = "unavailable"
    else:
        served_users = str(len(users_result))

    text = (
        f"<b>📊 {app.name} Statistics</b>\n\n"
        "<b>🎙️ Service</b>\n"
        f"• Assistants online: <code>{len(userbot.clients)}</code>\n"
        f"• Active voice chats: <code>{len(db.active_calls)}</code>\n"
        f"• Uptime: <code>{human_duration(time.time() - boot)}</code>\n"
        f"• Auto leave: <code>{'enabled' if config.AUTO_LEAVE else 'disabled'}</code>\n"
        f"• Auto end: <code>{'enabled' if config.AUTO_END else 'disabled'}</code>\n\n"
        "<b>👥 Reach</b>\n"
        f"• Served groups: <code>{served_chats}</code>\n"
        f"• Served users: <code>{served_users}</code>\n"
        f"• Blocked chats: <code>{len(db.blacklisted)}</code>\n"
        f"• Blocked users: <code>{len(app.bl_users)}</code>"
    )

    if sudo and metrics_task:
        try:
            metrics = await metrics_task
            text += (
                "\n\n<b>🖥️ Runtime</b>\n"
                f"• CPU: <code>{metrics['cpu_percent']:.1f}%</code> "
                f"across <code>{metrics['cpu_count']}</code> cores\n"
                f"• Bot memory: <code>{metrics['process_mb']:.1f} MB</code>\n"
                f"• System memory: <code>{metrics['memory_used_gb']:.1f}/"
                f"{metrics['memory_total_gb']:.1f} GB</code> "
                f"(<code>{metrics['memory_percent']:.1f}%</code>)\n"
                f"• Storage: <code>{metrics['storage_used_gb']:.1f}/"
                f"{metrics['storage_total_gb']:.1f} GB</code> "
                f"(<code>{metrics['storage_percent']:.1f}%</code>)\n\n"
                "<b>🧩 Software</b>\n"
                f"• Modules: <code>{len(all_modules)}</code>\n"
                f"• Platform: <code>{platform.system()}</code>\n"
                f"• Python: <code>v{sys.version.split()[0]}</code>\n"
                f"• Pyrogram: <code>v{__version__}</code>\n"
                f"• PyTgCalls: <code>v{pytgver}</code>"
            )
        except Exception:
            warnings.append("system metrics")

    if warnings:
        text += (
            "\n\n⚠️ Some metrics are temporarily unavailable: "
            + ", ".join(warnings)
            + "."
        )
    text += f"\n\n🕒 Updated: <code>{time.strftime('%H:%M:%S')}</code>"
    return text, warnings


@app.on_message(filters.command(["stats"]) & ~app.bl_users)
@lang.language()
async def _stats(_, m: types.Message):
    sent = await m.reply_photo(
        photo=config.PING_IMG,
        caption="🔎 Collecting service and usage statistics...",
    )
    text, _ = await collect_stats(m.from_user.id in app.sudoers)
    await sent.edit_caption(text, reply_markup=buttons.stats_markup())


@app.on_callback_query(filters.regex(r"^stats(?: refresh| close)?$") & ~app.bl_users)
@lang.language()
async def _stats_callback(_, query: types.CallbackQuery):
    action = query.data.split(maxsplit=1)[1] if " " in query.data else "refresh"
    if action == "close":
        await query.answer()
        return await query.message.delete()

    await query.answer("Refreshing statistics...")
    try:
        await query.edit_message_caption(
            "🔄 Refreshing service, database, and system statistics..."
        )
        text, _ = await collect_stats(query.from_user.id in app.sudoers)
        await query.edit_message_caption(text, reply_markup=buttons.stats_markup())
    except errors.MessageNotModified:
        pass
