# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic

import asyncio
from pyrogram import enums, filters, types
from pyrogram.types import ReplyParameters

from anony import app, config, db, lang
from anony.helpers import buttons, utils
from anony.plugins.setup import begin_session_setup, claim_owner, setup_complete, setup_text


@app.on_message(filters.command(["help"]) & filters.private & ~app.bl_users)
@lang.language()
async def _help(_, m: types.Message):
    await m.reply_text(
        text=f"❔ {m.lang['help_menu']}",
        reply_markup=await buttons.help_markup(m.lang, user_id=m.from_user.id),
        reply_parameters=ReplyParameters(message_id=m.id),
    )


@app.on_message(filters.command(["start"]))
@lang.language()
async def start(_, message: types.Message):
    if message.from_user.id in app.bl_users and message.from_user.id not in db.notified:
        return await message.reply_text(message.lang["bl_user_notify"])

    private = message.chat.type == enums.ChatType.PRIVATE
    if private and len(message.command) > 1 and message.command[1] == "addsession":
        return await begin_session_setup(message)

    if private and not setup_complete():
        claimed = await claim_owner(message.from_user)
        if not claimed and not config.OWNER_ID:
            return await message.reply_text(
                "❌ I could not save the deployment owner.\n\n"
                "💡 Check the database connection, then send /start again."
            )
        return await message.reply_text(setup_text())

    if len(message.command) > 1 and message.command[1] == "help":
        return await _help(_, message)

    _text = (
        f"🎵 {message.lang['start_pm'].format(message.from_user.first_name, app.name)}"
        if private
        else f"🎵 {message.lang['start_gp'].format(app.name)}"
    )

    key = buttons.start_key(message.lang, private)
    await message.reply_photo(
        photo=config.START_IMG,
        caption=_text,
        reply_markup=key,
        reply_parameters=ReplyParameters(message_id=message.id) if not private else None,
    )

    if private:
        if await db.is_user(message.from_user.id):
            return
        await utils.send_log(message)
        await db.add_user(message.from_user.id)
    else:
        if await db.is_chat(message.chat.id):
            return
        await utils.send_log(message, True)
        await db.add_chat(message.chat.id)


@app.on_message(filters.command(["playmode", "settings"]) & filters.group & ~app.bl_users)
@lang.language()
async def settings(_, message: types.Message):
    admin_only = await db.get_play_mode(message.chat.id)
    cmd_delete = await db.get_cmd_delete(message.chat.id)
    _language = await db.get_lang(message.chat.id)
    await message.reply_text(
        text=message.lang["start_settings"].format(message.chat.title),
        reply_markup=buttons.settings_markup(
            message.lang, admin_only, cmd_delete, _language, message.chat.id
        ),
        reply_parameters=ReplyParameters(message_id=message.id),
    )


@app.on_message(filters.new_chat_members, group=7)
@lang.language()
async def _new_member(_, message: types.Message):
    if message.chat.type != enums.ChatType.SUPERGROUP:
        return await message.chat.leave()

    await asyncio.sleep(3)
    for member in message.new_chat_members:
        if member.id == app.id:
            if await db.is_chat(message.chat.id):
                return
            await utils.send_log(message, True)
            await db.add_chat(message.chat.id)
