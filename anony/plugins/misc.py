# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import time
import asyncio

from pyrogram import enums, errors, filters, types

from anony import anon, app, config, db, lang, queue, tasks, userbot, yt
from anony.helpers import buttons


optional_tasks = {}


def _start_optional_task(name: str, coroutine) -> None:
    task = asyncio.create_task(coroutine)
    optional_tasks[name] = task
    tasks.append(task)


async def sync_optional_tasks() -> None:
    desired = {
        "auto_end": (config.AUTO_END, vc_watcher),
        "auto_leave": (config.AUTO_LEAVE, auto_leave),
    }
    for name, (enabled, factory) in desired.items():
        task = optional_tasks.get(name)
        if enabled and (not task or task.done()):
            if task in tasks:
                tasks.remove(task)
            _start_optional_task(name, factory())
        elif not enabled and task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            optional_tasks.pop(name, None)
            if task in tasks:
                tasks.remove(task)


@app.on_message(filters.video_chat_started, group=19)
@app.on_message(filters.video_chat_ended, group=20)
async def _watcher_vc(_, m: types.Message):
    await anon.stop(m.chat.id)


async def auto_leave():
    while True:
        await asyncio.sleep(3600)
        for ub in userbot.clients:
            try:
                chats = [dialog.chat.id async for dialog in ub.get_dialogs()
                            if dialog.chat.type in [
                                enums.ChatType.GROUP, enums.ChatType.SUPERGROUP,
                            ]][-20:]
                for chat in chats:
                    if chat in [app.logger, -1001686672798, -1001549206010]:
                        continue
                    if chat in db.active_calls:
                        continue
                    await ub.leave_chat(chat)
                    await asyncio.sleep(7)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue


async def track_time():
    while True:
        await asyncio.sleep(1)
        for chat_id in list(db.active_calls):
            if not await db.playing(chat_id):
                continue
            media = queue.get_current(chat_id)
            if not media:
                continue
            media.time += 1


async def update_timer(length=10):
    while True:
        await asyncio.sleep(7)
        for chat_id in list(db.active_calls):
            if not await db.playing(chat_id):
                continue
            try:
                media = queue.get_current(chat_id)
                duration, message_id = media.duration_sec, media.message_id
                if not duration or not message_id or not media.time:
                    continue
                played = media.time
                remaining = duration - played
                pos = min(int((played / duration) * length), length - 1)
                timer = "—" * pos + "◉" + "—" * (length - pos - 1)
                next_ready = False

                if remaining <= 30:
                    next = queue.get_next(chat_id, check=True)
                    if next and not next.file_path:
                        next.file_path = await yt.download(next.id, video=next.video)
                    next_ready = bool(next and next.file_path)

                if remaining < 10:
                    remove = True
                else:
                    if config.THUMB_GEN:
                        timer = f"{time.strftime('%M:%S', time.gmtime(played))} | {timer} | -{time.strftime('%M:%S', time.gmtime(remaining))}"
                        if next_ready:
                            timer = f"✅ Next ready • {timer}"
                    else:
                        timer = None
                    remove = False

                if not timer and not remove:
                    continue

                await app.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=buttons.controls(
                        chat_id=chat_id, timer=timer, remove=remove
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                pass


async def vc_watcher(sleep=15):
    while True:
        await asyncio.sleep(sleep)
        for chat_id in list(db.active_calls):
            client = await db.get_assistant(chat_id)
            media = queue.get_current(chat_id)
            participants = await client.get_participants(chat_id)
            if len(participants) < 2 and media.time > 30:
                _lang = await lang.get_lang(chat_id)
                try:
                    sent = await app.edit_message_reply_markup(
                        chat_id=chat_id,
                        message_id=media.message_id,
                        reply_markup=buttons.controls(
                            chat_id=chat_id, status=_lang["stopped"], remove=True
                        ),
                    )
                    await anon.stop(chat_id)
                    await sent.reply_text(_lang["auto_left"])
                except errors.MessageIdInvalid:
                    pass


if config.AUTO_END:
    _start_optional_task("auto_end", vc_watcher())
if config.AUTO_LEAVE:
    _start_optional_task("auto_leave", auto_leave())
tasks.append(asyncio.create_task(track_time()))
tasks.append(asyncio.create_task(update_timer()))
