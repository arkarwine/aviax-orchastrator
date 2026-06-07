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

from anony import anon, app, config, db, lang, userbot


session_setup = {}


def is_owner(user_id: int) -> bool:
    return bool(user_id and (user_id == app.owner or user_id in app.sudoers))


async def claim_owner(user: types.User) -> bool:
    if config.OWNER_ID or not user:
        return False
    await db.set_config("OWNER_ID", user.id)
    config.apply_runtime_config({"OWNER_ID": user.id})
    app.owner = user.id
    app.sudoers.add(user.id)
    await db.add_sudo(user.id)
    return True


def setup_text() -> str:
    owner = "done" if config.OWNER_ID else "pending"
    logger = "done" if config.LOGGER_ID else "pending"
    sessions = len([s for s in (config.SESSION1, config.SESSION2, config.SESSION3) if s])
    return (
        "<b>Bot setup</b>\n\n"
        f"Owner: <code>{owner}</code>\n"
        f"Log group: <code>{logger}</code>\n"
        f"Assistant sessions: <code>{sessions}/3</code>\n"
        f"Support group: <code>{config.SUPPORT_CHAT}</code>\n"
        f"Updates channel: <code>{config.SUPPORT_CHANNEL}</code>\n"
        f"Language: <code>{config.LANG_CODE}</code>\n\n"
        "<b>Next steps</b>\n"
        "1. Create a log group, add this bot, promote it as admin, then run <code>/setlog</code> in that group.\n"
        "2. Run <code>/addsession</code> here to connect a user assistant account.\n"
        "3. Run <code>/support &lt;link&gt;</code>, <code>/updates &lt;link&gt;</code>, and <code>/langcode &lt;code&gt;</code>."
    )


@app.on_message(filters.private & ~app.bl_users, group=-2)
@lang.language()
async def _claim_first_owner(_, m: types.Message):
    if await claim_owner(m.from_user):
        await m.reply_text(
            "You are now the owner for this deployment.\n\n" + setup_text()
        )


@app.on_message(filters.command(["setup"]) & ~app.bl_users)
@lang.language()
async def _setup_status(_, m: types.Message):
    if not is_owner(m.from_user.id):
        return await m.reply_text(
            "Only the deployment owner can view setup. Send the first private message to this bot to claim ownership."
        )
    await m.reply_text(setup_text())


@app.on_message(filters.command(["setlog"]) & ~app.bl_users)
@lang.language()
async def _set_log_group(_, m: types.Message):
    if m.chat.type == enums.ChatType.PRIVATE:
        return await m.reply_text(
            "Use this command inside the log group after adding the bot and promoting it as admin."
        )
    if not is_owner(m.from_user.id):
        return await m.reply_text("Only the deployment owner can set the log group.")

    try:
        member = await app.get_chat_member(m.chat.id, app.id)
    except Exception as exc:
        return await m.reply_text(
            f"I could not verify my permissions in this group.\n\nReason: <code>{exc}</code>"
        )

    if member.status != enums.ChatMemberStatus.ADMINISTRATOR:
        return await m.reply_text(
            "I am not an admin in this group yet. Promote me as admin, then run <code>/setlog</code> again."
        )

    await db.set_config("LOGGER_ID", m.chat.id)
    config.apply_runtime_config({"LOGGER_ID": m.chat.id})
    app.logger = m.chat.id
    started = 0
    if any([config.SESSION1, config.SESSION2, config.SESSION3]) and not userbot.clients:
        await userbot.boot()
        for ub in userbot.clients:
            await anon.add_client(ub)
            started += 1
    await m.reply_text(
        "Log group configured. I can write logs here now."
        + (f"\n\nStarted {started} stored assistant session(s)." if started else "")
    )


@app.on_message(filters.command(["support"]) & filters.private & ~app.bl_users)
@lang.language()
async def _set_support(_, m: types.Message):
    if not is_owner(m.from_user.id):
        return await m.reply_text("Only the deployment owner can set the support group.")
    if len(m.command) < 2:
        return await m.reply_text("Usage: <code>/support https://t.me/your_group</code>")
    value = m.command[1].strip()
    await db.set_config("SUPPORT_CHAT", value)
    config.apply_runtime_config({"SUPPORT_CHAT": value})
    await m.reply_text(f"Support group set to: <code>{value}</code>")


@app.on_message(filters.command(["updates", "channel"]) & filters.private & ~app.bl_users)
@lang.language()
async def _set_updates(_, m: types.Message):
    if not is_owner(m.from_user.id):
        return await m.reply_text("Only the deployment owner can set the updates channel.")
    if len(m.command) < 2:
        return await m.reply_text("Usage: <code>/updates https://t.me/your_channel</code>")
    value = m.command[1].strip()
    await db.set_config("SUPPORT_CHANNEL", value)
    config.apply_runtime_config({"SUPPORT_CHANNEL": value})
    await m.reply_text(f"Updates channel set to: <code>{value}</code>")


@app.on_message(filters.command(["langcode"]) & filters.private & ~app.bl_users)
@lang.language()
async def _set_lang_code(_, m: types.Message):
    if not is_owner(m.from_user.id):
        return await m.reply_text("Only the deployment owner can set the default language.")
    if len(m.command) < 2:
        return await m.reply_text("Usage: <code>/langcode en</code>")
    value = m.command[1].strip().lower()
    await db.set_config("LANG_CODE", value)
    config.apply_runtime_config({"LANG_CODE": value})
    await m.reply_text(f"Default language set to: <code>{value}</code>")


@app.on_message(filters.command(["addsession"]) & filters.private & ~app.bl_users)
@lang.language()
async def _add_session_start(_, m: types.Message):
    if not is_owner(m.from_user.id):
        return await m.reply_text("Only the deployment owner can add assistant sessions.")
    if not config.LOGGER_ID:
        return await m.reply_text(
            "Set the log group first with <code>/setlog</code>. Assistants must be able to send a startup message there."
        )
    if all([config.SESSION1, config.SESSION2, config.SESSION3]):
        return await m.reply_text("All three assistant session slots are already configured.")

    session_setup[m.from_user.id] = {"step": "phone"}
    await m.reply_text(
        "Send the assistant account phone number in international format, for example <code>+959123456789</code>.\n\n"
        "Send /cancel anytime to stop."
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
        await m.reply_text("Session setup cancelled.")


@app.on_message(filters.private & filters.text & ~app.bl_users, group=-1)
async def _session_setup_text(_, m: types.Message):
    state = session_setup.get(m.from_user.id)
    if not state:
        return
    if not is_owner(m.from_user.id):
        session_setup.pop(m.from_user.id, None)
        return await m.reply_text("Session setup stopped because you are not the owner.")

    text = (m.text or "").strip()
    if text.startswith("/"):
        return

    if state["step"] == "phone":
        client = Client(
            name=f"session-gen-{m.from_user.id}",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            in_memory=True,
        )
        await client.connect()
        sent = await client.send_code(text)
        state.update(
            {
                "step": "code",
                "phone": text,
                "phone_code_hash": sent.phone_code_hash,
                "client": client,
            }
        )
        return await m.reply_text(
            "Code sent. Reply with the login code you received.\n\n"
            "Spaces are fine; I will remove them before submitting."
        )

    if state["step"] == "code":
        code = text.replace(" ", "")
        client = state["client"]
        try:
            await client.sign_in(
                phone_number=state["phone"],
                phone_code_hash=state["phone_code_hash"],
                phone_code=code,
            )
        except SessionPasswordNeeded:
            state["step"] = "password"
            return await m.reply_text("Two-step verification is enabled. Reply with the account password.")
        except PhoneCodeInvalid:
            return await m.reply_text("That code was invalid. Send the latest login code again.")
        except PhoneCodeExpired:
            await client.disconnect()
            session_setup.pop(m.from_user.id, None)
            return await m.reply_text("That code expired. Run <code>/addsession</code> to start again.")
        return await _finish_session(m, client)

    if state["step"] == "password":
        client = state["client"]
        try:
            await client.check_password(text)
        except PasswordHashInvalid:
            return await m.reply_text("That password was incorrect. Send the correct two-step password.")
        return await _finish_session(m, client)


async def _finish_session(m: types.Message, client: Client) -> None:
    session_string = await client.export_session_string()
    await client.disconnect()

    slot = next(
        num
        for num, value in enumerate((config.SESSION1, config.SESSION2, config.SESSION3), start=1)
        if not value
    )
    await db.set_config(f"SESSION{slot}", session_string)
    started_slot = await userbot.add_session(session_string)
    await anon.add_client(userbot.clients[-1])
    session_setup.pop(m.from_user.id, None)
    await m.reply_text(
        f"Assistant session saved in slot {started_slot} and started successfully."
    )
