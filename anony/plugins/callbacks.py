# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import re

from pyrogram import enums, errors, filters, types

from anony import anon, app, config, db, lang, queue, tg, yt
from anony.helpers import admin_check, buttons, can_manage_vc, maintenance_status_text


def format_wait(seconds: int) -> str:
    if seconds <= 0:
        return "starting shortly"
    minutes = max(1, round(seconds / 60))
    return f"about {minutes} minute{'s' if minutes != 1 else ''}"


@app.on_callback_query(filters.regex("cancel_dl") & ~app.bl_users)
@lang.language()
async def cancel_dl(_, query: types.CallbackQuery):
    await query.answer()
    await tg.cancel(query)


@app.on_callback_query(filters.regex(r"^maintenance ") & ~app.bl_users)
async def maintenance_callback(_, query: types.CallbackQuery):
    action, chat_id_text, maintenance_id, owner_id_text = query.data.split()[1:]
    chat_id = int(chat_id_text)
    owner_id = int(owner_id_text)

    if action == "status":
        return await query.edit_message_text(
            await maintenance_status_text(chat_id, maintenance_id),
            reply_markup=buttons.maintenance_receipt(chat_id, maintenance_id, owner_id),
        )

    if action == "queue":
        deferred = queue.get_deferred(chat_id)
        text = "🛠️ <b>Saved maintenance requests</b>\n\n"
        if deferred:
            text += "<blockquote expandable>"
            for index, media in enumerate(deferred[:15], start=1):
                text += f"<b>{index}.</b> {media.title} — {media.duration}\n"
            text += "</blockquote>"
        else:
            text += "✅ No requests are waiting for maintenance."
        return await query.edit_message_text(
            text,
            reply_markup=buttons.maintenance_receipt(chat_id, maintenance_id, owner_id),
        )

    if query.from_user.id != owner_id and query.from_user.id not in app.sudoers:
        admins = await db.get_admins(chat_id)
        if query.from_user.id not in admins:
            return await query.answer(
                "Only the requester or a chat administrator can remove this saved request.",
                show_alert=True,
            )

    removed = queue.remove_deferred(chat_id, maintenance_id)
    if not removed:
        return await query.answer(
            "This saved request was already removed or has started playing.",
            show_alert=True,
        )
    await query.answer("Removed from the maintenance queue.", show_alert=True)
    await query.edit_message_text(
        "🗑 <b>Removed from maintenance queue</b>\n\n"
        f"🎵 {removed.title}\n"
        "This request will not play after the maintenance restart."
    )


@app.on_callback_query(filters.regex(r"^queue_request ") & ~app.bl_users)
async def queue_request_callback(_, query: types.CallbackQuery):
    action, chat_id_text, queue_id, owner_id_text = query.data.split()[1:]
    chat_id, owner_id = int(chat_id_text), int(owner_id_text)
    position = queue.position(chat_id, queue_id)

    if action == "status":
        if position < 0:
            return await query.answer(
                "This request has played or left the queue.", show_alert=True
            )
        media = queue.get_queue(chat_id)[position]
        return await query.edit_message_text(
            "📥 <b>Queue request</b>\n\n"
            f"🎵 <b>Title:</b> {media.title}\n"
            f"📍 Position: <code>{position}</code>\n"
            f"⌛ Estimated wait: <code>{format_wait(queue.estimated_wait(chat_id, position))}</code>",
            reply_markup=buttons.queue_receipt(chat_id, queue_id, owner_id),
        )

    if action == "queue":
        items = queue.get_queue(chat_id)
        text = "📋 <b>Current playback queue</b>\n\n<blockquote expandable>"
        for index, media in enumerate(items[:15]):
            text += f"<b>{index}.</b> {media.title} — {media.duration}\n"
        return await query.edit_message_text(
            text + "</blockquote>",
            reply_markup=buttons.queue_receipt(chat_id, queue_id, owner_id),
        )

    if query.from_user.id != owner_id and query.from_user.id not in app.sudoers:
        admins = await db.get_admins(chat_id)
        if query.from_user.id not in admins:
            return await query.answer(
                "Only the requester or a chat administrator can remove this request.",
                show_alert=True,
            )
    removed = queue.remove(chat_id, queue_id)
    if not removed:
        return await query.answer(
            "This request has started or left the queue.", show_alert=True
        )
    await query.answer("Removed from queue.", show_alert=True)
    await query.edit_message_text(f"🗑 <b>Removed from queue</b>\n\n🎵 {removed.title}")


@app.on_callback_query(filters.regex(r"^song_request ") & ~app.bl_users)
@lang.language()
async def song_request_callback(_, query: types.CallbackQuery):
    chat_id = int(query.data.split()[1])
    if not await db.get_call(chat_id):
        return await query.answer("Nothing is currently playing.", show_alert=True)

    media = queue.get_current(chat_id)
    if not media:
        return await query.answer("Nothing is currently playing.", show_alert=True)
    if getattr(media, "video", False):
        return await query.answer(
            "The current playback is a video stream, so there is no MP3 to send.",
            show_alert=True,
        )

    from anony.plugins.play import can_send_song_file, send_song_file

    if not can_send_song_file(media):
        if getattr(media, "file_path", None):
            return await query.answer(
                "The current song file is no longer available on disk.",
                show_alert=True,
            )
        return await query.answer(
            "This track is being streamed from a source that cannot be sent as an MP3.",
            show_alert=True,
        )

    await query.answer("Preparing MP3...", show_alert=False)
    status = await query.message.reply_text(
        f"🎵 Preparing MP3 for <b>{media.title}</b>..."
    )
    try:
        await send_song_file(
            chat_id,
            media,
            reply_to_message_id=query.message.id,
        )
    except Exception:
        from anony import logger

        logger.exception(
            "Could not prepare song MP3 from callback chat=%s media=%s",
            chat_id,
            getattr(media, "id", "unknown"),
        )
        return await status.edit_text(
            "❌ I could not prepare the MP3 for this song. Please try again shortly."
        )

    await status.delete()


@app.on_callback_query(filters.regex("controls") & ~app.bl_users)
@lang.language()
@can_manage_vc
async def _controls(_, query: types.CallbackQuery):
    args = query.data.split()
    action, chat_id = args[1], int(args[2])
    qaction = len(args) == 4
    user = query.from_user.mention

    if not await db.get_call(chat_id):
        try:
            return await query.answer(query.lang["not_playing"], show_alert=True)
        except errors.QueryIdInvalid:
            try:
                await query.message.delete()
            except Exception:
                pass
            return

    if action == "status":
        return await query.answer()
    if action == "queue":
        items = queue.get_queue(chat_id)[1:6]
        if not items:
            return await query.answer(
                "No tracks are waiting in the queue.", show_alert=True
            )
        text = "\n".join(
            f"{index}. {item.title}" for index, item in enumerate(items, start=1)
        )
        return await query.answer(f"Next tracks:\n{text}", show_alert=True)
    await query.answer(query.lang["processing"], show_alert=True)

    if action == "pause":
        if not await db.playing(chat_id):
            return await query.answer(
                query.lang["play_already_paused"], show_alert=True
            )
        await anon.pause(chat_id)
        if qaction:
            return await query.edit_message_reply_markup(
                reply_markup=buttons.queue_markup(chat_id, query.lang["paused"], False)
            )
        status = query.lang["paused"]
        reply = query.lang["play_paused"].format(user)

    elif action == "resume":
        if await db.playing(chat_id):
            return await query.answer(query.lang["play_not_paused"], show_alert=True)
        await anon.resume(chat_id)
        if qaction:
            return await query.edit_message_reply_markup(
                reply_markup=buttons.queue_markup(chat_id, query.lang["playing"], True)
            )
        reply = query.lang["play_resumed"].format(user)

    elif action == "skip":
        await anon.play_next(chat_id)
        status = query.lang["skipped"]
        reply = query.lang["play_skipped"].format(user)

    elif action == "force":
        pos, media = queue.check_item(chat_id, args[3])
        if not media:
            pos = queue.position(chat_id, args[3])
            media = queue.get_queue(chat_id)[pos] if pos > 0 else None
        if not media or pos == -1:
            return await query.edit_message_text(query.lang["play_expired"])

        m_id = queue.get_current(chat_id).message_id
        queue.force_add(chat_id, media, remove=pos)
        try:
            await app.delete_messages(
                chat_id=chat_id, message_ids=[m_id, media.message_id], revoke=True
            )
            media.message_id = None
        except Exception:
            pass

        msg = await app.send_message(chat_id=chat_id, text=query.lang["play_next"])
        if not media.file_path:
            media.file_path = await yt.download(media.id, video=media.video)
        if not media.file_path:
            queue.remove_current_if(chat_id, media.queue_id)
            return await msg.edit_text(
                "❌ I could not download that queued track.\n\n"
                "💡 I removed it from the front of the queue. Try another result or link."
            )
        media.message_id = msg.id
        try:
            return await anon.play_media(chat_id, msg, media)
        except Exception:
            queue.remove_current_if(chat_id, media.queue_id)
            raise

    elif action == "replay":
        media = queue.get_current(chat_id)
        media.user = user
        await anon.replay(chat_id)
        status = query.lang["replayed"]
        reply = query.lang["play_replayed"].format(user)

    elif action == "loop":
        enabled = bool(await db.get_loop(chat_id))
        await db.set_loop(chat_id, 0 if enabled else 1)
        reply = "🔁 Loop disabled." if enabled else "🔁 Current track will repeat once."
        status = query.lang["playing"]

    elif action == "stop":
        await anon.stop(chat_id)
        status = query.lang["stopped"]
        reply = query.lang["play_stopped"].format(user)

    try:
        if action in ["skip", "replay", "stop"]:
            await query.message.reply_text(reply)
            await query.message.delete()
        else:
            mtext = re.sub(
                r"\n\n<blockquote>.*?</blockquote>",
                "",
                query.message.caption.html or query.message.text.html,
                flags=re.DOTALL,
            )
            keyboard = buttons.controls(
                chat_id, status=status if action != "resume" else None
            )
        await query.edit_message_text(
            f"{mtext}\n\n<blockquote>{reply}</blockquote>", reply_markup=keyboard
        )
    except Exception:
        pass


@app.on_callback_query(filters.regex("help") & ~app.bl_users)
@lang.language()
async def _help(_, query: types.CallbackQuery):
    data = query.data.split()
    if len(data) == 1:
        return await query.answer(url=f"https://t.me/{app.username}?start=help")

    if data[1] == "back":
        return await query.edit_message_text(
            text=f"❔ {query.lang['help_menu']}",
            reply_markup=await buttons.help_markup(
                query.lang,
                user_id=query.from_user.id,
            ),
        )
    elif data[1] == "close":
        try:
            await query.message.delete()
            return await query.message.reply_to_message.delete()
        except Exception:
            return

    if (
        data[1] == "sudo"
        and query.from_user.id != app.owner
        and query.from_user.id not in app.sudoers
    ):
        return await query.answer(
            "Sudo commands are only visible to the owner and sudo users.",
            show_alert=True,
        )

    if data[1] == "mod":
        if not config.MODERATION_ENABLED:
            return await query.answer("Moderation tools are not enabled for this bot.", show_alert=True)
        return await query.edit_message_text(
            text=(
                "🧰 <b>Moderation Tools</b>\n\n"
                "Pick the section you need. The menu stays compact, but each page gives the exact commands and when to use them."
            ),
            reply_markup=buttons.moderation_markup(query.lang),
        )

    if data[1].startswith("mod_"):
        if not config.MODERATION_ENABLED:
            return await query.answer("Moderation tools are not enabled for this bot.", show_alert=True)
        return await query.edit_message_text(
            text=buttons.moderation_help_text(data[1]),
            reply_markup=buttons.moderation_back_markup(query.lang),
        )

    await query.edit_message_text(
        text=f"📘 {query.lang[f'help_{data[1]}']}",
        reply_markup=await buttons.help_markup(
            query.lang,
            True,
            user_id=query.from_user.id,
        ),
    )


@app.on_callback_query(filters.regex("settings") & ~app.bl_users)
@lang.language()
@admin_check
async def _settings_cb(_, query: types.CallbackQuery):
    cmd = query.data.split()
    if len(cmd) == 1:
        return await query.answer()
    await query.answer(query.lang["processing"], show_alert=True)

    chat_id = query.message.chat.id
    _admin = await db.get_play_mode(chat_id)
    _delete = await db.get_cmd_delete(chat_id)
    _language = await db.get_lang(chat_id)

    if cmd[1] == "delete":
        _delete = not _delete
        await db.set_cmd_delete(chat_id, _delete)
    elif cmd[1] == "play":
        await db.set_play_mode(chat_id, _admin)
        _admin = not _admin
    await query.edit_message_reply_markup(
        reply_markup=buttons.settings_markup(
            query.lang,
            _admin,
            _delete,
            _language,
            chat_id,
        )
    )
