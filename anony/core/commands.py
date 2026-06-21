# Copyright (c) 2026 CyberPixelPro
# Licensed under the MIT License.
# This file is part of AviaxMusic


from pyrogram import types

from anony import app, config, logger

PUBLIC_PRIVATE_COMMANDS = [
    ("start", "Open the music bot start menu"),
    ("help", "Browse the command reference"),
    ("stats", "View service and usage statistics"),
    ("ping", "Check whether the bot is responsive"),
    ("language", "Choose your preferred language"),
]

PUBLIC_GROUP_COMMANDS = [
    ("play", "Play a song or replied media"),
    ("nowplaying", "Show the current now-playing card"),
    ("song", "Send the currently playing song as MP3"),
    ("vplay", "Play a video"),
    ("queue", "View the current queue"),
    ("maintenance", "View maintenance restart status"),
    ("pause", "Pause the current stream"),
    ("resume", "Resume the paused stream"),
    ("skip", "Skip the current track"),
    ("stop", "Stop playback and clear the queue"),
    ("loop", "Configure track looping"),
    ("seek", "Seek forward in the current track"),
    ("seekback", "Seek backward in the current track"),
    ("settings", "Open this group's playback settings"),
    ("stats", "View service and usage statistics"),
    ("help", "Browse the command reference"),
    ("ping", "Check whether the bot is responsive"),
    ("language", "Choose this group's language"),
]

MODERATION_GROUP_COMMANDS = [
    ("ban", "Ban a user from the group"),
    ("kick", "Kick a user from the group"),
    ("unban", "Unban a user"),
    ("mute", "Mute a user"),
    ("tmute", "Temporarily mute a user"),
    ("unmute", "Unmute a user"),
    ("warn", "Warn a user"),
    ("warns", "View a user's warnings"),
    ("resetwarns", "Reset a user's warnings"),
    ("setwarnslimit", "Set this group's warning limit"),
    ("setwarnsaction", "Set the warning limit action"),
    ("purge", "Delete a range of messages"),
    ("pin", "Pin a message"),
    ("unpin", "Unpin a message"),
    ("unpinall", "Unpin all messages"),
    ("cleanservice", "Auto-delete service messages"),
    ("antichannelpin", "Auto-delete linked-channel pins"),
    ("antispam", "Configure anti-spam"),
    ("spamfilter", "Add a spam word filter"),
    ("delspamfilter", "Remove a spam word filter"),
    ("spamfilters", "List spam word filters"),
    ("spamallow", "Allow a user, link, or forward source"),
    ("delspamallow", "Remove an anti-spam allowlist item"),
    ("spamallowlist", "View anti-spam allowlist"),
    ("filter", "Save a keyword response"),
    ("delfilter", "Delete a keyword response"),
    ("filters", "List keyword responses"),
    ("note", "Save or view a note"),
    ("delnote", "Delete a note"),
    ("notes", "List notes"),
    ("rules", "Show group rules"),
    ("setrules", "Set group rules"),
    ("resetrules", "Reset group rules"),
    ("welcome", "Enable or disable welcomes"),
    ("setwelcome", "Set the welcome message"),
    ("resetwelcome", "Reset the welcome message"),
    ("welcomeformat", "Show welcome placeholders"),
    ("all", "Mention group members in batches"),
    ("callall", "Mention group members in batches"),
    ("call", "Mention a limited number of members"),
    ("calladmins", "Mention group admins"),
    ("anybody", "Ask available members to respond"),
    ("stopcall", "Stop the active mention call"),
    ("allstatus", "View mention-call status"),
    ("setall", "Configure mention-call behavior"),
    ("id", "Show Telegram IDs"),
    ("info", "Show user info"),
    ("admins", "List group admins"),
    ("report", "Report a message to admins"),
]

SUDO_PRIVATE_COMMANDS = PUBLIC_PRIVATE_COMMANDS + [
    ("activevc", "View active voice chats"),
    ("broadcast", "Broadcast a replied message"),
    ("schedulebroadcast", "Schedule a text broadcast"),
    ("scheduledbroadcasts", "View scheduled broadcasts"),
    ("cancelbroadcast", "Cancel a scheduled broadcast"),
    ("stop_broadcast", "Cancel the active broadcast"),
    ("logs", "Download the bot log file"),
    ("logger", "Enable or disable activity logging"),
    ("restart", "Restart the deployed bot"),
    ("config", "Manage live runtime settings"),
    ("getconfig", "View a runtime setting"),
    ("resetconfig", "Reset a runtime setting"),
    ("refreshconfig", "Reload configuration without restarting"),
    ("blacklist", "Block a chat or user"),
    ("unblacklist", "Unblock a chat or user"),
    ("sudolist", "View owner and sudo users"),
    ("setup", "View deployment setup status"),
    ("setlog", "Configure the optional log group"),
    ("disablelog", "Disable activity logging and warnings"),
    ("checksetup", "Check deployment readiness"),
    ("support", "Configure the support group"),
    ("updates", "Configure the updates channel"),
    ("langcode", "Configure the default language"),
    ("addsession", "Connect an assistant account"),
    ("editsession", "Replace an assistant session"),
    ("sessions", "View configured assistant sessions"),
    ("removesession", "Remove an assistant session"),
]

OWNER_PRIVATE_COMMANDS = SUDO_PRIVATE_COMMANDS + [
    ("changeowner", "Transfer bot ownership"),
    ("addsudo", "Grant sudo access to a user"),
    ("delsudo", "Remove sudo access from a user"),
    ("eval", "Execute Python code as the owner"),
]


def command_list(items: list[tuple[str, str]]) -> list[types.BotCommand]:
    return [types.BotCommand(command, description) for command, description in items]


def group_commands() -> list[tuple[str, str]]:
    commands = list(PUBLIC_GROUP_COMMANDS)
    if config.MODERATION_ENABLED:
        commands.extend(MODERATION_GROUP_COMMANDS)
    return commands


async def set_user_command_menu(user_id: int, *, owner: bool = False) -> None:
    commands = OWNER_PRIVATE_COMMANDS if owner else SUDO_PRIVATE_COMMANDS
    await app.set_bot_commands(
        command_list(commands),
        scope=types.BotCommandScopeChat(chat_id=user_id),
    )


async def set_public_user_command_menu(user_id: int) -> None:
    await app.set_bot_commands(
        command_list(PUBLIC_PRIVATE_COMMANDS),
        scope=types.BotCommandScopeChat(chat_id=user_id),
    )


async def sync_command_menus(previous_privileged: set[int] | None = None) -> list[str]:
    warnings = []
    scopes = (
        (
            command_list(PUBLIC_PRIVATE_COMMANDS),
            types.BotCommandScopeAllPrivateChats(),
            "private user menu",
        ),
        (
            command_list(group_commands()),
            types.BotCommandScopeAllGroupChats(),
            "group menu",
        ),
        (
            command_list(PUBLIC_PRIVATE_COMMANDS),
            types.BotCommandScopeDefault(),
            "default menu",
        ),
    )
    for commands, scope, label in scopes:
        try:
            await app.set_bot_commands(commands, scope=scope)
        except Exception:
            logger.exception("Could not register %s", label)
            warnings.append(label)

    for user_id in set(app.sudoers):
        try:
            await set_user_command_menu(user_id, owner=user_id == app.owner)
        except Exception:
            logger.exception(
                "Could not register command menu for sudo user %s", user_id
            )
            warnings.append(f"sudo menu {user_id}")
    for user_id in (previous_privileged or set()) - set(app.sudoers):
        try:
            await set_public_user_command_menu(user_id)
        except Exception:
            logger.exception(
                "Could not restore public command menu for user %s", user_id
            )
            warnings.append(f"public menu {user_id}")
    return warnings
