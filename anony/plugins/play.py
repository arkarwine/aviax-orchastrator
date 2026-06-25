# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import asyncio
import re
import time
from functools import wraps
from html import escape
from pathlib import Path
from uuid import uuid4

from pyrogram import filters, types
from pyrogram.errors import ChatSendMediaForbidden, ChatSendPhotosForbidden

from anony import anon, app, config, db, lang, logger, queue, tg, thumb, yt
from anony.core.calls import PlaybackRecoveryQueued
from anony.helpers import Track, buttons, utils
from anony.helpers._feedback import (
    DOWNLOAD_CUSTOM,
    SEARCH_CUSTOM,
    edit_status,
    reply_status,
)
from anony.helpers._play import checkUB

active_play_requests = 0


def track_play_request(handler):
    @wraps(handler)
    async def wrapper(*args, **kwargs):
        global active_play_requests
        active_play_requests += 1
        try:
            return await handler(*args, **kwargs)
        finally:
            active_play_requests = max(0, active_play_requests - 1)

    return wrapper


def playlist_to_queue(chat_id: int, tracks: list) -> str:
    text = "<blockquote expandable>"
    for track in tracks:
        pos = queue.add(chat_id, track)
        text += f"<b>{pos}.</b> {track.title}\n"
    text = text[:1948] + "</blockquote>"
    return text


def format_wait(seconds: int) -> str:
    if seconds <= 0:
        return "starting shortly"
    minutes = max(1, round(seconds / 60))
    return f"about {minutes} minute{'s' if minutes != 1 else ''}"


def can_send_song_file(media) -> bool:
    if not media or getattr(media, "video", False):
        return False
    file_path = getattr(media, "file_path", None)
    return bool(file_path and Path(file_path).is_file())


def format_clock(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return time.strftime("%H:%M:%S", time.gmtime(seconds))
    return time.strftime("%M:%S", time.gmtime(seconds))


def clean_file_name(value: str, fallback: str = "song") -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "", (value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or fallback


def media_title_html(media) -> str:
    title = escape(getattr(media, "title", None) or "Unknown track")
    url = getattr(media, "url", None)
    return f'<a href="{url}">{title}</a>' if url else f"<b>{title}</b>"


def media_artist(media) -> str | None:
    artist = getattr(media, "channel_name", None)
    if artist:
        return artist.strip()
    return None


def build_now_playing_text(chat_id: int, media) -> str:
    next_media = queue.get_next(chat_id, check=True)
    queue_length = max(0, len(queue.get_queue(chat_id)) - 1)
    duration_text = getattr(media, "duration", None) or (
        format_clock(media.duration_sec)
        if getattr(media, "duration_sec", 0)
        else "Live"
    )
    text = (
        "▶️ <b>Now playing</b>\n\n"
        f"🎵 <b>Title:</b> {media_title_html(media)}\n"
        f"⏱️ Duration: <code>{duration_text}</code>\n"
        f"🙋 Requested by: {getattr(media, 'user', 'Unknown')}"
    )
    if getattr(media, "duration_sec", 0) > 0 and getattr(media, "time", 0) > 0:
        played = min(max(0, int(media.time)), int(media.duration_sec))
        text += f"\n⏳ Progress: <code>{format_clock(played)} / {format_clock(media.duration_sec)}</code>"
    text += (
        "\n\n"
        f"⏭️ Next: <b>{escape(next_media.title) if next_media and next_media.title else 'Nothing queued'}</b>\n"
        f"📋 Queue: <code>{queue_length}</code> waiting"
    )
    return text


def song_export_path(media) -> Path:
    downloads_dir = (
        Path(config.DOWNLOADS_PATH)
        if config.DOWNLOADS_PATH
        else Path.cwd() / "downloads"
    )
    export_dir = downloads_dir / "songs"
    export_dir.mkdir(parents=True, exist_ok=True)
    return export_dir / f"{clean_file_name(getattr(media, 'id', 'current-song'))}.mp3"


async def ensure_song_mp3(media) -> Path:
    if not can_send_song_file(media):
        raise FileNotFoundError("Source audio file is unavailable")

    source_path = Path(str(media.file_path))
    export_path = song_export_path(media)
    if (
        export_path.exists()
        and export_path.stat().st_size > 0
        and export_path.stat().st_mtime >= source_path.stat().st_mtime
    ):
        return export_path

    title = getattr(media, "title", None) or source_path.stem
    artist = media_artist(media)
    temp_path = export_path.with_name(f"{export_path.stem}.tmp.mp3")
    temp_path.unlink(missing_ok=True)

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "2",
        "-map_metadata",
        "-1",
        "-metadata",
        f"title={title}",
    ]
    if artist:
        command.extend(["-metadata", f"artist={artist}"])
    command.append(str(temp_path))

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("MP3 conversion timed out") from None

    if (
        process.returncode != 0
        or not temp_path.exists()
        or temp_path.stat().st_size <= 0
    ):
        temp_path.unlink(missing_ok=True)
        error_text = (stderr or b"").decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(error_text or "MP3 conversion failed")

    temp_path.replace(export_path)
    return export_path


async def send_song_file(
    chat_id: int,
    media,
    *,
    reply_to_message_id: int | None = None,
) -> bool:
    export_path = await ensure_song_mp3(media)
    title = getattr(media, "title", None) or export_path.stem
    performer = media_artist(media)
    send_kwargs = {
        "chat_id": chat_id,
        "audio": str(export_path),
        "title": title,
        "file_name": f"{clean_file_name(title)}.mp3",
        "disable_notification": True,
    }
    if performer:
        send_kwargs["performer"] = performer
    if getattr(media, "duration_sec", 0) > 0:
        send_kwargs["duration"] = media.duration_sec
    if reply_to_message_id:
        send_kwargs["reply_to_message_id"] = reply_to_message_id

    await app.send_audio(**send_kwargs)
    return True


async def send_now_playing_card(message: types.Message, media) -> None:
    text = build_now_playing_text(message.chat.id, media)
    keyboard = buttons.controls(message.chat.id)
    thumb_path = None
    if config.THUMB_GEN:
        try:
            thumb_path = (
                await thumb.generate(media)
                if isinstance(media, Track)
                else config.DEFAULT_THUMB
            )
        except Exception as exc:
            logger.warning(
                "Could not build now-playing artwork chat=%s media=%s: %s",
                message.chat.id,
                getattr(media, "id", "unknown"),
                exc,
            )
    if thumb_path:
        try:
            if str(thumb_path).lower().endswith(".gif"):
                await message.reply_animation(
                    animation=thumb_path,
                    caption=text,
                    reply_markup=keyboard,
                )
            else:
                await message.reply_photo(
                    photo=thumb_path,
                    caption=text,
                    reply_markup=keyboard,
                )
            return
        except (ChatSendMediaForbidden, ChatSendPhotosForbidden):
            pass
        except Exception as exc:
            logger.warning(
                "Could not send now-playing media card chat=%s media=%s: %s",
                message.chat.id,
                getattr(media, "id", "unknown"),
                exc,
            )
    await message.reply_text(text, reply_markup=keyboard)


async def send_play_log_safely(
    m: types.Message,
    sent: types.Message,
    title: str,
    duration: str,
) -> None:
    try:
        try:
            link = sent.link or ""
        except Exception:
            link = ""
        await asyncio.wait_for(
            utils.play_log(m, link, title, duration),
            timeout=10,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Play log delivery timed out chat=%s; playback continued", m.chat.id
        )
    except Exception as exc:
        logger.warning("Play log delivery failed chat=%s: %s", m.chat.id, exc)


@app.on_message(
    filters.command(["play", "playforce", "vplay", "vplayforce"])
    & filters.group
    & ~app.bl_users
)
@lang.language()
@checkUB
@track_play_request
async def play_hndlr(
    _,
    m: types.Message,
    force: bool = False,
    m3u8: bool = False,
    video: bool = False,
    url: str = None,
) -> None:
    sent = await reply_status(m, SEARCH_CUSTOM, "🔎", "Searching for your track...")
    marker = Path(".restart-when-idle")
    if not marker.exists() and queue.get_deferred(m.chat.id):
        anon._maintenance_restore_attempts.pop(m.chat.id, None)
        await anon.resume_maintenance_queues()
    file = None
    mention = m.from_user.mention
    media = tg.get_media(m.reply_to_message) if m.reply_to_message else None
    tracks = []

    if media:
        setattr(sent, "lang", m.lang)
        await edit_status(
            sent, DOWNLOAD_CUSTOM, "⬇️", "Downloading the replied media..."
        )
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
                tracks = await yt.playlist(config.PLAYLIST_LIMIT, mention, url, video)
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
            await edit_status(
                sent, SEARCH_CUSTOM, "🔎", "Checking the requested link..."
            )
            file = await yt.search(url, sent.id, video=video)

        if not file:
            return await sent.edit_text(
                "❌ I could not find a playable track at that link.\n\n"
                "💡 Check the link, try a song name instead, or use a different public source."
            )

    elif len(m.command) >= 2:
        query = " ".join(m.command[1:])
        await edit_status(
            sent, SEARCH_CUSTOM, "🔎", f"Searching for <b>{escape(query)}</b>..."
        )
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

    await edit_status(
        sent,
        SEARCH_CUSTOM,
        "✅",
        f"Found <b>{escape(file.title)}</b>.\n\n🎚️ Preparing your playback request...",
    )

    try:
        if db.logger:
            asyncio.create_task(
                send_play_log_safely(m, sent, file.title, file.duration),
                name=f"play-log-{m.chat.id}",
            )

        file.user = mention
        file.requester_id = m.from_user.id
        file.queue_id = file.queue_id or uuid4().hex[:10]
        for track in tracks:
            track.requester_id = m.from_user.id
            track.queue_id = track.queue_id or uuid4().hex[:10]

        if queue.get_queue(m.chat.id) and not await db.get_call(m.chat.id):
            if queue.is_interrupted(m.chat.id):
                anon._interrupted_restore_attempts.pop(m.chat.id, None)
                await anon.resume_interrupted_streams()
            elif not queue.get_deferred(m.chat.id):
                logger.warning(
                    "Clearing stale playback queue before new start chat=%s", m.chat.id
                )
                queue.clear(m.chat.id)

        duplicate = queue.duplicate_position(m.chat.id, file.id)
        if duplicate >= 0 and not force:
            return await sent.edit_text(
                "♻️ <b>This track is already queued.</b>\n\n"
                f"🎵 {file.title}\n"
                f"📍 Existing position: <code>{duplicate}</code>\n\n"
                "💡 I kept the original request instead of adding a duplicate."
            )

        pending_by_user = queue.requester_pending_count(m.chat.id, m.from_user.id)
        if (
            pending_by_user >= config.USER_QUEUE_LIMIT
            and not force
            and m.from_user.id not in app.sudoers
        ):
            return await sent.edit_text(
                "⚖️ <b>Your personal queue limit is full.</b>\n\n"
                f"You already have <code>{pending_by_user}</code> pending requests in this chat.\n"
                "💡 Remove one of your queued tracks or wait for it to play before adding another."
            )
        if tracks and not force and m.from_user.id not in app.sudoers:
            available = max(0, config.USER_QUEUE_LIMIT - pending_by_user - 1)
            tracks = tracks[:available]
    except Exception:
        logger.exception("Playback request preparation failed chat=%s", m.chat.id)
        return await sent.edit_text(
            "❌ <b>I could not prepare this playback request.</b>\n\n"
            "The track was found, but its queue information could not be prepared.\n"
            "💡 Try again once. If it repeats, send this message to the bot owner."
        )

    maintenance_pending = getattr(m, "maintenance_restart", False) or marker.exists()
    restoration_pending = bool(queue.get_deferred(m.chat.id))
    if maintenance_pending or restoration_pending:
        deferred = [file, *tracks]
        for item in deferred:
            item.maintenance_id = uuid4().hex[:10]
            item.maintenance_owner_id = m.from_user.id
        try:
            positions = queue.defer_many(m.chat.id, deferred)
        except TypeError:
            logger.exception(
                "Maintenance queue contains unsupported data chat=%s", m.chat.id
            )
            return await sent.edit_text(
                "❌ <b>I could not save this request because its track data is invalid.</b>\n\n"
                "💡 Try the request again. If it repeats for this track, send its link to the bot owner."
            )
        except PermissionError:
            logger.exception("Maintenance queue is not writable chat=%s", m.chat.id)
            return await sent.edit_text(
                "❌ <b>I could not save this request because maintenance storage is not writable.</b>\n\n"
                "💡 The bot owner needs to restore write permission for the deployment folder."
            )
        except OSError as exc:
            logger.exception("Could not persist maintenance queue chat=%s", m.chat.id)
            detail = (
                "The server does not have enough free storage."
                if getattr(exc, "errno", None) == 28
                else "The server could not update maintenance storage."
            )
            return await sent.edit_text(
                "❌ <b>I could not safely save this request for maintenance.</b>\n\n"
                f"{detail}\n"
                "💡 Please try again shortly. The bot owner has been given a precise error in the logs."
            )
        except Exception:
            logger.exception("Could not persist maintenance queue chat=%s", m.chat.id)
            return await sent.edit_text(
                "❌ <b>I could not safely save this request for maintenance.</b>\n\n"
                "An unexpected track-storage error occurred.\n"
                "💡 Please try again shortly. The bot owner can inspect the precise error in the logs."
            )
        grace = anon.maintenance_grace_remaining()
        grace_text = (
            f"approximately <code>{max(1, (grace + 59) // 60)}</code> minute(s) remain in the grace period"
            if grace is not None and grace > 0
            else (
                "maintenance will begin after the currently playing tracks finish"
                if maintenance_pending
                else "maintenance is complete and saved requests are being restored"
            )
        )
        maintenance_text = (
            (
                "🛠️ <b>Queued for scheduled maintenance restart</b>\n\n"
                if maintenance_pending
                else "🛠️ <b>Queued behind saved maintenance requests</b>\n\n"
            )
            + f"🎵 <b>Title:</b> <a href={file.url}>{file.title}</a>\n"
            + f"📥 Maintenance queue position: <code>{positions[0]}</code>\n\n"
            + f"⏱️ {grace_text.capitalize()}.\n"
            + (
                "▶️ Existing playback may continue during the grace period.\n"
                if maintenance_pending
                else "▶️ Saved requests are being restored in their original order.\n"
            )
            + "💾 This request is saved and will begin automatically after the maintenance restart."
            + (
                f"\n\n📚 The other <code>{len(tracks)}</code> playlist tracks were saved too."
                if tracks
                else ""
            )
        )
        return await sent.edit_text(
            maintenance_text,
            reply_markup=buttons.maintenance_receipt(
                m.chat.id,
                file.maintenance_id,
                m.from_user.id,
            ),
        )

    try:
        if force:
            queue.force_add(m.chat.id, file)
            position = 0
        else:
            position = queue.add(m.chat.id, file)
    except TypeError:
        logger.exception("Playback queue contains unsupported data chat=%s", m.chat.id)
        return await sent.edit_text(
            "❌ <b>I could not queue this track because its data is invalid.</b>\n\n"
            "💡 Try the request again. If it repeats for this track, send its link to the bot owner."
        )
    except PermissionError:
        logger.exception("Playback queue is not writable chat=%s", m.chat.id)
        return await sent.edit_text(
            "❌ <b>I could not queue this track because playback storage is not writable.</b>\n\n"
            "💡 The bot owner needs to restore write permission for the deployment folder."
        )
    except OSError as exc:
        logger.exception("Could not persist playback queue chat=%s", m.chat.id)
        detail = (
            "The server does not have enough free storage."
            if getattr(exc, "errno", None) == 28
            else "The server could not update playback storage."
        )
        return await sent.edit_text(
            "❌ <b>I could not safely save this playback request.</b>\n\n"
            f"{detail}\n"
            "💡 Please try again shortly. The bot owner can inspect the precise error in the logs."
        )
    except Exception:
        logger.exception("Could not persist playback queue chat=%s", m.chat.id)
        return await sent.edit_text(
            "❌ <b>I could not save this playback request.</b>\n\n"
            "An unexpected track-storage error occurred.\n"
            "💡 Please try again shortly. The bot owner can inspect the precise error in the logs."
        )

    if not force:
        if position != 0 or await db.get_call(m.chat.id):
            wait = format_wait(queue.estimated_wait(m.chat.id, position))
            await sent.edit_text(
                "📥 <b>Added to playback queue</b>\n\n"
                f"🎵 <b>Title:</b> <a href={file.url}>{file.title}</a>\n"
                f"⏱️ Duration: <code>{file.duration}</code>\n"
                f"📍 Position: <code>{position}</code>\n"
                f"⌛ Estimated wait: <code>{wait}</code>\n"
                f"🙋 Requested by: {m.from_user.mention}",
                reply_markup=buttons.queue_receipt(
                    m.chat.id,
                    file.queue_id,
                    m.from_user.id,
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
        downloads_dir = (
            Path(config.DOWNLOADS_PATH)
            if config.DOWNLOADS_PATH
            else Path.cwd() / "downloads"
        )
        downloads_dir.mkdir(parents=True, exist_ok=True)
        fname = downloads_dir / f"{file.id}.{'mp4' if video else 'webm'}"
        if fname.exists():
            file.file_path = str(fname)
        else:
            await edit_status(
                sent,
                DOWNLOAD_CUSTOM,
                "⬇️",
                f"<b>{escape(file.title)}</b>\n\nDownloading and preparing the audio stream...",
            )
            try:
                file.file_path = await asyncio.wait_for(
                    yt.download(file.id, video=video),
                    timeout=180,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Track download timed out chat=%s media=%s", m.chat.id, file.id
                )
                file.file_path = None
            except Exception:
                logger.exception("Track download failed in chat %s", m.chat.id)
                file.file_path = None
            if not file.file_path:
                queue.remove_current_if(m.chat.id, file.queue_id)
                return await sent.edit_text(
                    "❌ I found the track, but could not download it.\n\n"
                    "💡 The source may be restricted or temporarily unavailable. Try another result or link."
                )

    try:
        await edit_status(
            sent,
            SEARCH_CUSTOM,
            "🎵",
            f"<b>{escape(file.title)}</b>\n\nConnecting to the voice chat...",
        )
        await anon.play_media(chat_id=m.chat.id, message=sent, media=file)
    except PlaybackRecoveryQueued:
        return
    except Exception:
        logger.exception("Playback start failed in chat %s", m.chat.id)
        queue.remove_current_if(m.chat.id, file.queue_id)
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


@app.on_message(filters.command(["nowplaying", "np"]) & filters.group & ~app.bl_users)
@lang.language()
async def now_playing_hndlr(_, m: types.Message) -> None:
    media = queue.get_current(m.chat.id)
    if not media or not await db.get_call(m.chat.id):
        return await m.reply_text("❌ Nothing is currently playing.")
    await send_now_playing_card(m, media)


@app.on_message(filters.command(["song"]) & filters.group & ~app.bl_users)
@lang.language()
async def song_hndlr(_, m: types.Message) -> None:
    media = queue.get_current(m.chat.id)
    if not media or not await db.get_call(m.chat.id):
        return await m.reply_text("❌ No song is currently playing.")

    if getattr(media, "video", False):
        return await m.reply_text(
            "❌ The current playback is a video stream, so there is no song file to send."
        )

    if not can_send_song_file(media):
        if getattr(media, "file_path", None):
            return await m.reply_text(
                "❌ The current song file is no longer available on disk."
            )
        return await m.reply_text(
            "❌ The current track is being streamed from a source that cannot be sent as a song file."
        )

    sent = await m.reply_text(
        f"🎵 Preparing MP3 for <b>{escape(media.title or 'current song')}</b>..."
    )
    try:
        await send_song_file(
            m.chat.id,
            media,
            reply_to_message_id=m.id,
        )
    except Exception:
        logger.exception(
            "Could not send current song file chat=%s media=%s",
            m.chat.id,
            getattr(media, "id", "unknown"),
        )
        return await sent.edit_text(
            "❌ I could not prepare the MP3 for this song. Please try again shortly."
        )

    await sent.delete()
