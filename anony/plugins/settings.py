# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import asyncio
import secrets
import time

from pyrogram import enums, filters, types

from config import Config

from anony import app, config, db, lang, logger, yt
from anony.core.commands import set_public_user_command_menu, set_user_command_menu, sync_command_menus
from anony.helpers import NexGenApi


VALID_KEYS = [
    "AUTO_LEAVE",
    "AUTO_END",
    "THUMB_GEN",
    "VIDEO_PLAY",
    "LANG_CODE",
    "DEFAULT_THUMB",
    "PING_IMG",
    "START_IMG",
    "OWNER_LINK",
]
BOOL_KEYS = {"AUTO_LEAVE", "AUTO_END", "THUMB_GEN", "VIDEO_PLAY"}
RESTART_REQUIRED_KEYS = {
    "API_ID",
    "API_HASH",
    "BOT_TOKEN",
    "MONGO_URL",
    "DB_NAME",
    "DEPLOYMENT_ID",
    "MANAGED_SETUP",
    "SESSION_PATH",
    "SESSION1",
    "SESSION2",
    "SESSION3",
}
refresh_lock = asyncio.Lock()
owner_transfers = {}


def restart_reason_lines(keys: list[str]) -> list[str]:
    groups = (
        (
            {"API_ID", "API_HASH", "BOT_TOKEN", "SESSION_PATH"},
            "Telegram client identity or session storage",
        ),
        (
            {"MONGO_URL", "DB_NAME", "DEPLOYMENT_ID", "MANAGED_SETUP"},
            "database connection or deployment identity",
        ),
        (
            {"SESSION1", "SESSION2", "SESSION3"},
            "active assistant account sessions",
        ),
    )
    lines = []
    for group, reason in groups:
        matched = sorted(set(keys) & group)
        if matched:
            lines.append(
                "• "
                + ", ".join(f"<code>{key}</code>" for key in matched)
                + f": {reason}"
            )
    return lines


async def apply_owner_id(value: int, *, keep_previous_sudo: bool) -> None:
    previous_owner = app.owner
    await db.set_config("OWNER_ID", value)
    await db.add_sudo(value)
    config.apply_runtime_config({"OWNER_ID": value})
    app.owner = value
    app.sudoers.add(value)
    if previous_owner and previous_owner != value and not keep_previous_sudo:
        app.sudoers.discard(previous_owner)
        try:
            await db.del_sudo(previous_owner)
        except Exception:
            logger.warning("Owner changed, but old owner sudo cleanup failed.")
        try:
            await set_public_user_command_menu(previous_owner)
        except Exception:
            logger.warning("Owner changed, but old owner command menu cleanup failed.")
    elif previous_owner and previous_owner != value:
        app.sudoers.add(previous_owner)
        await db.add_sudo(previous_owner)
        try:
            await set_user_command_menu(previous_owner)
        except Exception:
            logger.warning("Owner changed, but the previous owner's sudo menu could not be updated.")
    try:
        await set_user_command_menu(value, owner=True)
    except Exception:
        logger.warning("Owner changed, but the new owner command menu could not be updated.")


@app.on_message(filters.command(["changeowner", "transferowner"]) & filters.private & ~app.bl_users)
@lang.language()
async def _change_owner(_, m: types.Message):
    if m.from_user.id != app.owner:
        return await m.reply_text("🔒 Only the current owner can transfer bot ownership.")
    if len(m.command) < 2 or not m.command[1].isdigit() or int(m.command[1]) <= 0:
        return await m.reply_text(
            "👑 Usage: <code>/changeowner &lt;new_owner_user_id&gt;</code>\n\n"
            "💡 Provide the new owner's numeric Telegram user ID."
        )

    new_owner = int(m.command[1])
    if new_owner == app.owner:
        return await m.reply_text("👑 You are already the bot owner.")

    try:
        user = await app.get_users(new_owner)
        if user.is_bot:
            return await m.reply_text(
                "❌ A bot account cannot become the deployment owner.\n\n"
                "💡 Provide the Telegram user ID of a person."
            )
        target = user.mention
    except Exception:
        target = f"<code>{new_owner}</code>"

    token = secrets.token_urlsafe(8)
    for pending_token, transfer in list(owner_transfers.items()):
        if transfer["previous_owner"] == app.owner:
            owner_transfers.pop(pending_token, None)
    owner_transfers[token] = {
        "previous_owner": app.owner,
        "new_owner": new_owner,
        "created_at": time.monotonic(),
    }
    await m.reply_text(
        f"👑 Transfer ownership to {target}?\n\n"
        "Would you like to remain as a sudo user after the transfer?",
        reply_markup=types.InlineKeyboardMarkup(
            [
                [
                    types.InlineKeyboardButton(
                        "✅ Remain as sudo",
                        callback_data=f"owner_transfer keep {token}",
                        style=enums.ButtonStyle.SUCCESS,
                    ),
                    types.InlineKeyboardButton(
                        "🚪 Remove my access",
                        callback_data=f"owner_transfer remove {token}",
                        style=enums.ButtonStyle.DANGER,
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        "✖️ Cancel",
                        callback_data=f"owner_transfer cancel {token}",
                    )
                ],
            ]
        ),
    )


@app.on_callback_query(filters.regex(r"^owner_transfer (keep|remove|cancel) "))
async def _change_owner_confirm(_, query: types.CallbackQuery):
    _, action, token = query.data.split(maxsplit=2)
    transfer = owner_transfers.get(token)
    if (
        not transfer
        or time.monotonic() - transfer["created_at"] > 600
        or query.from_user.id != transfer["previous_owner"]
        or app.owner != transfer["previous_owner"]
    ):
        owner_transfers.pop(token, None)
        return await query.answer("This ownership transfer request is no longer valid.", show_alert=True)
    owner_transfers.pop(token, None)
    if action == "cancel":
        await query.answer("Ownership transfer cancelled.")
        return await query.message.edit_text("✖️ Ownership transfer cancelled.")

    await query.answer("Transferring ownership...")
    try:
        await apply_owner_id(
            transfer["new_owner"],
            keep_previous_sudo=action == "keep",
        )
    except Exception:
        logger.exception("Could not transfer bot ownership")
        return await query.message.edit_text(
            "❌ I could not transfer ownership.\n\n"
            "💡 Check the database connection and try again."
        )

    access = (
        "✅ You remain a sudo user."
        if action == "keep"
        else "🚪 Your previous owner access and sudo access were removed."
    )
    await query.message.edit_text(
        f"✅ Ownership transferred to <code>{transfer['new_owner']}</code>.\n\n{access}"
    )
    try:
        await app.send_message(
            transfer["new_owner"],
            "👑 You are now the owner of this music bot.\n\n"
            "Use <code>/help</code> to view the commands available to you.",
        )
    except Exception:
        logger.warning("Ownership transferred, but the new owner could not be notified.")


async def build_api_client(settings: dict):
    if not all(settings.get(key) for key in ("API_URL", "VIDEO_API_URL", "API_KEY")):
        return None
    api = NexGenApi(
        settings["API_URL"],
        settings["API_KEY"],
        settings["VIDEO_API_URL"],
    )
    await api.get_session()
    return api


async def close_api_client(api) -> None:
    if api and api.session and not api.session.closed:
        await api.session.close()


@app.on_message(
    filters.command(["refreshconfig", "reloadconfig"])
    & filters.private
    & ~app.bl_users
)
@lang.language()
async def _refresh_config(_, m: types.Message):
    if m.from_user.id not in app.sudoers:
        return await m.reply_text(
            "🔒 You need sudo access to refresh the live configuration."
        )
    if refresh_lock.locked():
        return await m.reply_text(
            "⏳ A configuration refresh is already running. Please wait for it to finish."
        )

    async with refresh_lock:
        status = await m.reply_text("📂 Reading configuration from disk...")
        current = config.snapshot()
        try:
            candidate = Config.from_disk()
            candidate.check()
            disk_defaults = candidate.snapshot()
            await status.edit_text("🗄️ Loading runtime overrides from the database...")
            runtime = await db.get_all_config()
            if (
                candidate.MANAGED_SETUP
                and candidate.DEPLOYMENT_ID
                and runtime
                and not runtime.get("DEPLOYMENT_ID")
            ):
                await status.edit_text("🪪 Repairing the stored deployment identity...")
                await db.set_config("DEPLOYMENT_ID", candidate.DEPLOYMENT_ID)
                runtime["DEPLOYMENT_ID"] = candidate.DEPLOYMENT_ID
            if (
                candidate.MANAGED_SETUP
                and candidate.DEPLOYMENT_ID
                and runtime
                and runtime.get("DEPLOYMENT_ID") != candidate.DEPLOYMENT_ID
            ):
                return await status.edit_text(
                    "❌ The stored runtime configuration belongs to a different deployment.\n\n"
                    "💡 Correct the deployment identity before refreshing. Nothing was changed."
                )
            candidate.apply_runtime_config(
                {key: value for key, value in runtime.items() if key != "DEPLOYMENT_ID"}
            )
            desired = candidate.snapshot()
            sudoers = set(await db.get_sudoers())
        except (FileNotFoundError, ValueError, TypeError, SystemExit):
            logger.exception("Could not parse configuration refresh")
            return await status.edit_text(
                "❌ I could not read a valid configuration from disk.\n\n"
                "💡 Check the deployment <code>.env</code> values and try again. Nothing was changed."
            )
        except Exception:
            logger.exception("Could not load runtime configuration refresh")
            return await status.edit_text(
                "❌ I could not load the stored runtime configuration.\n\n"
                "💡 Check the database connection and try again. Nothing was changed."
            )

        await status.edit_text("🌐 Validating language files...")
        try:
            refreshed_languages = lang.load_files()
        except Exception:
            logger.exception("Could not reload language files")
            return await status.edit_text(
                "❌ One or more language files could not be loaded.\n\n"
                "💡 Fix the locale JSON files and try again. Nothing was changed."
            )

        changed = {
            key for key in Config.KEYS
            if current.get(key) != desired.get(key)
        }
        restart_required = sorted(changed & RESTART_REQUIRED_KEYS)
        live_changes = sorted(changed - RESTART_REQUIRED_KEYS)
        api_changed = bool({"API_URL", "VIDEO_API_URL", "API_KEY"} & set(live_changes))

        new_api = yt.api
        if api_changed:
            await status.edit_text("🔌 Preparing refreshed API configuration...")
            try:
                new_api = await build_api_client(desired)
            except Exception:
                logger.exception("Could not prepare refreshed API client")
                return await status.edit_text(
                    "❌ The refreshed API configuration could not be prepared.\n\n"
                    "💡 Check the API settings and try again. Nothing was changed."
                )

        await status.edit_text("⚡ Applying safe configuration changes...")
        subsystem_errors = []
        safe_values = {key: desired[key] for key in live_changes}
        config.apply_runtime_config(safe_values)
        config._runtime_defaults = {
            key: disk_defaults[key]
            for key in config._runtime_defaults
        }

        previous_privileged = set(app.sudoers)
        app.owner = config.OWNER_ID
        app.logger = config.LOGGER_ID
        app.sudoers.clear()
        if app.owner:
            sudoers.add(app.owner)
        app.sudoers.update(sudoers)

        lang.languages = refreshed_languages
        db.lang.clear()

        await status.edit_text("📋 Refreshing registered command menus...")
        menu_warnings = await sync_command_menus(previous_privileged)
        if menu_warnings:
            subsystem_errors.append("registered command menus")

        try:
            from anony.plugins.misc import sync_optional_tasks
            await sync_optional_tasks()
        except Exception:
            logger.exception("Could not synchronize optional background tasks")
            subsystem_errors.append("background tasks")

        if api_changed:
            old_api = yt.api
            yt.api = new_api
            try:
                await close_api_client(old_api)
            except Exception:
                logger.exception("Could not close previous API client")
                subsystem_errors.append("previous API connection cleanup")

        if "COOKIES_URL" in live_changes:
            yt.cookies.clear()
            yt.warned = False
            if config.COOKIES_URL:
                try:
                    await yt.save_cookies(config.COOKIES_URL)
                    yt.checked = False
                except Exception:
                    logger.exception("Could not refresh cookie files")
                    subsystem_errors.append("cookie download")
            else:
                yt.checked = True

        unchanged = len(Config.KEYS) - len(changed)
        text = (
            "✅ <b>Configuration refreshed.</b>\n\n"
            f"⚡ Applied live: <code>{len(live_changes)}</code> settings\n"
            f"🌐 Reloaded: <code>{len(refreshed_languages)}</code> language files\n"
            f"⏸️ Unchanged: <code>{unchanged}</code> settings"
        )
        if restart_required:
            text += (
                "\n\n🔄 <b>Restart required</b>\n"
                + "\n".join(restart_reason_lines(restart_required))
                + "\nThese settings were not applied live."
            )
        if subsystem_errors:
            text += (
                "\n\n⚠️ Some live services could not be fully refreshed: "
                + ", ".join(subsystem_errors)
                + ". Their existing state was kept where possible."
            )
        await status.edit_text(text)


@app.on_message(filters.command(["config", "botconfig"]) & ~app.bl_users)
@lang.language()
async def _settings(_, m: types.Message):
    """Show runtime setting help or update a setting when key/value are provided."""
    if m.chat.type != enums.ChatType.PRIVATE:
        return await m.reply_text(
            "This command works in private chats only, because it changes live bot settings."
        )
    if m.from_user.id not in app.sudoers:
        return await m.reply_text(
            "You need sudo access to use this command, because it changes live bot settings."
        )

    runtime = await db.get_all_config()

    def short(value: str, limit: int = 50) -> str:
        return value if not value or len(value) <= limit else value[:limit - 3] + "..."

    if len(m.command) == 1:
        text = (
            "<b>📌 Runtime Settings</b>\n"
            "Change live bot behavior without restarting.\n\n"
            "<b>Available keys</b>\n"
            "• auto_leave (true/false)\n"
            "• auto_end (true/false)\n"
            "• thumb_gen (true/false)\n"
            "• video_play (true/false)\n"
            "• lang_code (e.g. en, fr, de)\n"
            "• default_thumb (image URL)\n"
            "• ping_img (image URL)\n"
            "• start_img (image URL)\n"
            "• owner_link (Telegram profile or contact URL)\n\n"
            "Use <code>/config &lt;key&gt; &lt;value&gt;</code> to update a setting.\n"
            "Use <code>/config &lt;key&gt;</code> to view the current value.\n\n"
            "Examples:\n"
            "<code>/config auto_leave true</code>\n"
            "<code>/config lang_code en</code>\n"
            "<code>/config default_thumb https://example.com/thumb.jpg</code>\n"
            "<code>/config ping_img https://example.com/ping.jpg</code>\n"
            "<code>/config owner_link https://t.me/username</code>\n\n"
            "Use <code>/changeowner &lt;user_id&gt;</code> to transfer ownership.\n\n"
            "Use <code>/refreshconfig</code> to reload the deployment <code>.env</code>, "
            "database overrides, and language files without restarting."
        )
        await m.reply_text(text)
        return

    key = m.command[1].upper()
    if key == "OWNER_ID":
        return await m.reply_text(
            "👑 Ownership is managed separately.\n\n"
            "💡 Use <code>/changeowner &lt;new_owner_user_id&gt;</code>."
        )
    if key not in VALID_KEYS:
        return await m.reply_text(
            "❌ Invalid key. Available keys: auto_leave, auto_end, thumb_gen, video_play, lang_code, default_thumb, ping_img, start_img, owner_link"
        )

    if len(m.command) == 2:
        stored_value = runtime.get(key)
        current_value = stored_value if stored_value is not None else getattr(config, key, None)
        await m.reply_text(
            f"<b>{key}</b> = <code>{short(str(current_value))}</code>\n"
            f"({'override' if stored_value is not None else 'default'})"
        )
        return

    value = " ".join(m.command[2:]).strip()
    if not value:
        return await m.reply_text(f"❌ Please provide a value for <code>{key}</code>.")

    if key == "OWNER_LINK" and not value.startswith(("https://t.me/", "tg://")):
        return await m.reply_text(
            "❌ Owner link must be a Telegram link.\n\n"
            "💡 Example: <code>https://t.me/ViPdEeE</code>"
        )

    if key in BOOL_KEYS:
        if value.lower() not in ("true", "false", "on", "off", "yes", "no", "1", "0"):
            return await m.reply_text(
                "❌ Boolean values must be true or false. Example: <code>true</code> or <code>false</code>."
            )
        value = value.lower() in ("true", "on", "yes", "1")

    await db.set_config(key, value)
    config.apply_runtime_config({key: value})

    await m.reply_text(
        f"✅ Updated <code>{key}</code>.\n"
        f"Current value: <code>{short(str(value))}</code>"
    )


@app.on_message(filters.command(["getconfig"]) & ~app.bl_users)
@lang.language()
async def _get_setting(_, m: types.Message):
    """Get a specific setting value. Usage: /getconfig KEY"""
    if m.chat.type != enums.ChatType.PRIVATE:
        return await m.reply_text(
            "This command works in private chats only, because it reads live bot settings."
        )
    if m.from_user.id not in app.sudoers:
        return await m.reply_text(
            "You need sudo access to use this command, because it reads live bot settings."
        )

    if len(m.command) < 2:
        return await m.reply_text("Usage: /getconfig &lt;key&gt;")
    
    key = m.command[1].upper()
    value = await db.get_config(key)
    
    if value is None:
        # Return the default from config object
        default = getattr(config, key, None)
        if default is None:
            return await m.reply_text(f"❌ Setting <code>{key}</code> not found")
        return await m.reply_text(f"<code>{key}</code> = <code>{default}</code>\n(default)")
    
    # Mask sensitive values
    if key == "API_KEY":
        value = "***"
    
    await m.reply_text(f"<code>{key}</code> = <code>{value}</code>\n(override)")


@app.on_message(filters.command(["resetconfig"]) & ~app.bl_users)
@lang.language()
async def _reset_setting(_, m: types.Message):
    """Reset a setting to its default (environment) value. Usage: /resetconfig KEY"""
    if m.chat.type != enums.ChatType.PRIVATE:
        return await m.reply_text(
            "This command works in private chats only, because it resets live bot settings."
        )
    if m.from_user.id not in app.sudoers:
        return await m.reply_text(
            "You need sudo access to use this command, because it resets live bot settings."
        )

    if len(m.command) < 2:
        return await m.reply_text("Usage: /resetconfig &lt;key&gt;")
    
    key = m.command[1].upper()
    
    valid_keys = {
        "AUTO_LEAVE", "AUTO_END", "THUMB_GEN", "VIDEO_PLAY",
        "LANG_CODE", "DEFAULT_THUMB", "PING_IMG", "START_IMG", "OWNER_LINK",
    }
    
    if key not in valid_keys:
        return await m.reply_text(
            "❌ Invalid setting key. Available keys: auto_leave, auto_end, thumb_gen, video_play, lang_code, default_thumb, ping_img, start_img, owner_link"
        )
    
    original_value = config._runtime_defaults.get(key)
    
    # Delete from MongoDB
    await db.delete_config(key)
    
    # Update the live config
    config.apply_runtime_config({key: original_value})
    
    await m.reply_text(f"✅ Setting <code>{key}</code> reset to default.\nValue: <code>{original_value}</code>")
