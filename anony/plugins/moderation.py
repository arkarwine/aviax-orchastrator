import asyncio
import html
import random
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from pyrogram import enums, errors, filters, handlers, types

from anony import app, config, db


WARN_RESET_SECONDS = 3 * 24 * 60 * 60
DEFAULT_WARN_LIMIT = 3
DEFAULT_WARN_ACTION = "mute"
DEFAULT_MUTE_SECONDS = 30 * 24 * 60 * 60
FLOOD_WINDOW = 12
FLOOD_LIMIT = 7
REPEAT_WINDOW = 25
REPEAT_LIMIT = 4
TELEGRAM_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/\S+", re.I)
WORD_RE = re.compile(r"\b[\w']+\b", re.U)
CALL_EMOJIS = ["👋", "🔔", "🎵", "✨", "📣", "💬", "🎧", "⚡"]

recent_messages: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=20))
call_tasks: dict[int, dict] = {}

GROUP_ONLY_COMMANDS = {
    "ban", "kick", "unban", "mute", "tmute", "unmute",
    "warn", "warns", "resetwarns", "setwarnslimit", "setwarnsaction",
    "purge", "pin", "unpin", "unpinall", "cleanservice", "antichannelpin",
    "antispam", "spamfilter", "delspamfilter", "spamfilters",
    "spamallow", "delspamallow", "spamallowlist",
    "filter", "delfilter", "filters", "note", "delnote", "notes",
    "rules", "setrules", "resetrules", "welcome", "setwelcome",
    "resetwelcome", "welcomeformat", "all", "callall", "call",
    "calladmins", "anybody", "stopcall", "allstatus", "setall",
    "admins", "report",
}
REMOTE_COMMANDS = {
    "cban", "ckick", "cunban", "cmute", "ctban", "ctmute", "cunmute",
    "cbanall", "cban_all", "ckickall", "ckick_all",
    "ctbanall", "ctban_all", "cmuteall", "cmute_all",
    "ctmuteall", "ctmute_all",
}


def enabled() -> bool:
    return bool(config.MODERATION_ENABLED)


async def disabled(message: types.Message) -> bool:
    if enabled():
        return False
    return True


async def chat_settings(chat_id: int) -> dict:
    doc = await db.db.mod_settings.find_one({"_id": chat_id}) or {}
    settings = doc.get("settings", {})
    return {
        "warn_limit": int(settings.get("warn_limit", DEFAULT_WARN_LIMIT) or DEFAULT_WARN_LIMIT),
        "warn_action": settings.get("warn_action", DEFAULT_WARN_ACTION),
        "antispam": bool(settings.get("antispam", True)),
        "cleanservice": bool(settings.get("cleanservice", False)),
        "antichannelpin": bool(settings.get("antichannelpin", False)),
        "welcome": bool(settings.get("welcome", True)),
        "call_batch": int(settings.get("call_batch", 5) or 5),
        "call_delay": int(settings.get("call_delay", 5) or 5),
        "call_hidden": bool(settings.get("call_hidden", True)),
        "call_admins": bool(settings.get("call_admins", False)),
    }


async def set_chat_setting(chat_id: int, key: str, value) -> None:
    await db.db.mod_settings.update_one(
        {"_id": chat_id},
        {"$set": {f"settings.{key}": value}},
        upsert=True,
    )


async def is_admin(chat_id: int, user_id: int) -> bool:
    if user_id in app.sudoers:
        return True
    try:
        member = await app.get_chat_member(chat_id, user_id)
        return member.status in {enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER}
    except Exception:
        return False


async def require_user_admin(message: types.Message, permission: str | None = None) -> bool:
    if message.chat.type == enums.ChatType.PRIVATE:
        await message.reply_text("👥 This command works in groups only.")
        return False
    if not await is_admin(message.chat.id, message.from_user.id):
        await message.reply_text("🔒 You need to be a group admin to use this command.")
        return False
    if permission and message.from_user.id not in app.sudoers:
        member = await app.get_chat_member(message.chat.id, message.from_user.id)
        if member.status != enums.ChatMemberStatus.OWNER and not getattr(member.privileges, permission, False):
            await message.reply_text(f"🔒 You are missing the <code>{permission}</code> permission.")
            return False
    return True


async def require_bot_permission(message: types.Message, permission: str) -> bool:
    try:
        member = await app.get_chat_member(message.chat.id, app.id)
    except Exception:
        await message.reply_text("❌ I could not check my admin permissions in this group.")
        return False
    if member.status == enums.ChatMemberStatus.OWNER:
        return True
    if member.status != enums.ChatMemberStatus.ADMINISTRATOR:
        await message.reply_text("⚠️ I need to be promoted as admin before I can moderate this group.")
        return False
    if not getattr(member.privileges, permission, False):
        await message.reply_text(f"⚠️ I am missing the <code>{permission}</code> permission.")
        return False
    return True


async def target_user(message: types.Message, offset: int = 1):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user, " ".join(message.command[offset:])
    if len(message.command) <= offset:
        return None, ""
    token = message.command[offset]
    reason = " ".join(message.command[offset + 1:])
    try:
        user = await app.get_users(int(token) if token.lstrip("-").isdigit() else token)
        return user, reason
    except Exception:
        return None, reason


async def ensure_target_moderatable(message: types.Message, user: types.User) -> bool:
    if user.is_bot:
        await message.reply_text("🤖 I will not moderate bot accounts with this command.")
        return False
    if await is_admin(message.chat.id, user.id):
        await message.reply_text("🛡️ I will not moderate group admins.")
        return False
    return True


def parse_duration(value: str) -> int | None:
    match = re.fullmatch(r"(\d+)([smhdw])", value.lower())
    if not match:
        return None
    amount = int(match.group(1))
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]


async def restrict_user(chat_id: int, user_id: int, seconds: int) -> None:
    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    await app.restrict_chat_member(
        chat_id,
        user_id,
        types.ChatPermissions(),
        until_date=until,
    )


async def unrestrict_user(chat_id: int, user_id: int) -> None:
    await app.restrict_chat_member(
        chat_id,
        user_id,
        types.ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_invite_users=True,
        ),
    )


def is_sudo_user(user_id: int | None) -> bool:
    return bool(user_id and (user_id == app.owner or user_id in app.sudoers))


async def resolve_chat(token: str) -> types.Chat:
    chat_ref = int(token) if token.lstrip("-").isdigit() else token
    return await app.get_chat(chat_ref)


async def resolve_user(token: str) -> types.User:
    user_ref = int(token) if token.lstrip("-").isdigit() else token
    return await app.get_users(user_ref)


async def remote_bot_permission(message: types.Message, chat_id: int, permission: str) -> bool:
    try:
        member = await app.get_chat_member(chat_id, app.id)
    except Exception:
        await message.reply_text(
            "❌ I cannot check my permissions in that group.\n\n"
            "💡 Make sure I am still in the group and promoted as admin."
        )
        return False
    if member.status == enums.ChatMemberStatus.OWNER:
        return True
    if member.status != enums.ChatMemberStatus.ADMINISTRATOR:
        await message.reply_text(
            "⚠️ I am not an admin in that group.\n\n"
            "💡 Promote me there first, then retry the remote command."
        )
        return False
    if not getattr(member.privileges, permission, False):
        await message.reply_text(
            f"⚠️ I am missing <code>{permission}</code> in that group.\n\n"
            "💡 Update my admin rights there, then retry."
        )
        return False
    return True


async def remote_target_allowed(chat_id: int, user: types.User) -> tuple[bool, str]:
    if user.is_bot:
        return False, "skipped bot account"
    try:
        member = await app.get_chat_member(chat_id, user.id)
    except (errors.UserNotParticipant, errors.exceptions.bad_request_400.UserNotParticipant):
        return True, ""
    except errors.RPCError:
        return True, ""
    if member.status in {enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER}:
        return False, "skipped group admin"
    return True, ""


async def flood_safe(method, *args, **kwargs):
    while True:
        try:
            return await method(*args, **kwargs)
        except errors.FloodWait as exc:
            await asyncio.sleep(int(getattr(exc, "value", 5)) + 1)


async def verify_remote_state(chat_id: int, user_id: int, action: str) -> bool:
    try:
        member = await app.get_chat_member(chat_id, user_id)
    except (errors.UserNotParticipant, errors.exceptions.bad_request_400.UserNotParticipant):
        return action in {"kick", "unban"}
    except errors.RPCError:
        return False
    if action in {"ban", "tban"}:
        return member.status == enums.ChatMemberStatus.BANNED
    if action == "kick":
        return member.status in {enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED}
    if action == "unban":
        return member.status != enums.ChatMemberStatus.BANNED
    if action in {"mute", "tmute"}:
        return member.status == enums.ChatMemberStatus.RESTRICTED or not getattr(member.permissions, "can_send_messages", True)
    if action == "unmute":
        return member.status != enums.ChatMemberStatus.RESTRICTED
    return False


async def remote_action(chat_id: int, user_id: int, action: str, duration: int | None = None) -> bool:
    until = datetime.now(timezone.utc) + timedelta(seconds=duration) if duration else None
    if action in {"ban", "tban"}:
        await flood_safe(app.ban_chat_member, chat_id, user_id, until_date=until)
    elif action == "kick":
        await flood_safe(app.ban_chat_member, chat_id, user_id)
        await flood_safe(app.unban_chat_member, chat_id, user_id)
    elif action == "unban":
        await flood_safe(app.unban_chat_member, chat_id, user_id)
    elif action in {"mute", "tmute"}:
        await flood_safe(app.restrict_chat_member, chat_id, user_id, types.ChatPermissions(), until_date=until)
    elif action == "unmute":
        await flood_safe(
            app.restrict_chat_member,
            chat_id,
            user_id,
            types.ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_invite_users=True,
            ),
        )
    else:
        return False
    return await verify_remote_state(chat_id, user_id, action)


async def warn_user(chat_id: int, user_id: int, reason: str = "") -> tuple[int, int, str]:
    now = time.time()
    key = {"chat_id": chat_id, "user_id": user_id}
    doc = await db.db.mod_warns.find_one(key) or {}
    if now - float(doc.get("updated_at", 0) or 0) > WARN_RESET_SECONDS:
        count = 0
    else:
        count = int(doc.get("count", 0) or 0)
    count += 1
    await db.db.mod_warns.update_one(
        key,
        {"$set": {"count": count, "updated_at": now, "last_reason": reason}},
        upsert=True,
    )
    settings = await chat_settings(chat_id)
    return count, settings["warn_limit"], settings["warn_action"]


async def reset_warns(chat_id: int, user_id: int) -> None:
    await db.db.mod_warns.delete_one({"chat_id": chat_id, "user_id": user_id})


async def apply_warn_limit(message: types.Message, user_id: int, action: str) -> str:
    await reset_warns(message.chat.id, user_id)
    if action == "mute":
        await restrict_user(message.chat.id, user_id, DEFAULT_MUTE_SECONDS)
        return "muted for 30 days"
    await app.ban_chat_member(message.chat.id, user_id)
    return "banned"


def remote_usage(command: str) -> str:
    usages = {
        "cban": "/cban <chat> <user> [reason]",
        "ckick": "/ckick <chat> <user> [reason]",
        "cunban": "/cunban <chat> <user> [reason]",
        "cmute": "/cmute <chat> <user> [reason]",
        "ctban": "/ctban <chat> <user> <duration> [reason]",
        "ctmute": "/ctmute <chat> <user> <duration> [reason]",
        "cunmute": "/cunmute <chat> <user> [reason]",
        "cbanall": "/cbanall <chat> [reason]",
        "ckickall": "/ckickall <chat> [reason]",
        "ctbanall": "/ctbanall <chat> <duration> [reason]",
        "cmuteall": "/cmuteall <chat> [reason]",
        "ctmuteall": "/ctmuteall <chat> <duration> [reason]",
    }
    return usages.get(command.replace("_", ""), "/cban <chat> <user> [reason]")


def remote_action_name(command: str) -> str:
    command = command.replace("_", "")
    if command in {"ctban", "ctbanall"}:
        return "tban"
    if command in {"ctmute", "ctmuteall"}:
        return "tmute"
    if command.startswith("c"):
        return command[1:].removesuffix("all")
    return command


async def remote_command_allowed(message: types.Message) -> bool:
    if not enabled():
        await message.reply_text(
            "🧩 Moderation tools are not enabled for this bot.\n\n"
            "💡 The manager can enable them with <code>/setbotfeature &lt;deployment&gt; moderation on</code>."
        )
        return False
    if not is_sudo_user(message.from_user.id if message.from_user else None):
        await message.reply_text(
            "🔒 Remote moderation is sudo-only.\n\n"
            "💡 Ask the bot owner to add you as sudo if you should manage groups remotely."
        )
        return False
    return True


async def parse_remote_chat(message: types.Message, command: str) -> types.Chat | None:
    if len(message.command) < 2:
        await message.reply_text(f"⚙️ Usage: <code>{remote_usage(command)}</code>")
        return None
    try:
        return await resolve_chat(message.command[1])
    except Exception:
        await message.reply_text(
            "❌ I could not find that chat.\n\n"
            "💡 Use a chat ID like <code>-100123...</code> or a public @username where the bot is present."
        )
        return None


@app.on_message(filters.command(list(REMOTE_COMMANDS)) & ~app.bl_users)
async def _remote_moderation(_, message: types.Message):
    command = message.command[0].lower()
    normalized = command.replace("_", "")
    if not await remote_command_allowed(message):
        return
    chat = await parse_remote_chat(message, normalized)
    if not chat:
        return
    action = remote_action_name(normalized)
    duration = None
    if normalized in {"ctban", "ctmute"}:
        if len(message.command) < 4:
            return await message.reply_text(f"⚙️ Usage: <code>{remote_usage(normalized)}</code>")
        duration = parse_duration(message.command[3])
        if not duration:
            return await message.reply_text("⏱️ Duration must look like <code>10m</code>, <code>2h</code>, <code>7d</code>, or <code>1w</code>.")
        user_index = 2
        reason = " ".join(message.command[4:]) or "No reason provided"
    elif normalized in {"ctbanall", "ctmuteall"}:
        if len(message.command) < 3:
            return await message.reply_text(f"⚙️ Usage: <code>{remote_usage(normalized)}</code>")
        duration = parse_duration(message.command[2])
        if not duration:
            return await message.reply_text("⏱️ Duration must look like <code>10m</code>, <code>2h</code>, <code>7d</code>, or <code>1w</code>.")
        user_index = None
        reason = " ".join(message.command[3:]) or "No reason provided"
    else:
        user_index = 2 if not normalized.endswith("all") else None
        reason = " ".join(message.command[3 if user_index else 2:]) or "No reason provided"

    if action in {"ban", "tban", "kick", "mute", "tmute", "unmute"}:
        permission = "can_restrict_members"
    elif action == "unban":
        permission = "can_restrict_members"
    else:
        return await message.reply_text(f"❌ Unsupported remote action: <code>{html.escape(action)}</code>")
    if not await remote_bot_permission(message, chat.id, permission):
        return

    if user_index is not None:
        if len(message.command) <= user_index:
            return await message.reply_text(f"⚙️ Usage: <code>{remote_usage(normalized)}</code>")
        try:
            user = await resolve_user(message.command[user_index])
        except Exception:
            return await message.reply_text("❌ I could not resolve that user. Use a user ID or @username.")
        if action not in {"unban", "unmute"}:
            allowed, why = await remote_target_allowed(chat.id, user)
            if not allowed:
                return await message.reply_text(f"🛡️ No action taken: {why}.")
        status = await message.reply_text(
            f"🛰️ Applying <code>{action}</code> in <b>{html.escape(chat.title or str(chat.id))}</b>..."
        )
        try:
            success = await remote_action(chat.id, user.id, action, duration)
        except errors.RPCError as exc:
            return await status.edit_text(
                f"❌ Telegram rejected the remote action.\n\n"
                f"📍 Chat: <code>{chat.id}</code>\n"
                f"👤 User: <code>{user.id}</code>\n"
                f"⚙️ Error: <code>{type(exc).__name__}</code>"
            )
        if not success:
            return await status.edit_text(
                "⚠️ I attempted the action, but Telegram did not report the expected final state.\n\n"
                "💡 Check the target user's current status and my admin permissions in that group."
            )
        return await status.edit_text(
            f"✅ Remote action complete.\n\n"
            f"📍 Chat: <code>{chat.id}</code>\n"
            f"👤 User: <code>{user.id}</code>\n"
            f"🛠️ Action: <code>{action}</code>\n"
            f"📝 Reason: {html.escape(reason)}"
        )

    status = await message.reply_text(
        f"🛰️ Preparing bulk <code>{action}</code> in <b>{html.escape(chat.title or str(chat.id))}</b>..."
    )
    succeeded = failed = skipped = 0
    try:
        async for member in app.get_chat_members(chat.id):
            user = member.user
            if not user or user.is_bot or member.status in {enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER}:
                skipped += 1
                continue
            try:
                if await remote_action(chat.id, user.id, action, duration):
                    succeeded += 1
                else:
                    failed += 1
            except errors.RPCError:
                failed += 1
            if (succeeded + failed + skipped) % 15 == 0:
                await status.edit_text(
                    f"🛰️ Bulk <code>{action}</code> running...\n\n"
                    f"✅ Succeeded: <code>{succeeded}</code>\n"
                    f"❌ Failed: <code>{failed}</code>\n"
                    f"🛡️ Skipped admins/bots: <code>{skipped}</code>"
                )
            await asyncio.sleep(0.7)
    except errors.ChatAdminRequired:
        return await status.edit_text("⚠️ I need admin access to enumerate members in that group.")
    except errors.RPCError as exc:
        return await status.edit_text(f"❌ Bulk action stopped: <code>{type(exc).__name__}</code>")

    await status.edit_text(
        f"✅ Bulk remote action complete.\n\n"
        f"📍 Chat: <code>{chat.id}</code>\n"
        f"🛠️ Action: <code>{action}</code>\n"
        f"✅ Succeeded: <code>{succeeded}</code>\n"
        f"❌ Failed: <code>{failed}</code>\n"
        f"🛡️ Skipped admins/bots: <code>{skipped}</code>\n"
        f"📝 Reason: {html.escape(reason)}"
    )


@app.on_message(filters.private & filters.command(list(GROUP_ONLY_COMMANDS)) & ~app.bl_users)
async def _group_only_feedback(_, message: types.Message):
    if not enabled():
        return
    command = message.command[0].lower()
    await message.reply_text(
        f"👥 <code>/{command}</code> works in groups only.\n\n"
        "💡 Use it inside the target group because it needs that group's chat context, admin list, and settings.\n"
        "🛰️ If you are sudo and want to act from DM, use remote moderation commands like "
        "<code>/cban &lt;chat&gt; &lt;user&gt; [reason]</code>."
    )


@app.on_message(filters.private & filters.command(["id", "info"]) & ~app.bl_users)
async def _private_utilities(_, message: types.Message):
    command = message.command[0].lower()
    user = message.from_user
    if command == "info" and len(message.command) > 1:
        try:
            user = await resolve_user(message.command[1])
        except Exception:
            return await message.reply_text("❌ I could not resolve that user. Use a user ID or @username.")
    if command == "id":
        return await message.reply_text(
            f"🆔 <b>IDs</b>\n\n"
            f"👤 User: <code>{user.id}</code>\n"
            f"💬 Chat: <code>{message.chat.id}</code>\n"
            f"📨 Message: <code>{message.id}</code>"
        )
    await message.reply_text(
        f"👤 <b>User info</b>\n\n"
        f"• Name: {user.mention}\n"
        f"• ID: <code>{user.id}</code>\n"
        f"• Bot: <code>{'yes' if user.is_bot else 'no'}</code>\n"
        f"• Username: <code>{html.escape('@' + user.username if user.username else 'none')}</code>"
    )


@app.on_message(filters.command(["ban", "kick", "unban"]) & filters.group & ~app.bl_users)
async def _ban_kick_unban(_, message: types.Message):
    if await disabled(message):
        return
    command = message.command[0].lower()
    permission = "can_restrict_members"
    if not await require_user_admin(message, permission) or not await require_bot_permission(message, permission):
        return
    user, reason = await target_user(message)
    if not user:
        return await message.reply_text(f"⚙️ Usage: <code>/{command} &lt;user_id|username&gt; [reason]</code> or reply to a user.")
    if command != "unban" and not await ensure_target_moderatable(message, user):
        return
    try:
        if command == "ban":
            await app.ban_chat_member(message.chat.id, user.id)
            text = "🔨 Banned"
        elif command == "kick":
            await app.ban_chat_member(message.chat.id, user.id)
            await app.unban_chat_member(message.chat.id, user.id)
            text = "👢 Kicked"
        else:
            await app.unban_chat_member(message.chat.id, user.id)
            text = "🕊️ Unbanned"
        await message.reply_text(f"{text} <code>{user.id}</code>.\n📝 Reason: {reason or 'No reason provided'}")
    except errors.RPCError as exc:
        await message.reply_text(f"❌ Telegram rejected that action.\n\n<code>{type(exc).__name__}</code>")


@app.on_message(filters.command(["mute", "tmute", "unmute"]) & filters.group & ~app.bl_users)
async def _mute_unmute(_, message: types.Message):
    if await disabled(message):
        return
    if not await require_user_admin(message, "can_restrict_members") or not await require_bot_permission(message, "can_restrict_members"):
        return
    command = message.command[0].lower()
    if command == "tmute":
        if message.reply_to_message:
            user = message.reply_to_message.from_user
            duration_text = message.command[1] if len(message.command) > 1 else ""
            reason = " ".join(message.command[2:])
        else:
            user, _ = await target_user(message)
            duration_text = message.command[2] if len(message.command) > 2 else ""
            reason = " ".join(message.command[3:])
    else:
        user, reason = await target_user(message)
    if not user:
        return await message.reply_text("⚙️ Usage: <code>/mute</code>, <code>/tmute 10m</code>, or <code>/unmute</code> by reply or user ID.")
    if command != "unmute" and not await ensure_target_moderatable(message, user):
        return
    try:
        if command == "unmute":
            await unrestrict_user(message.chat.id, user.id)
            return await message.reply_text(f"🔊 Unmuted <code>{user.id}</code>.")
        seconds = DEFAULT_MUTE_SECONDS
        if command == "tmute":
            seconds = parse_duration(duration_text) or 0
            if seconds <= 0:
                return await message.reply_text("⏱️ Usage: <code>/tmute &lt;user&gt; 10m [reason]</code> or reply with <code>/tmute 10m</code>.")
        await restrict_user(message.chat.id, user.id, seconds)
        await message.reply_text(f"🔇 Muted <code>{user.id}</code>.\n⏱️ Duration: <code>{seconds}s</code>\n📝 Reason: {reason or 'No reason provided'}")
    except errors.RPCError as exc:
        await message.reply_text(f"❌ Telegram rejected that action.\n\n<code>{type(exc).__name__}</code>")


@app.on_message(filters.command(["warn", "warns", "resetwarns", "setwarnslimit", "setwarnsaction"]) & filters.group & ~app.bl_users)
async def _warns(_, message: types.Message):
    if await disabled(message):
        return
    command = message.command[0].lower()
    if not await require_user_admin(message, "can_restrict_members"):
        return
    if command == "setwarnslimit":
        if len(message.command) < 2 or not message.command[1].isdigit() or int(message.command[1]) < 1:
            return await message.reply_text("⚙️ Usage: <code>/setwarnslimit 3</code>")
        await set_chat_setting(message.chat.id, "warn_limit", int(message.command[1]))
        return await message.reply_text(f"✅ Warning limit set to <code>{message.command[1]}</code>.")
    if command == "setwarnsaction":
        action = message.command[1].lower() if len(message.command) > 1 else ""
        if action not in {"mute", "ban"}:
            return await message.reply_text("⚙️ Usage: <code>/setwarnsaction mute</code> or <code>/setwarnsaction ban</code>")
        await set_chat_setting(message.chat.id, "warn_action", action)
        return await message.reply_text(f"✅ Warning limit action set to <code>{action}</code>.")
    user, reason = await target_user(message)
    if not user:
        return await message.reply_text(f"⚙️ Usage: <code>/{command} &lt;user_id|username&gt;</code> or reply to a user.")
    if command == "warns":
        doc = await db.db.mod_warns.find_one({"chat_id": message.chat.id, "user_id": user.id}) or {}
        return await message.reply_text(f"⚠️ <code>{user.id}</code> has <code>{int(doc.get('count', 0) or 0)}</code> warning(s).")
    if command == "resetwarns":
        await reset_warns(message.chat.id, user.id)
        return await message.reply_text(f"✅ Reset warnings for <code>{user.id}</code>.")
    if not await ensure_target_moderatable(message, user):
        return
    count, limit, action = await warn_user(message.chat.id, user.id, reason)
    if count >= limit:
        if not await require_bot_permission(message, "can_restrict_members"):
            return await message.reply_text(f"⚠️ Warning limit reached, but I cannot apply <code>{action}</code> without restrict permission.")
        result = await apply_warn_limit(message, user.id, action)
        return await message.reply_text(f"⚠️ Warning <code>{count}/{limit}</code> for <code>{user.id}</code>.\n🚨 Limit reached: user was {result}.")
    await message.reply_text(f"⚠️ Warned <code>{user.id}</code>: <code>{count}/{limit}</code>\n📝 Reason: {reason or 'No reason provided'}")


@app.on_message(filters.command(["pin", "unpin", "unpinall", "purge"]) & filters.group & ~app.bl_users)
async def _message_tools(_, message: types.Message):
    if await disabled(message):
        return
    command = message.command[0].lower()
    permission = "can_pin_messages" if command.startswith("pin") or command.startswith("unpin") else "can_delete_messages"
    if not await require_user_admin(message, permission) or not await require_bot_permission(message, permission):
        return
    if command == "pin":
        if not message.reply_to_message:
            return await message.reply_text("📌 Reply to a message with <code>/pin</code>.")
        await app.pin_chat_message(message.chat.id, message.reply_to_message.id)
        return await message.reply_text("📌 Message pinned.")
    if command == "unpin":
        await app.unpin_chat_message(message.chat.id, message.reply_to_message.id if message.reply_to_message else 0)
        return await message.reply_text("📍 Message unpinned.")
    if command == "unpinall":
        keyboard = types.InlineKeyboardMarkup([[
            types.InlineKeyboardButton("Confirm unpin all", callback_data=f"mod unpinall {message.chat.id}"),
            types.InlineKeyboardButton("Cancel", callback_data="mod cancel"),
        ]])
        return await message.reply_text("⚠️ Unpin all messages in this chat?", reply_markup=keyboard)
    if not message.reply_to_message:
        return await message.reply_text("🧹 Reply to the first message to purge, then send <code>/purge</code> at the end.")
    keyboard = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton("Confirm purge", callback_data=f"mod purge {message.chat.id} {message.reply_to_message.id} {message.id}"),
        types.InlineKeyboardButton("Cancel", callback_data="mod cancel"),
    ]])
    await message.reply_text(
        f"⚠️ Delete messages <code>{message.reply_to_message.id}</code> to <code>{message.id}</code>?",
        reply_markup=keyboard,
    )


@app.on_callback_query(filters.regex(r"^mod "))
async def _moderation_callbacks(_, query: types.CallbackQuery):
    if not enabled():
        return await query.answer("Moderation is disabled for this bot.", show_alert=True)
    parts = query.data.split()
    if parts[1] == "cancel":
        await query.message.edit_text("✅ Cancelled.")
        return await query.answer()
    chat_id = int(parts[2])
    if not await is_admin(chat_id, query.from_user.id):
        return await query.answer("Only group admins can confirm this action.", show_alert=True)
    if parts[1] == "unpinall":
        await app.unpin_all_chat_messages(chat_id)
        await query.message.edit_text("✅ All pinned messages were unpinned.")
        return await query.answer()
    if parts[1] == "purge":
        start, end = int(parts[3]), int(parts[4])
        ids = list(range(min(start, end), max(start, end) + 1))
        deleted = 0
        for index in range(0, len(ids), 100):
            batch = ids[index:index + 100]
            try:
                await app.delete_messages(chat_id, batch)
                deleted += len(batch)
                await asyncio.sleep(0.2)
            except errors.RPCError:
                continue
        await query.message.edit_text(f"🧹 Purge complete. Requested deletion for <code>{deleted}</code> message(s).")
        return await query.answer()


async def spam_words(chat_id: int) -> set[str]:
    doc = await db.db.spam_filters.find_one({"_id": chat_id}) or {}
    words = {str(word).lower() for word in doc.get("words", [])}
    words.add("bio")
    return words


async def spam_allowlist(chat_id: int) -> dict:
    doc = await db.db.spam_allow.find_one({"_id": chat_id}) or {}
    return {
        "users": {int(value) for value in doc.get("users", []) if str(value).lstrip("-").isdigit()},
        "links": {str(value).lower() for value in doc.get("links", [])},
        "forwards": {int(value) for value in doc.get("forwards", []) if str(value).lstrip("-").isdigit()},
    }


def command_payload(message: types.Message) -> str:
    text = message.text or message.caption or ""
    return text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""


def parse_named_payload(payload: str) -> tuple[str | None, str]:
    match = re.match(r'\s*"([^"]+)"\s+(.+)\s*$', payload, re.S)
    if match:
        return match.group(1).strip().lower(), match.group(2).strip()
    match = re.match(r"\s*(\S+)\s+(.+)\s*$", payload, re.S)
    if match:
        return match.group(1).strip().lower(), match.group(2).strip()
    if payload.strip():
        return payload.strip().lower(), ""
    return None, ""


def render_template(template: str, message: types.Message, user: types.User | None = None) -> str:
    user = user or message.from_user
    first_name = user.first_name if user else "there"
    full_name = " ".join(part for part in [getattr(user, "first_name", ""), getattr(user, "last_name", "")] if part).strip() or first_name
    mention = user.mention if user else html.escape(first_name)
    count = getattr(message.chat, "members_count", "") or ""
    return (
        template
        .replace("{mention}", mention)
        .replace("{name}", html.escape(first_name))
        .replace("{fullname}", html.escape(full_name))
        .replace("{id}", str(user.id if user else ""))
        .replace("{chat}", html.escape(message.chat.title or "this chat"))
        .replace("{count}", str(count))
    )


def member_status(member) -> enums.ChatMemberStatus | None:
    return getattr(member, "status", None) if member else None


def is_join_transition(update: types.ChatMemberUpdated) -> bool:
    new_member = update.new_chat_member
    user = getattr(new_member, "user", None) if new_member else None
    if not user or user.is_bot or user.id == app.id:
        return False
    old_status = member_status(update.old_chat_member)
    new_status = member_status(update.new_chat_member)
    join_statuses = {
        enums.ChatMemberStatus.MEMBER,
        enums.ChatMemberStatus.ADMINISTRATOR,
        enums.ChatMemberStatus.OWNER,
    }
    left_statuses = {
        None,
        enums.ChatMemberStatus.LEFT,
        enums.ChatMemberStatus.BANNED,
    }
    return old_status in left_statuses and new_status in join_statuses


async def send_welcome(chat: types.Chat, user: types.User) -> None:
    if not enabled():
        return
    settings = await chat_settings(chat.id)
    if not settings["welcome"]:
        return
    doc = await db.db.welcome.find_one({"_id": chat.id}) or {}
    template = doc.get("text") or "👋 Welcome {mention} to <b>{chat}</b>!"
    shim = type("WelcomeMessage", (), {"chat": chat, "from_user": user})()
    await app.send_message(chat.id, render_template(template, shim, user))


@app.on_message(filters.command(["filter", "delfilter", "filters"]) & filters.group & ~app.bl_users)
async def _keyword_filters(_, message: types.Message):
    if await disabled(message):
        return
    if not await require_user_admin(message, "can_delete_messages"):
        return
    command = message.command[0].lower()
    if command == "filters":
        docs = db.db.keyword_filters.find({"chat_id": message.chat.id}).sort("trigger", 1)
        triggers = [doc["trigger"] async for doc in docs]
        if not triggers:
            return await message.reply_text("🧾 No keyword filters are saved in this group.")
        return await message.reply_text("🧾 <b>Saved filters</b>\n" + "\n".join(f"• <code>{html.escape(item)}</code>" for item in triggers))

    payload = command_payload(message)
    if command == "delfilter":
        trigger = payload.strip().strip('"').lower()
        if not trigger:
            return await message.reply_text("⚙️ Usage: <code>/delfilter trigger</code>")
        result = await db.db.keyword_filters.delete_one({"chat_id": message.chat.id, "trigger": trigger})
        if not result.deleted_count:
            return await message.reply_text(f"❌ No filter named <code>{html.escape(trigger)}</code> was found.")
        return await message.reply_text(f"✅ Deleted filter <code>{html.escape(trigger)}</code>.")

    trigger, response = parse_named_payload(payload)
    if not trigger:
        return await message.reply_text('⚙️ Usage: <code>/filter "trigger phrase" response text</code>')
    if not response and message.reply_to_message:
        response = message.reply_to_message.text or message.reply_to_message.caption or ""
    if not response:
        return await message.reply_text("❌ Send a response after the trigger, or reply to a text/caption message.")
    await db.db.keyword_filters.update_one(
        {"chat_id": message.chat.id, "trigger": trigger},
        {"$set": {"response": response, "updated_at": time.time()}},
        upsert=True,
    )
    await message.reply_text(f"✅ Saved filter <code>{html.escape(trigger)}</code>.")


@app.on_message(filters.command(["note", "delnote", "notes"]) & filters.group & ~app.bl_users)
async def _notes(_, message: types.Message):
    if await disabled(message):
        return
    command = message.command[0].lower()
    if command == "notes":
        docs = db.db.notes.find({"chat_id": message.chat.id}).sort("name", 1)
        names = [doc["name"] async for doc in docs]
        if not names:
            return await message.reply_text("🗒️ No notes are saved in this group.")
        return await message.reply_text("🗒️ <b>Saved notes</b>\n" + "\n".join(f"• <code>{html.escape(item)}</code>" for item in names))

    payload = command_payload(message)
    if command == "delnote":
        if not await require_user_admin(message, "can_delete_messages"):
            return
        name = payload.strip().strip('"').lower()
        if not name:
            return await message.reply_text("⚙️ Usage: <code>/delnote name</code>")
        result = await db.db.notes.delete_one({"chat_id": message.chat.id, "name": name})
        if not result.deleted_count:
            return await message.reply_text(f"❌ No note named <code>{html.escape(name)}</code> was found.")
        return await message.reply_text(f"✅ Deleted note <code>{html.escape(name)}</code>.")

    name, content = parse_named_payload(payload)
    if not name:
        return await message.reply_text('⚙️ Usage: <code>/note "name" content</code> or <code>/note name</code> to view it.')
    if not content and message.reply_to_message:
        content = message.reply_to_message.text or message.reply_to_message.caption or ""
    if not content:
        doc = await db.db.notes.find_one({"chat_id": message.chat.id, "name": name})
        if not doc:
            return await message.reply_text(f"❌ No note named <code>{html.escape(name)}</code> was found.")
        return await message.reply_text(f"🗒️ <b>{html.escape(name)}</b>\n\n{html.escape(doc.get('content', ''))}")
    if not await require_user_admin(message, "can_delete_messages"):
        return
    await db.db.notes.update_one(
        {"chat_id": message.chat.id, "name": name},
        {"$set": {"content": content, "updated_at": time.time()}},
        upsert=True,
    )
    await message.reply_text(f"✅ Saved note <code>{html.escape(name)}</code>.")


@app.on_message(filters.regex(r"^#[A-Za-z0-9_.-]{1,64}$") & filters.group & ~app.bl_users)
async def _note_shortcut(_, message: types.Message):
    if not enabled():
        return
    name = (message.text or "").strip()[1:].lower()
    doc = await db.db.notes.find_one({"chat_id": message.chat.id, "name": name})
    if doc:
        await message.reply_text(f"🗒️ <b>{html.escape(name)}</b>\n\n{html.escape(doc.get('content', ''))}")


@app.on_message(filters.command(["rules", "setrules", "resetrules"]) & filters.group & ~app.bl_users)
async def _rules(_, message: types.Message):
    if await disabled(message):
        return
    command = message.command[0].lower()
    if command == "rules":
        doc = await db.db.rules.find_one({"_id": message.chat.id}) or {}
        text = doc.get("text")
        if not text:
            return await message.reply_text("📜 No rules have been set for this group yet.")
        return await message.reply_text(f"📜 <b>{html.escape(message.chat.title or 'Group')} rules</b>\n\n{html.escape(text)}")
    if not await require_user_admin(message, "can_change_info"):
        return
    if command == "resetrules":
        await db.db.rules.delete_one({"_id": message.chat.id})
        return await message.reply_text("✅ Group rules were reset.")
    text = command_payload(message) or ((message.reply_to_message.text or message.reply_to_message.caption) if message.reply_to_message else "")
    if not text:
        return await message.reply_text("⚙️ Usage: <code>/setrules group rules text</code> or reply to a text message.")
    await db.db.rules.update_one({"_id": message.chat.id}, {"$set": {"text": text, "updated_at": time.time()}}, upsert=True)
    await message.reply_text("✅ Group rules saved.")


@app.on_message(filters.command(["welcome", "setwelcome", "resetwelcome", "welcomeformat"]) & filters.group & ~app.bl_users)
async def _welcome_config(_, message: types.Message):
    if await disabled(message):
        return
    command = message.command[0].lower()
    if command == "welcomeformat":
        return await message.reply_text(
            "🎨 <b>Welcome placeholders</b>\n\n"
            "• <code>{mention}</code> - clickable user mention\n"
            "• <code>{name}</code> - first name\n"
            "• <code>{fullname}</code> - full name\n"
            "• <code>{id}</code> - user ID\n"
            "• <code>{chat}</code> - chat title\n"
            "• <code>{count}</code> - member count"
        )
    if not await require_user_admin(message, "can_change_info"):
        return
    if command == "welcome":
        value = message.command[1].lower() if len(message.command) > 1 else ""
        if value not in {"on", "off"}:
            settings = await chat_settings(message.chat.id)
            return await message.reply_text(f"👋 Welcome is <code>{'on' if settings['welcome'] else 'off'}</code>.\n\nUsage: <code>/welcome on|off</code>")
        await set_chat_setting(message.chat.id, "welcome", value == "on")
        return await message.reply_text(f"✅ Welcome turned <code>{value}</code>.")
    if command == "resetwelcome":
        await db.db.welcome.delete_one({"_id": message.chat.id})
        await set_chat_setting(message.chat.id, "welcome", True)
        return await message.reply_text("✅ Welcome message reset.")
    text = command_payload(message) or ((message.reply_to_message.text or message.reply_to_message.caption) if message.reply_to_message else "")
    if not text:
        return await message.reply_text("⚙️ Usage: <code>/setwelcome Welcome {mention} to {chat}!</code>")
    await db.db.welcome.update_one({"_id": message.chat.id}, {"$set": {"text": text, "updated_at": time.time()}}, upsert=True)
    await message.reply_text("✅ Welcome message saved.")


@app.on_message(filters.new_chat_members & filters.group, group=8)
async def _welcome_new_members(_, message: types.Message):
    for user in message.new_chat_members:
        if user.id != app.id:
            await send_welcome(message.chat, user)


async def _welcome_member_update(_, update: types.ChatMemberUpdated):
    if is_join_transition(update):
        await send_welcome(update.chat, update.new_chat_member.user)


app.add_handler(handlers.ChatMemberUpdatedHandler(_welcome_member_update), group=8)


@app.on_message(filters.command(["cleanservice", "antichannelpin"]) & filters.group & ~app.bl_users)
async def _service_config(_, message: types.Message):
    if await disabled(message):
        return
    if not await require_user_admin(message, "can_delete_messages"):
        return
    command = message.command[0].lower()
    value = message.command[1].lower() if len(message.command) > 1 else ""
    if value not in {"on", "off"}:
        settings = await chat_settings(message.chat.id)
        key = "cleanservice" if command == "cleanservice" else "antichannelpin"
        return await message.reply_text(f"🧹 <code>{command}</code> is <code>{'on' if settings[key] else 'off'}</code>.\n\nUsage: <code>/{command} on|off</code>")
    await set_chat_setting(message.chat.id, command, value == "on")
    await message.reply_text(f"✅ <code>{command}</code> turned <code>{value}</code>.")


@app.on_message(filters.service & filters.group & ~app.bl_users, group=4)
async def _service_cleanup(_, message: types.Message):
    if not enabled():
        return
    settings = await chat_settings(message.chat.id)
    channel_pin = message.pinned_message and message.pinned_message.sender_chat and not message.pinned_message.from_user
    if channel_pin and settings["antichannelpin"]:
        try:
            await app.unpin_chat_message(message.chat.id, message.pinned_message.id)
        except errors.RPCError:
            pass
        try:
            return await message.delete()
        except errors.RPCError:
            return
    if settings["cleanservice"]:
        try:
            await message.delete()
        except errors.RPCError:
            return


@app.on_message(filters.command(["id", "info", "admins", "report"]) & filters.group & ~app.bl_users)
async def _utilities(_, message: types.Message):
    if await disabled(message):
        return
    command = message.command[0].lower()
    if command == "id":
        target = message.reply_to_message.from_user if message.reply_to_message and message.reply_to_message.from_user else message.from_user
        return await message.reply_text(
            f"🆔 <b>IDs</b>\n\n"
            f"👥 Chat: <code>{message.chat.id}</code>\n"
            f"👤 User: <code>{target.id}</code>\n"
            f"💬 Message: <code>{message.reply_to_message.id if message.reply_to_message else message.id}</code>"
        )
    if command == "info":
        user, _ = await target_user(message)
        user = user or message.from_user
        return await message.reply_text(
            f"👤 <b>User info</b>\n\n"
            f"• Name: {user.mention}\n"
            f"• ID: <code>{user.id}</code>\n"
            f"• Bot: <code>{'yes' if user.is_bot else 'no'}</code>\n"
            f"• Username: <code>{html.escape('@' + user.username if user.username else 'none')}</code>"
        )
    if command == "admins":
        admins = []
        async for member in app.get_chat_members(message.chat.id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
            if not member.user.is_bot:
                admins.append(member.user.mention)
        return await message.reply_text("🛡️ <b>Group admins</b>\n" + "\n".join(f"• {item}" for item in admins))
    if not message.reply_to_message:
        return await message.reply_text("🚨 Reply to the message you want to report with <code>/report [reason]</code>.")
    reason = command_payload(message) or "No reason provided"
    mentions = []
    async for member in app.get_chat_members(message.chat.id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
        if not member.user.is_bot:
            mentions.append(member.user.mention)
        if len(mentions) >= 8:
            break
    await message.reply_to_message.reply_text(
        f"🚨 <b>Reported to admins</b>\n\n"
        f"📝 Reason: {html.escape(reason)}\n"
        f"🙋 Reported by: {message.from_user.mention}\n\n"
        + " ".join(mentions)
    )


def mention_text(user: types.User, hidden: bool) -> str:
    if hidden:
        return f'<a href="tg://user?id={user.id}">{random.choice(CALL_EMOJIS)}</a>'
    return user.mention


async def call_settings(chat_id: int) -> dict:
    settings = await chat_settings(chat_id)
    settings["call_batch"] = max(1, min(int(settings["call_batch"]), 20))
    settings["call_delay"] = max(2, min(int(settings["call_delay"]), 60))
    return settings


async def call_targets(chat_id: int, *, admins_only: bool, include_admins: bool, limit: int | None) -> list[types.User]:
    admin_ids = set()
    admins = []
    async for member in app.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
        if member.user and not member.user.is_bot:
            admin_ids.add(member.user.id)
            admins.append(member.user)
    if admins_only:
        return admins[:limit] if limit else admins

    users = []
    async for member in app.get_chat_members(chat_id):
        user = member.user
        if not user or user.is_bot:
            continue
        if not include_admins and user.id in admin_ids:
            continue
        users.append(user)
        if limit and len(users) >= limit:
            break
    return users


async def run_call(message: types.Message, users: list[types.User], text: str, settings: dict, status: types.Message) -> None:
    chat_id = message.chat.id
    sent = 0
    try:
        for index in range(0, len(users), settings["call_batch"]):
            state = call_tasks.get(chat_id)
            if not state or state.get("stop"):
                await status.edit_text(f"🛑 Mention call stopped.\n\n📣 Sent: <code>{sent}</code>/<code>{len(users)}</code>")
                return
            batch = users[index:index + settings["call_batch"]]
            mentions = " ".join(mention_text(user, settings["call_hidden"]) for user in batch)
            prefix = f"{html.escape(text)}\n\n" if text else ""
            try:
                await app.send_message(
                    chat_id,
                    f"{prefix}{mentions}\n\n🛑 Admins can stop this with <code>/stopcall</code>.",
                    disable_web_page_preview=True,
                )
                sent += len(batch)
                call_tasks[chat_id]["sent"] = sent
                await status.edit_text(f"📣 Mention call running...\n\n✅ Sent: <code>{sent}</code>/<code>{len(users)}</code>\n🛑 Stop with <code>/stopcall</code>.")
            except errors.FloodWait as exc:
                await asyncio.sleep(int(getattr(exc, "value", 5)) + 1)
            except errors.RPCError:
                continue
            await asyncio.sleep(settings["call_delay"])
        await status.edit_text(f"✅ Mention call complete.\n\n📣 Sent: <code>{sent}</code>/<code>{len(users)}</code>")
    finally:
        call_tasks.pop(chat_id, None)


@app.on_message(filters.command(["all", "callall", "call", "calladmins", "anybody", "stopcall", "allstatus", "setall"]) & filters.group & ~app.bl_users)
async def _mention_all(_, message: types.Message):
    if await disabled(message):
        return
    command = message.command[0].lower()
    if command in {"stopcall", "allstatus"}:
        if not await require_user_admin(message):
            return
        state = call_tasks.get(message.chat.id)
        if not state:
            return await message.reply_text("📭 No mention call is active in this group.")
        if command == "allstatus":
            return await message.reply_text(
                f"📣 Mention call is active.\n\n"
                f"✅ Sent: <code>{state.get('sent', 0)}</code>/<code>{state.get('total', 0)}</code>\n"
                f"🙋 Started by: <code>{state.get('issuer')}</code>"
            )
        state["stop"] = True
        return await message.reply_text("🛑 Stopping the active mention call...")

    if command == "setall":
        if not await require_user_admin(message, "can_change_info"):
            return
        if len(message.command) < 3:
            return await message.reply_text(
                "⚙️ Usage: <code>/setall batch 5</code>, <code>/setall delay 5</code>, "
                "<code>/setall hidden on|off</code>, or <code>/setall admins on|off</code>"
            )
        key, value = message.command[1].lower(), message.command[2].lower()
        if key == "batch":
            if not value.isdigit():
                return await message.reply_text("❌ Batch must be a number from 1 to 20.")
            await set_chat_setting(message.chat.id, "call_batch", max(1, min(int(value), 20)))
        elif key == "delay":
            if not value.isdigit():
                return await message.reply_text("❌ Delay must be a number from 2 to 60 seconds.")
            await set_chat_setting(message.chat.id, "call_delay", max(2, min(int(value), 60)))
        elif key == "hidden":
            if value not in {"on", "off"}:
                return await message.reply_text("❌ Hidden must be <code>on</code> or <code>off</code>.")
            await set_chat_setting(message.chat.id, "call_hidden", value == "on")
        elif key == "admins":
            if value not in {"on", "off"}:
                return await message.reply_text("❌ Admins must be <code>on</code> or <code>off</code>.")
            await set_chat_setting(message.chat.id, "call_admins", value == "on")
        else:
            return await message.reply_text("❌ Setting must be <code>batch</code>, <code>delay</code>, <code>hidden</code>, or <code>admins</code>.")
        return await message.reply_text(f"✅ Mention-call setting <code>{key}</code> updated.")

    if not await require_user_admin(message):
        return
    if message.chat.id in call_tasks:
        return await message.reply_text("⏳ A mention call is already running. Use <code>/allstatus</code> or <code>/stopcall</code>.")
    settings = await call_settings(message.chat.id)
    payload = command_payload(message)
    limit = None
    text = payload
    if command == "call":
        parts = payload.split(maxsplit=2)
        if parts and parts[0].isdigit():
            limit = max(1, min(int(parts[0]), 500))
            if len(parts) > 1 and parts[1].isdigit():
                settings["call_batch"] = max(1, min(int(parts[1]), 20))
                text = parts[2] if len(parts) > 2 else ""
            else:
                text = " ".join(parts[1:]) if len(parts) > 1 else ""
    elif command == "anybody":
        limit = 25
        text = payload or "Anyone available?"
    if not text and message.reply_to_message:
        text = message.reply_to_message.text or message.reply_to_message.caption or ""

    status = await message.reply_text("🔎 Preparing mention call...")
    try:
        users = await call_targets(
            message.chat.id,
            admins_only=command == "calladmins",
            include_admins=settings["call_admins"] or command == "calladmins",
            limit=limit,
        )
    except errors.ChatAdminRequired:
        return await status.edit_text("⚠️ I need admin access to read members for mention calls.")
    if not users:
        return await status.edit_text("📭 I could not find any eligible members to mention.")
    call_tasks[message.chat.id] = {
        "issuer": message.from_user.id,
        "sent": 0,
        "total": len(users),
        "stop": False,
    }
    await status.edit_text(
        f"📣 Mention call queued.\n\n"
        f"👥 Targets: <code>{len(users)}</code>\n"
        f"📦 Batch: <code>{settings['call_batch']}</code>\n"
        f"⏱️ Delay: <code>{settings['call_delay']}s</code>\n"
        f"🛑 Stop with <code>/stopcall</code>."
    )
    task = asyncio.create_task(run_call(message, users, text, settings, status))
    call_tasks[message.chat.id]["task"] = task


@app.on_message(filters.text & filters.group & ~app.bl_users, group=5)
async def _filter_watcher(_, message: types.Message):
    if not enabled() or not message.text:
        return
    if message.text.startswith("/"):
        return
    text = message.text.lower()
    async for doc in db.db.keyword_filters.find({"chat_id": message.chat.id}):
        trigger = str(doc.get("trigger") or "").lower()
        if trigger and trigger in text:
            return await message.reply_text(html.escape(str(doc.get("response") or "")))


@app.on_message(filters.command(["antispam", "spamfilter", "delspamfilter", "spamfilters", "spamallow", "delspamallow", "spamallowlist"]) & filters.group & ~app.bl_users)
async def _antispam_config(_, message: types.Message):
    if await disabled(message):
        return
    if not await require_user_admin(message, "can_delete_messages"):
        return
    command = message.command[0].lower()
    if command == "antispam":
        value = message.command[1].lower() if len(message.command) > 1 else ""
        if value not in {"on", "off"}:
            settings = await chat_settings(message.chat.id)
            return await message.reply_text(f"🛡️ Anti-spam is <code>{'on' if settings['antispam'] else 'off'}</code>.\n\nUsage: <code>/antispam on|off</code>")
        await set_chat_setting(message.chat.id, "antispam", value == "on")
        return await message.reply_text(f"✅ Anti-spam turned <code>{value}</code>.")
    if command == "spamfilters":
        words = sorted(await spam_words(message.chat.id))
        return await message.reply_text("🧹 Spam filters:\n" + "\n".join(f"• <code>{word}</code>" for word in words))
    if command == "spamallowlist":
        allow = await spam_allowlist(message.chat.id)
        lines = ["🟢 <b>Anti-spam allowlist</b>"]
        lines.append("\n<b>Users</b>")
        if allow["users"]:
            lines.extend(f"• <code>{user_id}</code>" for user_id in sorted(allow["users"]))
        else:
            lines.append("• none")
        lines.append("\n<b>Links</b>")
        if allow["links"]:
            lines.extend(f"• <code>{link}</code>" for link in sorted(allow["links"]))
        else:
            lines.append("• none")
        lines.append("\n<b>Forward sources</b>")
        if allow["forwards"]:
            lines.extend(f"• <code>{source}</code>" for source in sorted(allow["forwards"]))
        else:
            lines.append("• none")
        return await message.reply_text("\n".join(lines))
    if command in {"spamallow", "delspamallow"}:
        if len(message.command) < 3 and not message.reply_to_message:
            return await message.reply_text(
                f"⚙️ Usage: <code>/{command} user &lt;id|reply&gt;</code>, <code>/{command} link t.me/example</code>, or <code>/{command} forward &lt;chat_id|reply&gt;</code>"
            )
        kind = message.command[1].lower() if len(message.command) > 1 else "user"
        pull = command == "delspamallow"
        update_op = "$pull" if pull else "$addToSet"
        try:
            if kind == "user":
                value = (
                    message.reply_to_message.from_user.id
                    if message.reply_to_message and message.reply_to_message.from_user
                    else int(message.command[2])
                )
                field = "users"
            elif kind in {"link", "links"}:
                value = message.command[2].lower().removeprefix("https://").removeprefix("http://")
                field = "links"
            elif kind in {"forward", "forwards", "source"}:
                if message.reply_to_message and message.reply_to_message.forward_from_chat:
                    value = message.reply_to_message.forward_from_chat.id
                elif message.reply_to_message and message.reply_to_message.forward_from:
                    value = message.reply_to_message.forward_from.id
                else:
                    value = int(message.command[2])
                field = "forwards"
            else:
                return await message.reply_text("❌ Allowlist type must be <code>user</code>, <code>link</code>, or <code>forward</code>.")
        except (IndexError, ValueError):
            return await message.reply_text(
                f"❌ I could not understand that allowlist target.\n\n"
                f"💡 Use <code>/{command} user 123456</code>, reply with <code>/{command} user</code>, "
                f"or use <code>/{command} link t.me/example</code>."
            )
        await db.db.spam_allow.update_one({"_id": message.chat.id}, {update_op: {field: value}}, upsert=True)
        action = "Removed from" if pull else "Added to"
        return await message.reply_text(f"✅ {action} anti-spam allowlist: <code>{field}</code> <code>{value}</code>")
    if len(message.command) < 2:
        return await message.reply_text(f"⚙️ Usage: <code>/{command} word</code>")
    word = message.command[1].lower()
    if command == "spamfilter":
        await db.db.spam_filters.update_one({"_id": message.chat.id}, {"$addToSet": {"words": word}}, upsert=True)
        return await message.reply_text(f"✅ Added spam filter <code>{word}</code>.")
    await db.db.spam_filters.update_one({"_id": message.chat.id}, {"$pull": {"words": word}}, upsert=True)
    await message.reply_text(f"✅ Removed spam filter <code>{word}</code>.")


@app.on_message(filters.group & ~filters.service & ~filters.me & ~app.bl_users, group=3)
async def _antispam_watcher(_, message: types.Message):
    if not enabled() or not message.from_user or await is_admin(message.chat.id, message.from_user.id):
        return
    settings = await chat_settings(message.chat.id)
    if not settings["antispam"]:
        return
    allow = await spam_allowlist(message.chat.id)
    if message.from_user.id in allow["users"]:
        return
    text = (message.text or message.caption or "").lower()
    compact_text = text.removeprefix("https://").removeprefix("http://")
    if any(link in compact_text for link in allow["links"]):
        return
    forward_id = None
    if message.forward_from_chat:
        forward_id = message.forward_from_chat.id
    elif message.forward_from:
        forward_id = message.forward_from.id
    if forward_id and forward_id in allow["forwards"]:
        return
    words = {match.group(0).lower() for match in WORD_RE.finditer(text)}
    reason = None
    if TELEGRAM_LINK_RE.search(text):
        reason = "Telegram link"
    elif message.forward_from or message.forward_from_chat:
        reason = "forwarded message"
    elif words & await spam_words(message.chat.id):
        reason = "spam word"
    else:
        key = (message.chat.id, message.from_user.id)
        now = time.time()
        bucket = recent_messages[key]
        bucket.append((now, text))
        flood = sum(1 for timestamp, _ in bucket if now - timestamp <= FLOOD_WINDOW)
        repeat = sum(1 for timestamp, value in bucket if now - timestamp <= REPEAT_WINDOW and value and value == text)
        if flood >= FLOOD_LIMIT:
            reason = "message flood"
        elif repeat >= REPEAT_LIMIT:
            reason = "repeated message"
    if not reason:
        return
    try:
        await message.delete()
    except errors.RPCError:
        return
    try:
        count, limit, action = await warn_user(message.chat.id, message.from_user.id, reason)
        if count >= limit and await require_bot_permission(message, "can_restrict_members"):
            result = await apply_warn_limit(message, message.from_user.id, action)
            await app.send_message(message.chat.id, f"🛡️ Deleted spam from <code>{message.from_user.id}</code>.\n🚨 Limit reached: user was {result}.")
    except Exception:
        await app.send_message(message.chat.id, "🛡️ Deleted spam, but I could not update the user's warning count.")
