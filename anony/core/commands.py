# Copyright (c) 2026 CyberPixelPro
# Licensed under the MIT License.
# This file is part of AviaxMusic


from pyrogram import types

from anony import app, logger

PUBLIC_PRIVATE_COMMANDS = [
    ("start", "Open the music bot start menu"),
    ("help", "Browse the command reference"),
    ("stats", "View service and usage statistics"),
    ("ping", "Check whether the bot is responsive"),
    ("language", "Choose your preferred language"),
]

PUBLIC_GROUP_COMMANDS = [
    ("play", "Play a song or replied media"),
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
            command_list(PUBLIC_GROUP_COMMANDS),
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
