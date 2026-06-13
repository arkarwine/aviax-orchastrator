# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


from html import escape
from pathlib import Path

from pyrogram import filters, types

from anony import anon, app, config, db, lang, logger, queue, tg, yt
from anony.core.calls import PlaybackRecoveryQueued
from anony.helpers import buttons, utils
from anony.helpers._feedback import (
    DOWNLOAD_CUSTOM,
    SEARCH_CUSTOM,
    edit_status,
    reply_status,
)
from anony.helpers._play import checkUB


def playlist_to_queue(chat_id: int, tracks: list) -> str:
    text = "<blockquote expandable>"
    for track in tracks:
        pos = queue.add(chat_id, track)
        text += f"<b>{pos}.</b> {track.title}\n"
    text = text[:1948] + "</blockquote>"
    return text

@app.on_message(
    filters.command(["play", "playforce", "vplay", "vplayforce"])
    & filters.group
    & ~app.bl_users
)
@lang.language()
@checkUB
async def play_hndlr(
    _,
    m: types.Message,
    force: bool = False,
    m3u8: bool = False,
    video: bool = False,
    url: str = None,
) -> None:
    sent = await reply_status(m, SEARCH_CUSTOM, "🔎", "Searching for your track...")
    file = None
    mention = m.from_user.mention
    media = tg.get_media(m.reply_to_message) if m.reply_to_message else None
    tracks = []

    if media:
        setattr(sent, "lang", m.lang)
        await edit_status(sent, DOWNLOAD_CUSTOM, "⬇️", "Downloading the replied media...")
        try:
            file = await tg.download(m.reply_to_message, sent)
        except Exception:
            logger.exception("Telegram media download failed in chat %s", m.chat.id)
            return await sent.edit_text(
                "❌ I could not download that Telegram media.\n\n"
                "💡 Make sure the file is still available, under the size limit, and try again."
            )

    elif m3u8:
        await edit_status(sent, SEARCH_CUSTOM, "⌛", "Checking the stream link...")
        try:
            file = await tg.process_m3u8(url, sent.id, video)
        except Exception:
            logger.exception("Stream link processing failed in chat %s", m.chat.id)
            return await sent.edit_text(
                "❌ I could not use that stream link.\n\n"
                "💡 Check that the link is public, active, and points directly to a playable stream."
            )

    elif url:
        if "playlist" in url:
            await edit_status(sent, SEARCH_CUSTOM, "⌛", "Fetching the playlist...")
            try:
                tracks = await yt.playlist(
                    config.PLAYLIST_LIMIT, mention, url, video
                )
            except Exception:
                logger.exception("Playlist lookup failed in chat %s", m.chat.id)
                tracks = []

            if not tracks:
                return await sent.edit_text(
                    "❌ I could not read that playlist.\n\n"
                    "💡 Make sure it is public, contains playable tracks, and try again."
                )

            file = tracks[0]
            tracks.remove(file)
            file.message_id = sent.id
        else:
            await edit_status(sent, SEARCH_CUSTOM, "🔎", "Checking the requested link...")
            file = await yt.search(url, sent.id, video=video)

        if not file:
            return await sent.edit_text(
                "❌ I could not find a playable track at that link.\n\n"
                "💡 Check the link, try a song name instead, or use a different public source."
            )

    elif len(m.command) >= 2:
        query = " ".join(m.command[1:])
        await edit_status(sent, SEARCH_CUSTOM, "🔎", f"Searching for <b>{escape(query)}</b>...")
        file = await yt.search(query, sent.id, video=video)
        if not file:
            return await sent.edit_text(
                "❌ I could not find a playable result.\n\n"
                "💡 Try a more specific song title, include the artist name, or send a direct link."
            )

    if not file:
        return await sent.edit_text(m.lang["play_usage"])

    if file.duration_sec > config.DURATION_LIMIT:
        return await sent.edit_text(
            m.lang["play_duration_limit"].format(config.DURATION_LIMIT // 60)
        )

    if await db.is_logger():
        await utils.play_log(m, sent.link, file.title, file.duration)

    file.user = mention
    if force:
        queue.force_add(m.chat.id, file)
    else:
        position = queue.add(m.chat.id, file)

        if position != 0 or await db.get_call(m.chat.id):
            await sent.edit_text(
                m.lang["play_queued"].format(
                    position,
                    file.url,
                    file.title,
                    file.duration,
                    m.from_user.mention,
                ),
                reply_markup=buttons.play_queued(
                    m.chat.id, file.id, m.lang["play_now"]
                ),
            )
            if tracks:
                added = playlist_to_queue(m.chat.id, tracks)
                await app.send_message(
                    chat_id=m.chat.id,
                    text=m.lang["playlist_queued"].format(len(tracks)) + added,
                )
            return

    if not file.file_path:
        downloads_dir = Path(config.DOWNLOADS_PATH) if config.DOWNLOADS_PATH else Path.cwd() / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        fname = downloads_dir / f"{file.id}.{'mp4' if video else 'webm'}"
        if fname.exists():
            file.file_path = str(fname)
        else:
            await edit_status(sent, DOWNLOAD_CUSTOM, "⬇️", "Downloading and preparing the track...")
            try:
                file.file_path = await yt.download(file.id, video=video)
            except Exception:
                logger.exception("Track download failed in chat %s", m.chat.id)
                file.file_path = None
            if not file.file_path:
                return await sent.edit_text(
                    "❌ I found the track, but could not download it.\n\n"
                    "💡 The source may be restricted or temporarily unavailable. Try another result or link."
                )

    try:
        await anon.play_media(chat_id=m.chat.id, message=sent, media=file)
    except PlaybackRecoveryQueued:
        return
    except Exception:
        logger.exception("Playback start failed in chat %s", m.chat.id)
        return await sent.edit_text(
            "❌ The track is ready, but playback could not start.\n\n"
            "💡 Make sure the assistant is in the group, can join voice chats, and a voice chat is available."
        )
    if not tracks:
        return
    added = playlist_to_queue(m.chat.id, tracks)
    await app.send_message(
        chat_id=m.chat.id,
        text=m.lang["playlist_queued"].format(len(tracks)) + added,
    )
