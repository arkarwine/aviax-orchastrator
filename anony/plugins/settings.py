# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


from pyrogram import filters, types

from anony import app, config, db, lang


@app.on_message(filters.command(["settings"]) & app.sudoers & ~app.bl_users & filters.private)
@lang.language()
async def _settings_list(_, m: types.Message):
    """List all current settings including runtime overrides."""
    runtime = await db.get_all_config()
    text = "<b>📋 Bot Settings</b>\n\n"
    
    def short(value: str, limit: int = 50) -> str:
        return value if not value or len(value) <= limit else value[:limit - 3] + "..."

    settings_info = [
        ("API_URL", config.API_URL, runtime.get("API_URL")),
        ("VIDEO_API_URL", config.VIDEO_API_URL, runtime.get("VIDEO_API_URL")),
        ("API_KEY", "***" if config.API_KEY else "(not set)", "***" if runtime.get("API_KEY") else None),
        ("AUTO_LEAVE", str(config.AUTO_LEAVE), runtime.get("AUTO_LEAVE")),
        ("AUTO_END", str(config.AUTO_END), runtime.get("AUTO_END")),
        ("THUMB_GEN", str(config.THUMB_GEN), runtime.get("THUMB_GEN")),
        ("VIDEO_PLAY", str(config.VIDEO_PLAY), runtime.get("VIDEO_PLAY")),
        ("LANG_CODE", config.LANG_CODE, runtime.get("LANG_CODE")),
        ("DEFAULT_THUMB", short(config.DEFAULT_THUMB), short(runtime.get("DEFAULT_THUMB", ""))),
        ("PING_IMG", short(config.PING_IMG), short(runtime.get("PING_IMG", ""))),
        ("START_IMG", short(config.START_IMG), short(runtime.get("START_IMG", ""))),
        (
            "COOKIES_URL",
            " ".join(config.COOKIES_URL) or "(none)",
            " ".join(runtime.get("COOKIES_URL")) if runtime.get("COOKIES_URL") else None,
        ),
        (
            "DOWNLOADS_PATH",
            str(config.DOWNLOADS_PATH) if config.DOWNLOADS_PATH else "(default)",
            str(runtime.get("DOWNLOADS_PATH")) if runtime.get("DOWNLOADS_PATH") else None,
        ),
    ]
    
    for key, default, override in settings_info:
        value = override if override is not None else default
        marker = " 🔄" if override is not None else ""
        text += f"• <code>{key}</code>: <code>{value}</code>{marker}\n"
    
    await m.reply_text(text)


@app.on_message(filters.command(["set"]) & app.sudoers & filters.private)
@lang.language()
async def _set_setting(_, m: types.Message):
    """Set a runtime configuration value. Usage: /set KEY VALUE"""
    if len(m.command) < 3:
        return await m.reply_text(
            m.lang.get(
                "setting_usage",
                "Usage: /set <key> <value>\n"
                "Valid keys: api_url, video_api_url, api_key, auto_leave, auto_end, "
                "thumb_gen, video_play, lang_code, default_thumb, ping_img, start_img, "
                "cookies_url, downloads_path"
            )
        )
    
    key = m.command[1].upper()
    value = " ".join(m.command[2:])
    
    # Validate keys
    valid_keys = {
        "API_URL", "VIDEO_API_URL", "API_KEY", "AUTO_LEAVE", "AUTO_END",
        "THUMB_GEN", "VIDEO_PLAY", "LANG_CODE", "DEFAULT_THUMB", "PING_IMG", "START_IMG",
        "COOKIES_URL", "DOWNLOADS_PATH"
    }
    
    if key not in valid_keys:
        return await m.reply_text(f"❌ Invalid setting key: <code>{key}</code>\nValid keys: {', '.join(sorted(valid_keys))}")
    
    # Handle boolean values
    if key in ("AUTO_LEAVE", "AUTO_END", "THUMB_GEN", "VIDEO_PLAY"):
        if value.lower() not in ("true", "false", "on", "off", "yes", "no", "1", "0"):
            return await m.reply_text(f"❌ {key} must be true/false")
        value = value.lower() in ("true", "on", "yes", "1")
    
    # Set in MongoDB and update config object
    await db.set_config(key, value)
    config.apply_runtime_config({key: value})
    
    await m.reply_text(f"✅ Setting <code>{key}</code> updated.\nValue: <code>{value}</code>")


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
        "API_URL", "VIDEO_API_URL", "API_KEY", "AUTO_LEAVE", "AUTO_END",
        "THUMB_GEN", "VIDEO_PLAY", "LANG_CODE", "DEFAULT_THUMB", "PING_IMG", "START_IMG",
        "COOKIES_URL", "DOWNLOADS_PATH"
    }
    
    if key not in valid_keys:
        return await m.reply_text(f"❌ Invalid setting key: <code>{key}</code>")
    
    # Get the original env value (reload from Config defaults)
    from config import Config as OriginalConfig
    original_config = OriginalConfig()
    original_value = getattr(original_config, key, None)
    
    # Delete from MongoDB
    await db.delete_config(key)
    
    # Update the live config
    config.apply_runtime_config({key: original_value})
    
    await m.reply_text(f"✅ Setting <code>{key}</code> reset to default.\nValue: <code>{original_value}</code>")
