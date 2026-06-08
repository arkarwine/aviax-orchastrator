from pyrogram import types


SEARCH_CUSTOM = "<tg-emoji emoji-id='5406745015365943482'>⌛</tg-emoji>"
DOWNLOAD_CUSTOM = "<tg-emoji emoji-id='5406745015365943482'>⬇️</tg-emoji>"


async def reply_status(
    message: types.Message,
    custom_emoji: str,
    fallback_emoji: str,
    text: str,
) -> types.Message:
    try:
        return await message.reply_text(f"{custom_emoji} {text}")
    except Exception:
        return await message.reply_text(f"{fallback_emoji} {text}")


async def edit_status(
    message: types.Message,
    custom_emoji: str,
    fallback_emoji: str,
    text: str,
) -> types.Message:
    try:
        return await message.edit_text(f"{custom_emoji} {text}")
    except Exception:
        return await message.edit_text(f"{fallback_emoji} {text}")
