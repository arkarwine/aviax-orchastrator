# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic

import asyncio
from pyrogram import enums, errors, filters, types
from pyrogram.types import ReplyParameters

from anony import anon, app, config, db, lang, logger, userbot
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
    if not any(member.id == app.id for member in message.new_chat_members):
        return

    if message.chat.type != enums.ChatType.SUPERGROUP:
        try:
            await message.reply_text(
                "⚠️ <b>This chat is not ready for music streaming.</b>\n\n"
                "❌ I can only stream in a <b>supergroup</b>.\n"
                "💡 Make chat history visible once to upgrade this group, then add me again."
            )
            await asyncio.sleep(5)
        finally:
            return await message.chat.leave()

    status = await message.reply_text(
        "🔎 <b>Checking whether this group is ready for streaming...</b>"
    )
    blockers = []
    notices = []
    chat_id = message.chat.id

    try:
        bot_member = await app.get_chat_member(chat_id, app.id)
        bot_is_admin = bot_member.status in (
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        )
        privileges = getattr(bot_member, "privileges", None)
        can_invite = bot_is_admin and (
            bot_member.status == enums.ChatMemberStatus.OWNER
            or bool(getattr(privileges, "can_invite_users", False))
        )

        if not bot_is_admin:
            blockers.append(
                "🛡️ <b>Promote me as an admin</b> so I can prepare and manage the music assistant."
            )
        elif not can_invite:
            blockers.append(
                "➕ Enable <b>Invite Users</b> in my admin permissions so I can add the music assistant."
            )
    except Exception:
        logger.exception("Could not inspect bot permissions after joining chat %s", chat_id)
        bot_is_admin = False
        can_invite = False
        blockers.append(
            "🛡️ I could not verify my permissions. Promote me as an admin with <b>Invite Users</b>, then try again."
        )

    assistants = list(userbot.clients)
    if not assistants:
        blockers.append(
            "👤 No music assistant is connected. The deployment owner must use <code>/addsession</code> in my private chat."
        )
    else:
        assistant_present = False
        assistant_banned = False
        assistant_can_join = False
        for assistant in assistants:
            try:
                member = await app.get_chat_member(chat_id, assistant.id)
                if member.status in (
                    enums.ChatMemberStatus.BANNED,
                    enums.ChatMemberStatus.RESTRICTED,
                ):
                    assistant_banned = True
                    continue
                assistant_present = True
                break
            except (errors.UserNotParticipant, errors.exceptions.bad_request_400.UserNotParticipant):
                assistant_can_join = True
                continue
            except errors.ChatAdminRequired:
                break
            except Exception:
                logger.exception(
                    "Could not inspect assistant membership after joining chat %s", chat_id
                )
                break

        if assistant_present:
            notices.append("👤 A connected music assistant is already in this group.")
        elif assistant_can_join and (message.chat.username or can_invite):
            notices.append(
                "🤝 A connected music assistant will join automatically on the first <code>/play</code>."
            )
        elif assistant_banned and not assistant_can_join:
            blockers.append(
                "🚫 The connected music assistant is banned or restricted. Unban it before using <code>/play</code>."
            )
        else:
            blockers.append(
                "🔗 The music assistant cannot join this private group. Give me <b>Invite Users</b> permission or set a public group username."
            )

    if await anon.has_active_group_call(chat_id, assume_active_on_error=False):
        notices.append("🎙️ An active voice chat is available.")
    else:
        blockers.append(
            "🎙️ Start a voice or video chat before using <code>/play</code>."
        )

    if blockers:
        text = (
            "⚠️ <b>This group needs a little setup before streaming.</b>\n\n"
            + "\n\n".join(blockers)
        )
        if notices:
            text += "\n\n<b>Already ready</b>\n" + "\n".join(notices)
        text += "\n\n🔄 After fixing these items, send <code>/play song name</code>."
    else:
        text = (
            "✅ <b>This group is ready for streaming.</b>\n\n"
            + "\n".join(notices)
            + "\n\n🎵 Send <code>/play song name</code> to begin."
        )

    await status.edit_text(text)

    if await db.is_chat(chat_id):
        return
    await utils.send_log(message, True)
    await db.add_chat(chat_id)
