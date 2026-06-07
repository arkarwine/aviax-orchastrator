# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import os
from pathlib import Path

from pyrogram import filters, types

from anony import app, db, lang, queue


@app.on_message(filters.command(["ac", "activevc"]) & app.sudoers)
@lang.language()
async def _activevc(_, m: types.Message):
    if not db.active_calls:
        return await m.reply_text(m.lang["vc_empty"])

    if m.command[0] == "ac":
        return await m.reply_text(m.lang["vc_count"].format(len(db.active_calls)))

    sent = await m.reply_text(m.lang["vc_fetching"])
    text = ""

    for i, chat in enumerate(db.active_calls):
        playing = queue.get_current(chat)
        text += f"\n{i+1}. <code>{chat}</code>\n    ➜ {playing.title[:25]}"

    if len(text) < 4000:
        return await sent.edit_text(m.lang["vc_list"] + text)

    temp_file = Path.cwd() / "cache" / "activevc.txt"
    temp_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file.write_text(text, encoding="utf-8")

    await sent.edit_media(
        media=types.InputMediaDocument(
            media=str(temp_file),
            caption=m.lang["vc_list"],
        )
    )
    temp_file.unlink(missing_ok=True)
