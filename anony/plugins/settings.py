# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


from pyrogram import enums, filters, types

from anony import app, config, db, lang, logger


VALID_KEYS = [
    "AUTO_LEAVE",
    "AUTO_END",
    "THUMB_GEN",
    "VIDEO_PLAY",
    "LANG_CODE",
    "DEFAULT_THUMB",
    "PING_IMG",
    "START_IMG",
    "OWNER_ID",
]
BOOL_KEYS = {"AUTO_LEAVE", "AUTO_END", "THUMB_GEN", "VIDEO_PLAY"}


async def apply_owner_id(value: int) -> None:
    previous_owner = app.owner
    await db.set_config("OWNER_ID", value)
    await db.add_sudo(value)
    config.apply_runtime_config({"OWNER_ID": value})
    app.owner = value
    app.sudoers.add(value)
    if previous_owner and previous_owner != value:
        app.sudoers.discard(previous_owner)
        try:
            await db.del_sudo(previous_owner)
        except Exception:
            logger.warning("Owner changed, but old owner sudo cleanup failed.")


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
            "• owner_id (Telegram user ID, owner only)\n\n"
            "Use <code>/config &lt;key&gt; &lt;value&gt;</code> to update a setting.\n"
            "Use <code>/config &lt;key&gt;</code> to view the current value.\n\n"
            "Examples:\n"
            "<code>/config auto_leave true</code>\n"
            "<code>/config lang_code en</code>\n"
            "<code>/config owner_id 123456789</code>\n"
            "<code>/config default_thumb https://example.com/thumb.jpg</code>\n"
            "<code>/config ping_img https://example.com/ping.jpg</code>\n"
        )
        await m.reply_text(text)
        return

    key = m.command[1].upper()
    if key not in VALID_KEYS:
        return await m.reply_text(
            "❌ Invalid key. Available keys: auto_leave, auto_end, thumb_gen, video_play, lang_code, default_thumb, ping_img, start_img, owner_id"
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

    if key == "OWNER_ID" and m.from_user.id != app.owner:
        return await m.reply_text(
            "🔒 Only the current owner can change <code>OWNER_ID</code>.\n\n"
            "💡 Ask the owner to run <code>/config owner_id &lt;user_id&gt;</code>."
        )

    if key in BOOL_KEYS:
        if value.lower() not in ("true", "false", "on", "off", "yes", "no", "1", "0"):
            return await m.reply_text(
                "❌ Boolean values must be true or false. Example: <code>true</code> or <code>false</code>."
            )
        value = value.lower() in ("true", "on", "yes", "1")

    if key == "OWNER_ID":
        if not value.isdigit() or int(value) <= 0:
            return await m.reply_text(
                "❌ Owner ID must be a positive number.\n\n"
                "💡 Use the Telegram user ID, for example <code>123456789</code>."
            )
        value = int(value)
        status = await m.reply_text("👑 Updating bot owner...")
        try:
            await apply_owner_id(value)
        except Exception:
            logger.exception("Could not update owner id")
            return await status.edit_text(
                "❌ I could not update the owner.\n\n"
                "💡 Check the database connection and try again."
            )
        return await status.edit_text(
            f"✅ Owner updated to <code>{value}</code>.\n\n"
            "👑 The new owner now has sudo access too."
        )

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
        "LANG_CODE", "DEFAULT_THUMB", "PING_IMG", "START_IMG",
    }
    
    if key not in valid_keys:
        return await m.reply_text(
            "❌ Invalid setting key. Available keys: auto_leave, auto_end, thumb_gen, video_play, lang_code, default_thumb, ping_img, start_img"
        )
    
    # Get the original env value (reload from Config defaults)
    from config import Config as OriginalConfig
    original_config = OriginalConfig()
    original_value = getattr(original_config, key, None)
    
    # Delete from MongoDB
    await db.delete_config(key)
    
    # Update the live config
    config.apply_runtime_config({key: original_value})
    
    await m.reply_text(f"✅ Setting <code>{key}</code> reset to default.\nValue: <code>{original_value}</code>")
