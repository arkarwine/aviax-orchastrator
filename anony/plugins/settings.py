# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


from pyrogram import filters, types

from anony import app, config, db, lang


@app.on_message(filters.command(["settings", "set"]) & app.sudoers & ~app.bl_users & filters.private)
@lang.language()
async def _settings(_, m: types.Message):
    """Show runtime setting help or update a setting when key/value are provided."""
    runtime = await db.get_all_config()

    valid_keys = [
        "AUTO_LEAVE",
        "AUTO_END",
        "THUMB_GEN",
        "VIDEO_PLAY",
        "LANG_CODE",
        "DEFAULT_THUMB",
        "PING_IMG",
        "START_IMG",
    ]
    bool_keys = {"AUTO_LEAVE", "AUTO_END", "THUMB_GEN", "VIDEO_PLAY"}

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
            "• start_img (image URL)\n\n"
            "Use <code>/settings &lt;key&gt; &lt;value&gt;</code> to update a setting.\n"
            "Use <code>/settings &lt;key&gt;</code> to view the current value.\n\n"
            "Examples:\n"
            "<code>/settings auto_leave true</code>\n"
            "<code>/settings lang_code en</code>\n"
            "<code>/settings default_thumb https://example.com/thumb.jpg</code>\n"
            "<code>/settings ping_img https://example.com/ping.jpg</code>\n"
        )
        await m.reply_text(text)
        return

    key = m.command[1].upper()
    if key not in valid_keys:
        return await m.reply_text(
            "❌ Invalid key. Available keys: auto_leave, auto_end, thumb_gen, video_play, lang_code, default_thumb, ping_img, start_img"
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

    if key in bool_keys:
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


@app.on_message(filters.command(["get"]) & app.sudoers & filters.private)
@lang.language()
async def _get_setting(_, m: types.Message):
    """Get a specific setting value. Usage: /get KEY"""
    if len(m.command) < 2:
        return await m.reply_text("Usage: /get &lt;key&gt;")
    
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


@app.on_message(filters.command(["reset"]) & app.sudoers & filters.private)
@lang.language()
async def _reset_setting(_, m: types.Message):
    """Reset a setting to its default (environment) value. Usage: /reset KEY"""
    if len(m.command) < 2:
        return await m.reply_text("Usage: /reset &lt;key&gt;")
    
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
