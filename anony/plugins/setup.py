# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


from pyrogram import Client, enums, filters, types
from pyrogram.errors import (
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    SessionPasswordNeeded,
)

from anony import anon, app, config, db, lang, logger, userbot
from anony.helpers import buttons


session_setup = {}


def is_owner(user_id: int) -> bool:
    return bool(user_id and (user_id == app.owner or user_id in app.sudoers))


async def claim_owner(user: types.User) -> bool:
    if config.OWNER_ID or not user:
        return False
    try:
        if config.MANAGED_SETUP and config.DEPLOYMENT_ID:
            await db.set_config("DEPLOYMENT_ID", config.DEPLOYMENT_ID)
        await db.set_config("OWNER_ID", user.id)
        await db.add_sudo(user.id)
    except Exception:
        logger.exception("Could not claim deployment owner")
        return False

    config.apply_runtime_config({"OWNER_ID": user.id})
    app.owner = user.id
    app.sudoers.add(user.id)
    return True


def setup_complete() -> bool:
    return bool(config.OWNER_ID and config.LOGGER_ID and config.SESSION1)


def setup_text() -> str:
    if not config.OWNER_ID:
        return "👋 Send <code>/start</code> in private chat to claim this deployment."
    if not config.LOGGER_ID:
        return (
            "<b>📝 Setup step 1</b>\n\n"
            "Create a log group, add this bot, promote it as admin, then run <code>/setlog</code> in that group."
        )
    if not config.SESSION1:
        return (
            "<b>🔐 Setup step 2</b>\n\n"
            "Connect an assistant user account with <code>/addsession</code> in private chat."
        )
    return (
        "<b>✨ Setup step 3</b>\n\n"
        "Optional: set <code>/support &lt;link&gt;</code>, <code>/updates &lt;link&gt;</code>, and <code>/langcode &lt;code&gt;</code>."
    )


@app.on_message(filters.private & ~app.bl_users, group=-2)
@lang.language()
async def _claim_first_owner(_, m: types.Message):
    if m.text and m.text.startswith("/start"):
        return
    if await claim_owner(m.from_user):
        await m.reply_text(
            "👑 You are now the owner for this deployment.\n\n" + setup_text()
        )
    elif not config.OWNER_ID:
        await m.reply_text(
            "❌ I could not save the deployment owner.\n\n"
            "💡 Check the database connection, then send /start again."
        )


@app.on_message(filters.command(["setup"]) & ~app.bl_users)
@lang.language()
async def _setup_status(_, m: types.Message):
    if not is_owner(m.from_user.id):
        return await m.reply_text(
            "🔒 Only the deployment owner can view setup.\n\n💡 Send the first private /start to this bot to claim ownership."
        )
    await m.reply_text(setup_text())


@app.on_message(filters.command(["setlog"]) & ~app.bl_users)
@lang.language()
async def _set_log_group(_, m: types.Message):
    if m.chat.type == enums.ChatType.PRIVATE:
        return await m.reply_text(
            "🏠 Use this command inside the log group after adding the bot and promoting it as admin."
        )
    if not is_owner(m.from_user.id):
        return await m.reply_text("🔒 Only the deployment owner can set the log group.")

    status = await m.reply_text("🔎 Checking my admin status in this group...")
    try:
        member = await app.get_chat_member(m.chat.id, app.id)
    except Exception:
        logger.exception("Could not verify log group permissions for chat %s", m.chat.id)
        return await status.edit_text(
            "❌ I could not check my permissions in this group.\n\n"
            "💡 Make sure I am still in the group, promote me as admin, then run <code>/setlog</code> again."
        )

    if member.status != enums.ChatMemberStatus.ADMINISTRATOR:
        return await status.edit_text(
            "🛡️ I am not an admin in this group yet.\n\n💡 Promote me as admin, then run <code>/setlog</code> again."
        )

    await status.edit_text("💾 Saving this group as the log group...")
    try:
        await db.set_config("LOGGER_ID", m.chat.id)
    except Exception:
        logger.exception("Could not save log group %s", m.chat.id)
        return await status.edit_text(
            "❌ I could not save this log group.\n\n"
            "💡 Check the database connection, then run <code>/setlog</code> again."
        )
    config.apply_runtime_config({"LOGGER_ID": m.chat.id})
    app.logger = m.chat.id
    started = 0
    if any([config.SESSION1, config.SESSION2, config.SESSION3]) and not userbot.clients:
        await status.edit_text("🚀 Log group saved. Starting stored assistant session(s)...")
        try:
            await userbot.boot()
            for ub in userbot.clients:
                await anon.add_client(ub)
                started += 1
        except (Exception, SystemExit):
            logger.exception("Stored assistants could not start after setting log group")
            return await status.edit_text(
                "⚠️ The log group was saved, but stored assistants could not start.\n\n"
                "💡 Make sure they can send messages here, then restart the deployment."
            )
    await status.edit_text(
        "✅ Log group configured. I can write logs here now."
        + (f"\n\nStarted {started} stored assistant session(s)." if started else "")
        + "\n\n➡️ Next: connect an assistant user account in private chat.",
        reply_markup=buttons.setup_next_session(),
    )


@app.on_message(filters.command(["support"]) & filters.private & ~app.bl_users)
@lang.language()
async def _set_support(_, m: types.Message):
    if not is_owner(m.from_user.id):
        return await m.reply_text("🔒 Only the deployment owner can set the support group.")
    if len(m.command) < 2:
        return await m.reply_text("💬 Usage: <code>/support https://t.me/your_group</code>")
    value = m.command[1].strip()
    status = await m.reply_text("💾 Saving support group...")
    try:
        await db.set_config("SUPPORT_CHAT", value)
    except Exception:
        logger.exception("Could not save support group")
        return await status.edit_text(
            "❌ I could not save the support group.\n\n💡 Check the database connection and try again."
        )
    config.apply_runtime_config({"SUPPORT_CHAT": value})
    await status.edit_text(f"✅ Support group set to: <code>{value}</code>")


@app.on_message(filters.command(["updates", "channel"]) & filters.private & ~app.bl_users)
@lang.language()
async def _set_updates(_, m: types.Message):
    if not is_owner(m.from_user.id):
        return await m.reply_text("🔒 Only the deployment owner can set the updates channel.")
    if len(m.command) < 2:
        return await m.reply_text("📣 Usage: <code>/updates https://t.me/your_channel</code>")
    value = m.command[1].strip()
    status = await m.reply_text("💾 Saving updates channel...")
    try:
        await db.set_config("SUPPORT_CHANNEL", value)
    except Exception:
        logger.exception("Could not save updates channel")
        return await status.edit_text(
            "❌ I could not save the updates channel.\n\n💡 Check the database connection and try again."
        )
    config.apply_runtime_config({"SUPPORT_CHANNEL": value})
    await status.edit_text(f"✅ Updates channel set to: <code>{value}</code>")


@app.on_message(filters.command(["langcode"]) & filters.private & ~app.bl_users)
@lang.language()
async def _set_lang_code(_, m: types.Message):
    if not is_owner(m.from_user.id):
        return await m.reply_text("🔒 Only the deployment owner can set the default language.")
    if len(m.command) < 2:
        return await m.reply_text("🌐 Usage: <code>/langcode en</code>")
    value = m.command[1].strip().lower()
    status = await m.reply_text("💾 Saving default language...")
    try:
        await db.set_config("LANG_CODE", value)
    except Exception:
        logger.exception("Could not save default language")
        return await status.edit_text(
            "❌ I could not save the default language.\n\n💡 Check the database connection and try again."
        )
    config.apply_runtime_config({"LANG_CODE": value})
    await status.edit_text(f"✅ Default language set to: <code>{value}</code>")


@app.on_message(filters.command(["addsession"]) & filters.private & ~app.bl_users)
@lang.language()
async def _add_session_start(_, m: types.Message):
    await begin_session_setup(m)


async def begin_session_setup(m: types.Message):
    if not is_owner(m.from_user.id):
        return await m.reply_text("🔒 Only the deployment owner can add assistant sessions.")
    if not config.LOGGER_ID:
        return await m.reply_text(
            "📝 Set the log group first with <code>/setlog</code>.\n\n💡 Assistants must be able to send a startup message there."
        )
    if all([config.SESSION1, config.SESSION2, config.SESSION3]):
        return await m.reply_text("✅ All three assistant session slots are already configured.")

    session_setup[m.from_user.id] = {"step": "phone"}
    await m.reply_text(
        "📱 Send the assistant account phone number in international format, for example <code>+959123456789</code>.\n\n"
        "🛑 Send /cancel anytime to stop."
    )


@app.on_message(filters.command(["cancel"]) & filters.private)
async def _cancel_session(_, m: types.Message):
    state = session_setup.pop(m.from_user.id, None)
    if state and state.get("client"):
        try:
            await state["client"].disconnect()
        except Exception:
            pass
    if state:
        await m.reply_text("🛑 Session setup cancelled.")


@app.on_message(filters.private & filters.text & ~app.bl_users, group=-1)
async def _session_setup_text(_, m: types.Message):
    state = session_setup.get(m.from_user.id)
    if not state:
        return
    if not is_owner(m.from_user.id):
        session_setup.pop(m.from_user.id, None)
        return await m.reply_text("🛑 Session setup stopped because you are not the owner.")

    text = (m.text or "").strip()
    if text.startswith("/"):
        return

    if state["step"] == "phone":
        status = await m.reply_text("🔌 Connecting to Telegram...")
        client = Client(
            name=f"session-gen-{m.from_user.id}",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            in_memory=True,
        )
        try:
            await client.connect()
            await status.edit_text("📨 Sending login code...")
            sent = await client.send_code(text)
        except Exception:
            logger.exception("Could not send assistant login code")
            try:
                await client.disconnect()
            except Exception:
                pass
            session_setup.pop(m.from_user.id, None)
            return await status.edit_text(
                "❌ I could not send the login code.\n\n"
                "💡 Check the phone number and try again later with <code>/addsession</code>."
            )
        state.update(
            {
                "step": "code",
                "phone": text,
                "phone_code_hash": sent.phone_code_hash,
                "client": client,
            }
        )
        return await status.edit_text(
            "✅ Code sent. Reply with the login code you received.\n\n"
            "Spaces are fine; I will remove them before submitting."
        )

    if state["step"] == "code":
        code = text.replace(" ", "")
        client = state["client"]
        status = await m.reply_text("🔐 Verifying login code...")
        try:
            await client.sign_in(
                phone_number=state["phone"],
                phone_code_hash=state["phone_code_hash"],
                phone_code=code,
            )
        except SessionPasswordNeeded:
            state["step"] = "password"
            return await status.edit_text("🔐 Two-step verification is enabled. Reply with the account password.")
        except PhoneCodeInvalid:
            return await status.edit_text("❌ That code was invalid.\n\n💡 Send the latest login code again.")
        except PhoneCodeExpired:
            await client.disconnect()
            session_setup.pop(m.from_user.id, None)
            return await status.edit_text("⌛ That code expired.\n\n💡 Run <code>/addsession</code> to start again.")
        except Exception:
            logger.exception("Could not verify assistant login code")
            await client.disconnect()
            session_setup.pop(m.from_user.id, None)
            return await status.edit_text(
                "❌ I could not verify the login code.\n\n💡 Request a fresh code with <code>/addsession</code> and try again."
            )
        return await _finish_session(m, client, status)

    if state["step"] == "password":
        client = state["client"]
        status = await m.reply_text("🔐 Verifying two-step password...")
        try:
            await client.check_password(text)
        except PasswordHashInvalid:
            return await status.edit_text("❌ That password was incorrect.\n\n💡 Send the correct two-step password.")
        except Exception:
            logger.exception("Could not verify assistant two-step password")
            await client.disconnect()
            session_setup.pop(m.from_user.id, None)
            return await status.edit_text(
                "❌ I could not verify the password.\n\n💡 Run <code>/addsession</code> to start again."
            )
        return await _finish_session(m, client, status)


async def _finish_session(m: types.Message, client: Client, status: types.Message | None = None) -> None:
    status = status or await m.reply_text("💾 Saving assistant session...")
    await status.edit_text("📤 Exporting assistant session...")
    try:
        session_string = await client.export_session_string()
    except Exception:
        logger.exception("Could not export assistant session")
        await client.disconnect()
        session_setup.pop(m.from_user.id, None)
        return await status.edit_text(
            "❌ I could not export the assistant session.\n\n"
            "💡 Run <code>/addsession</code> to sign in again and retry."
        )
    await client.disconnect()

    slot = next(
        num
        for num, value in enumerate((config.SESSION1, config.SESSION2, config.SESSION3), start=1)
        if not value
    )
    await status.edit_text("💾 Saving assistant session...")
    try:
        await db.set_config(f"SESSION{slot}", session_string)
    except Exception:
        logger.exception("Could not save assistant session")
        session_setup.pop(m.from_user.id, None)
        return await status.edit_text(
            "❌ I could not save the assistant session.\n\n"
            "💡 Check the database connection, then run <code>/addsession</code> again."
        )

    await status.edit_text("🚀 Starting assistant account...")
    try:
        started_slot = await userbot.add_session(session_string)
    except SystemExit:
        logger.exception("Saved assistant session could not start")
        session_setup.pop(m.from_user.id, None)
        return await status.edit_text(
            "⚠️ The assistant session was saved, but it could not start.\n\n"
            "💡 Make sure the assistant can send messages in the log group, then restart the deployment."
        )
    except Exception:
        logger.exception("Saved assistant session could not start")
        session_setup.pop(m.from_user.id, None)
        return await status.edit_text(
            "⚠️ The assistant session was saved, but it could not start.\n\n"
            "💡 Check the log group access and Telegram login state, then restart the deployment."
        )

    await status.edit_text("🎙️ Connecting assistant to the voice call engine...")
    try:
        await anon.add_client(userbot.clients[-1])
    except Exception:
        logger.exception("Assistant could not connect to voice call engine")
        session_setup.pop(m.from_user.id, None)
        return await status.edit_text(
            "⚠️ The assistant started, but voice chat playback is not ready.\n\n"
            "💡 Restart the deployment and make sure the assistant can join voice chats."
        )

    session_setup.pop(m.from_user.id, None)
    await status.edit_text(
        f"✅ Assistant session saved in slot {started_slot} and started successfully.\n\n"
        "➡️ Next: set <code>/support &lt;link&gt;</code>, <code>/updates &lt;link&gt;</code>, and <code>/langcode &lt;code&gt;</code> if needed."
    )
