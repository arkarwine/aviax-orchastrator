from pyrogram import filters, types

from anony import app
from anony.helpers import maintenance_status_text


@app.on_message(filters.command("maintenance") & filters.group & ~app.bl_users)
async def maintenance_status(_, message: types.Message) -> None:
    await message.reply_text(await maintenance_status_text(message.chat.id))
