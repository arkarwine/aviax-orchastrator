# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import asyncio
import json
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from ntgcalls import (
    ConnectionError,
    ConnectionNotFound,
    RTMPStreamingUnsupported,
    TelegramServerError,
)
from pyrogram import raw
from pyrogram.errors import (
    ChatSendMediaForbidden,
    ChatSendPhotosForbidden,
    MessageIdInvalid,
)
from pyrogram.types import InputMediaAnimation, InputMediaPhoto, Message
from pytgcalls import PyTgCalls, exceptions, types
from pytgcalls.pytgcalls_session import PyTgCallsSession

from anony import app, config, db, lang, logger, queue, thumb, userbot, yt
from anony.helpers import Media, Track, buttons


class PlaybackRecoveryQueued(Exception):
    """Playback stalled twice and a safe deployment restart was queued."""


class TgCall(PyTgCalls):
    def __init__(self):
        self.clients = []
        self._locks = defaultdict(asyncio.Lock)
        self._assistant_locks = defaultdict(asyncio.Lock)
        self._operations = {}
        self._last_stream_end = {}
        self._maintenance_restore_attempts = {}
        self._playback_timeout_count = 0
        self._last_playback_timeout = None
        self._restart_request = None
        self.operation_timeout = 75
        self.play_start_timeout = 30

    @staticmethod
    def _consume_background_exception(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            logger.debug("Background task exception was consumed.", exc_info=True)

    def active_operations(self) -> dict:
        now = time.monotonic()
        return {
            str(chat_id): {
                "stage": operation["stage"],
                "seconds": round(now - operation["started"], 1),
            }
            for chat_id, operation in self._operations.items()
        }

    def playback_diagnostics(self) -> dict:
        return {
            "timeout_count": self._playback_timeout_count,
            "last_timeout": self._last_playback_timeout,
        }

    def restart_request(self) -> dict | None:
        return self._restart_request

    @staticmethod
    def _assistant_slot(client) -> int | str:
        return getattr(client, "session_slot", "unknown")

    @asynccontextmanager
    async def _assistant_operation(self, client):
        async with self._assistant_locks[self._assistant_slot(client)]:
            yield

    @asynccontextmanager
    async def _operation(self, chat_id: int, stage: str):
        async with self._locks[chat_id]:
            started = time.monotonic()
            self._operations[chat_id] = {"stage": stage, "started": started}
            try:
                yield
            finally:
                elapsed = time.monotonic() - started
                self._operations.pop(chat_id, None)
                if elapsed > 30:
                    logger.warning(
                        "Slow playback transition chat=%s stage=%s elapsed=%.1fs",
                        chat_id,
                        stage,
                        elapsed,
                    )

    async def pause(self, chat_id: int) -> bool:
        async with self._operation(chat_id, "pause"):
            client = await db.get_assistant(chat_id)
            await db.playing(chat_id, paused=True)
            try:
                async with self._assistant_operation(client):
                    return await asyncio.wait_for(
                        client.pause(chat_id), self.operation_timeout
                    )
            except asyncio.TimeoutError:
                await db.playing(chat_id, paused=False)
                logger.error("Voice call pause timed out chat=%s", chat_id)
                raise

    async def resume(self, chat_id: int) -> bool:
        async with self._operation(chat_id, "resume"):
            client = await db.get_assistant(chat_id)
            await db.playing(chat_id, paused=False)
            try:
                async with self._assistant_operation(client):
                    return await asyncio.wait_for(
                        client.resume(chat_id), self.operation_timeout
                    )
            except asyncio.TimeoutError:
                await db.playing(chat_id, paused=True)
                logger.error("Voice call resume timed out chat=%s", chat_id)
                raise

    async def stop(self, chat_id: int, leave_call: bool = True) -> None:
        async with self._operation(chat_id, "stop"):
            await self._stop(chat_id, leave_call)

    async def _stop(self, chat_id: int, leave_call: bool = True) -> None:
        was_active = await db.get_call(chat_id)
        queue.clear(chat_id)
        await db.remove_call(chat_id)
        await db.set_loop(chat_id, 0)

        if not leave_call or not was_active:
            return

        client = await db.get_assistant(chat_id)
        try:
            async with self._assistant_operation(client):
                await asyncio.wait_for(
                    client.leave_call(chat_id, close=False),
                    timeout=self.operation_timeout,
                )
        except asyncio.TimeoutError:
            logger.error("Voice call leave timed out chat=%s", chat_id)
        except (ConnectionNotFound, exceptions.NoActiveGroupCall):
            logger.debug("Voice call %s was already closed.", chat_id)
        except Exception as exc:
            logger.warning("Could not leave voice call %s cleanly: %s", chat_id, exc)

    async def has_active_group_call(
        self, chat_id: int, assume_active_on_error: bool = True
    ) -> bool:
        try:
            peer = await asyncio.wait_for(app.resolve_peer(chat_id), timeout=5)
            channel = raw.types.InputChannel(
                channel_id=peer.channel_id,
                access_hash=peer.access_hash,
            )
            full_chat = await asyncio.wait_for(
                app.invoke(raw.functions.channels.GetFullChannel(channel=channel)),
                timeout=5,
            )
            return bool(getattr(full_chat.full_chat, "call", None))
        except Exception as exc:
            logger.debug("Could not preflight voice chat %s: %s", chat_id, exc)
            return assume_active_on_error

    async def play_media(
        self,
        chat_id: int,
        message: Message,
        media: Media | Track,
        seek_time: int = 0,
    ) -> None:
        async with self._operation(chat_id, "play"):
            await self._play_media(chat_id, message, media, seek_time)

    async def _play_media(
        self,
        chat_id: int,
        message: Message,
        media: Media | Track,
        seek_time: int = 0,
    ) -> None:
        client = await db.get_assistant(chat_id)
        _lang = await lang.get_lang(chat_id)

        if not media.file_path:
            await message.edit_text(_lang["error_no_file"].format(config.SUPPORT_CHAT))
            return await self._play_next(chat_id)

        if not await self.has_active_group_call(chat_id):
            await self._stop(chat_id, leave_call=False)
            await message.edit_text(_lang["error_no_call"])
            return

        stream = types.MediaStream(
            media_path=media.file_path,
            audio_parameters=types.AudioQuality.HIGH,
            video_parameters=types.VideoQuality.HD_720p,
            audio_flags=types.MediaStream.Flags.REQUIRED,
            video_flags=(
                types.MediaStream.Flags.AUTO_DETECT
                if media.video
                else types.MediaStream.Flags.IGNORE
            ),
            ffmpeg_parameters=f"-ss {seek_time}" if seek_time > 1 else None,
        )
        thumb_task = None
        if config.THUMB_GEN:
            if isinstance(media, Track):
                thumb_task = asyncio.create_task(thumb.generate(media))
            else:
                thumb_task = asyncio.create_task(
                    asyncio.sleep(0, result=config.DEFAULT_THUMB)
                )
            thumb_task.add_done_callback(self._consume_background_exception)
        try:
            await self._edit_playback_feedback(
                message,
                f"🎵 <b>{media.title}</b>\n\nConnecting the prepared stream to the voice chat...",
            )
            await self._start_playback_with_recovery(
                client=client,
                chat_id=chat_id,
                stream=stream,
                message=message,
            )
            if not seek_time:
                media.time = 1
                await db.add_call(chat_id)
                next_media = queue.get_next(chat_id, check=True)
                text = (
                    "▶️ <b>Now playing</b>\n\n"
                    f"🎵 <b>Title:</b> <a href={media.url}>{media.title}</a>\n"
                    f"⏱️ Duration: <code>{media.duration}</code>\n"
                    f"🙋 Requested by: {media.user}\n\n"
                    f"⏭️ Next: <b>{next_media.title if next_media else 'Nothing queued'}</b>\n"
                    f"📋 Queue: <code>{max(0, len(queue.get_queue(chat_id)) - 1)}</code> waiting"
                )
                keyboard = buttons.controls(chat_id)
                _thumb = None
                if thumb_task:
                    try:
                        _thumb = await asyncio.wait_for(
                            asyncio.shield(thumb_task), timeout=2
                        )
                    except asyncio.TimeoutError:
                        logger.info(
                            "Thumbnail generation still running after playback start chat=%s media=%s; using text now-playing for now.",
                            chat_id,
                            media.id,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Thumbnail generation failed chat=%s media=%s: %s",
                            chat_id,
                            media.id,
                            exc,
                        )
                try:
                    if _thumb:
                        input_media = (
                            InputMediaAnimation(media=_thumb, caption=text)
                            if str(_thumb).lower().endswith(".gif")
                            else InputMediaPhoto(media=_thumb, caption=text)
                        )
                        await message.edit_media(
                            media=input_media,
                            reply_markup=keyboard,
                        )
                    else:
                        await message.edit_text(text, reply_markup=keyboard)
                except (
                    ChatSendMediaForbidden,
                    ChatSendPhotosForbidden,
                    MessageIdInvalid,
                ):
                    if _thumb:
                        if str(_thumb).lower().endswith(".gif"):
                            sent = await app.send_animation(
                                chat_id=chat_id,
                                animation=_thumb,
                                caption=text,
                                reply_markup=keyboard,
                            )
                        else:
                            sent = await app.send_photo(
                                chat_id=chat_id,
                                photo=_thumb,
                                caption=text,
                                reply_markup=keyboard,
                            )
                    else:
                        sent = await app.send_message(
                            chat_id=chat_id,
                            text=text,
                            reply_markup=keyboard,
                        )
                    media.message_id = sent.id

                if thumb_task and not _thumb:
                    late_thumb_task = asyncio.create_task(
                        self._deliver_late_now_playing_media(
                            chat_id,
                            media,
                            text,
                            keyboard,
                            thumb_task,
                        ),
                        name=f"late-thumb-{chat_id}-{getattr(media, 'id', 'unknown')}",
                    )
                    late_thumb_task.add_done_callback(
                        self._consume_background_exception
                    )

        except FileNotFoundError:
            await message.edit_text(_lang["error_no_file"].format(config.SUPPORT_CHAT))
            await self._play_next(chat_id)
        except exceptions.NoActiveGroupCall:
            await self._stop(chat_id, leave_call=False)
            await message.edit_text(_lang["error_no_call"])
        except exceptions.NoAudioSourceFound:
            await message.edit_text(_lang["error_no_audio"])
            await self._play_next(chat_id)
        except (ConnectionError, ConnectionNotFound, TelegramServerError):
            await self._stop(chat_id, leave_call=False)
            await message.edit_text(_lang["error_tg_server"])
        except RTMPStreamingUnsupported:
            await self._stop(chat_id)
            await message.edit_text(_lang["error_rtmp"])
        except PlaybackRecoveryQueued:
            raise

    def _record_playback_timeout(self, chat_id: int, client, retry_stage: str) -> None:
        self._playback_timeout_count += 1
        self._last_playback_timeout = {
            "timestamp": time.time(),
            "chat_id": chat_id,
            "assistant_slot": self._assistant_slot(client),
            "retry_stage": retry_stage,
        }

    async def _cleanup_failed_play_start(self, chat_id: int, client) -> None:
        await db.remove_call(chat_id)
        try:
            async with self._assistant_operation(client):
                await asyncio.wait_for(
                    client.leave_call(chat_id, close=False),
                    timeout=15,
                )
        except (asyncio.TimeoutError, ConnectionNotFound, exceptions.NoActiveGroupCall):
            logger.info(
                "Stale playback cleanup completed with no active call chat=%s", chat_id
            )
        except Exception as exc:
            logger.warning("Stale playback cleanup failed chat=%s: %s", chat_id, exc)

    def _same_queue_item(
        self, current: Media | Track | None, media: Media | Track
    ) -> bool:
        if not current:
            return False
        current_queue_id = getattr(current, "queue_id", None)
        media_queue_id = getattr(media, "queue_id", None)
        if current_queue_id and media_queue_id:
            return current_queue_id == media_queue_id
        return getattr(current, "id", None) == getattr(media, "id", None)

    async def _deliver_late_now_playing_media(
        self,
        chat_id: int,
        media: Media | Track,
        text: str,
        keyboard,
        thumb_task: asyncio.Task,
    ) -> None:
        try:
            thumb_path = await asyncio.shield(thumb_task)
        except Exception as exc:
            logger.warning(
                "Deferred thumbnail generation failed chat=%s media=%s: %s",
                chat_id,
                getattr(media, "id", "unknown"),
                exc,
            )
            return

        if not thumb_path:
            return

        current = queue.get_current(chat_id)
        if not self._same_queue_item(current, media):
            return

        message_id = getattr(current, "message_id", 0) or getattr(
            media, "message_id", 0
        )
        if not message_id:
            return

        try:
            target = await app.get_messages(chat_id, message_id)
            input_media = (
                InputMediaAnimation(media=thumb_path, caption=text)
                if str(thumb_path).lower().endswith(".gif")
                else InputMediaPhoto(media=thumb_path, caption=text)
            )
            await target.edit_media(media=input_media, reply_markup=keyboard)
        except (
            ChatSendMediaForbidden,
            ChatSendPhotosForbidden,
            MessageIdInvalid,
        ):
            try:
                sent = (
                    await app.send_animation(
                        chat_id=chat_id,
                        animation=thumb_path,
                        caption=text,
                        reply_markup=keyboard,
                    )
                    if str(thumb_path).lower().endswith(".gif")
                    else await app.send_photo(
                        chat_id=chat_id,
                        photo=thumb_path,
                        caption=text,
                        reply_markup=keyboard,
                    )
                )
                media.message_id = sent.id
                current = queue.get_current(chat_id)
                if self._same_queue_item(current, media):
                    current.message_id = sent.id
            except Exception as exc:
                logger.warning(
                    "Could not send deferred now-playing media chat=%s media=%s: %s",
                    chat_id,
                    getattr(media, "id", "unknown"),
                    exc,
                )
        except Exception as exc:
            logger.warning(
                "Could not apply deferred now-playing media chat=%s media=%s: %s",
                chat_id,
                getattr(media, "id", "unknown"),
                exc,
            )

    async def _edit_playback_feedback(self, message: Message, text: str) -> None:
        try:
            await message.edit_text(text)
        except Exception as exc:
            logger.warning("Could not update playback recovery feedback: %s", exc)

    async def _request_safe_restart(self, chat_id: int, client) -> None:
        request = {
            "requested_at": time.time(),
            "reason": "playback start timed out twice",
            "chat_id": chat_id,
            "assistant_slot": self._assistant_slot(client),
            "retry_stage": "retry_failed",
        }
        self._restart_request = request
        try:
            from anony.core.health import health

            health.write()
        except OSError as exc:
            logger.warning(
                "Could not immediately publish playback recovery heartbeat: %s", exc
            )
        marker = Path.cwd() / ".restart-when-idle"
        temporary = marker.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(request, ensure_ascii=True), encoding="ascii"
            )
            temporary.replace(marker)
        except OSError as exc:
            logger.error("Could not create playback recovery restart marker: %s", exc)
        if app.owner:
            try:
                await app.send_message(
                    app.owner,
                    "🚨 <b>Playback recovery maintenance restart queued</b>\n\n"
                    f"Playback stalled twice in chat <code>{chat_id}</code> using assistant "
                    f"slot <code>{self._assistant_slot(client)}</code>.\n\n"
                    "📥 The affected queue was preserved. New requests will be saved in the "
                    "maintenance queue, and the deployment will restart for maintenance after other active streams finish.",
                )
            except Exception as exc:
                logger.warning(
                    "Could not notify owner about playback recovery: %s", exc
                )

    async def _start_playback_with_recovery(
        self,
        *,
        client,
        chat_id: int,
        stream,
        message: Message,
    ) -> None:
        for attempt in (1, 2):
            try:
                async with self._assistant_operation(client):
                    play_task = asyncio.create_task(
                        client.play(
                            chat_id=chat_id,
                            stream=stream,
                            config=types.GroupCallConfig(auto_start=False),
                        )
                    )
                    try:
                        await asyncio.wait_for(asyncio.shield(play_task), timeout=8)
                    except asyncio.TimeoutError:
                        await self._edit_playback_feedback(
                            message,
                            "⌛ <b>Still connecting to the voice chat...</b>\n\n"
                            "Telegram is taking longer than usual to bind this stream. I am still waiting.",
                        )
                        await asyncio.wait_for(
                            play_task,
                            timeout=max(1, self.play_start_timeout - 8),
                        )
                return
            except asyncio.TimeoutError:
                stage = "retrying" if attempt == 1 else "retry_failed"
                self._record_playback_timeout(chat_id, client, stage)
                if attempt == 1:
                    logger.warning(
                        "Playback start timed out chat=%s assistant=%s; retrying once",
                        chat_id,
                        self._assistant_slot(client),
                    )
                    await self._edit_playback_feedback(
                        message,
                        "⚠️ <b>Playback connection stalled.</b>\n\n"
                        "🔄 I am cleaning up this voice-chat connection and retrying the same track once.",
                    )
                    await self._cleanup_failed_play_start(chat_id, client)
                    await asyncio.sleep(2)
                    continue

                logger.exception(
                    "Playback start timed out again chat=%s assistant=%s; safe restart queued",
                    chat_id,
                    self._assistant_slot(client),
                )
                await self._request_safe_restart(chat_id, client)
                await self._edit_playback_feedback(
                    message,
                    "🚨 <b>Playback could not reconnect after retrying.</b>\n\n"
                    "📥 Your queue has been preserved.\n"
                    "💾 New requests will be saved for after the restart.\n"
                    "🛠️ A maintenance restart is queued and will begin after other active streams finish.",
                )
                raise PlaybackRecoveryQueued from None

    async def replay(self, chat_id: int) -> None:
        async with self._operation(chat_id, "replay"):
            try:
                await self._replay(chat_id)
            except PlaybackRecoveryQueued:
                return

    async def _replay(self, chat_id: int) -> None:
        if not await db.get_call(chat_id):
            return

        media = queue.get_current(chat_id)
        if not media:
            return await self._stop(chat_id)
        _lang = await lang.get_lang(chat_id)
        msg = await app.send_message(chat_id=chat_id, text=_lang["play_again"])
        media.message_id = msg.id
        await self._play_media(chat_id, msg, media)

    async def play_next(self, chat_id: int) -> None:
        async with self._operation(chat_id, "next"):
            try:
                await self._play_next(chat_id)
            except PlaybackRecoveryQueued:
                return

    async def resume_maintenance_queues(self) -> None:
        marker = Path.cwd() / ".restart-when-idle"
        if marker.exists():
            return

        for chat_id in queue.deferred_chats():
            now = time.monotonic()
            if now - self._maintenance_restore_attempts.get(chat_id, 0) < 300:
                continue
            self._maintenance_restore_attempts[chat_id] = now
            deferred = queue.get_deferred(chat_id)
            if not deferred:
                continue
            try:
                was_active = await db.get_call(chat_id)
                for item in deferred:
                    queue.add(chat_id, item)
                if was_active:
                    queue.pop_deferred(chat_id)
                    self._maintenance_restore_attempts.pop(chat_id, None)
                    await app.send_message(
                        chat_id,
                        "▶️ <b>Maintenance restart is no longer pending.</b>\n\n"
                        f"📥 Added <code>{len(deferred)}</code> saved maintenance request"
                        f"{'s' if len(deferred) != 1 else ''} to the active playback queue.",
                    )
                    await self._notify_maintenance_requesters(
                        deferred,
                        "▶️ Your saved music request was added back to the active queue "
                        "because maintenance is no longer pending.",
                    )
                    continue
                current = queue.get_current(chat_id)
                msg = await app.send_message(
                    chat_id,
                    "🛠️ <b>Maintenance restart completed.</b>\n\n"
                    f"▶️ Restoring <code>{len(deferred)}</code> saved playback request"
                    f"{'s' if len(deferred) != 1 else ''} now...",
                )
                if not current.file_path:
                    current.file_path = await asyncio.wait_for(
                        yt.download(current.id, video=current.video),
                        timeout=180,
                    )
                current.message_id = msg.id
                await self.play_media(chat_id, msg, current)
                if await db.get_call(chat_id):
                    queue.pop_deferred(chat_id)
                    self._maintenance_restore_attempts.pop(chat_id, None)
                    await self._notify_maintenance_requesters(
                        deferred,
                        "✅ The music bot completed its maintenance restart.\n\n"
                        "▶️ Your saved request is now in the restored playback queue.",
                    )
                    logger.info(
                        "Restored %s maintenance queue item(s) for chat=%s",
                        len(deferred),
                        chat_id,
                    )
                else:
                    queue.clear(chat_id)
            except Exception:
                queue.clear(chat_id)
                logger.exception(
                    "Could not restore maintenance queue for chat=%s", chat_id
                )
                try:
                    await app.send_message(
                        chat_id,
                        "⚠️ <b>Maintenance restart completed, but saved playback could not start yet.</b>\n\n"
                        "💾 Your maintenance queue is still saved. Start or reopen the voice chat, "
                        "then send another playback request.",
                    )
                except Exception:
                    pass

    async def _notify_maintenance_requesters(self, items: list, text: str) -> None:
        for user_id in {
            item.maintenance_owner_id
            for item in items
            if getattr(item, "maintenance_owner_id", 0)
        }:
            try:
                await app.send_message(user_id, text)
            except Exception:
                logger.debug("Could not notify maintenance requester user=%s", user_id)

    async def maintenance_queue_worker(self) -> None:
        while True:
            try:
                await self.resume_maintenance_queues()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Maintenance queue worker failed.")
            await asyncio.sleep(15)

    def maintenance_grace_remaining(self) -> int | None:
        marker = Path.cwd() / ".restart-when-idle"
        try:
            deadline = marker.stat().st_mtime + (config.MAINTENANCE_GRACE_MINUTES * 60)
        except OSError:
            return None
        return max(0, int(deadline - time.time()))

    async def _play_next(self, chat_id: int) -> None:
        grace_remaining = self.maintenance_grace_remaining()
        if grace_remaining == 0:
            saved = queue.defer_live_remaining(chat_id)
            await app.send_message(
                chat_id,
                "🛠️ <b>Maintenance grace period completed.</b>\n\n"
                "⏹️ The current track has finished, so this stream is stopping for maintenance.\n"
                f"💾 Saved <code>{saved}</code> remaining track"
                f"{'s' if saved != 1 else ''} to resume after the restart.",
            )
            return await self._stop(chat_id)

        if loop := await db.get_loop(chat_id):
            await db.set_loop(chat_id, loop - 1)
            return await self._replay(chat_id)

        current = queue.get_current(chat_id)
        media = queue.get_next(chat_id)
        try:
            if current and current.message_id:
                await app.delete_messages(
                    chat_id=chat_id,
                    message_ids=current.message_id,
                    revoke=True,
                )
                current.message_id = 0
        except Exception:
            pass

        if not media:
            return await self._stop(chat_id)

        _lang = await lang.get_lang(chat_id)
        msg = await app.send_message(chat_id=chat_id, text=_lang["play_next"])
        if not media.file_path:
            try:
                media.file_path = await asyncio.wait_for(
                    yt.download(media.id, video=media.video),
                    timeout=180,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "Queued track download timed out chat=%s media=%s",
                    chat_id,
                    media.id,
                )
                media.file_path = None
            if not media.file_path:
                await self._play_next(chat_id)
                return await msg.edit_text(
                    _lang["error_no_file"].format(config.SUPPORT_CHAT)
                )

        media.message_id = msg.id
        await self._play_media(chat_id, msg, media)

    async def ping(self) -> float:
        if not self.clients:
            return 0.0
        pings = [client.ping for client in self.clients]
        return round(sum(pings) / len(pings), 2)

    async def decorators(self, client: PyTgCalls) -> None:
        @client.on_update()
        async def update_handler(_, update: types.Update) -> None:
            if isinstance(update, types.StreamEnded):
                if update.stream_type == types.StreamEnded.Type.AUDIO:
                    now = time.monotonic()
                    if now - self._last_stream_end.get(update.chat_id, 0) < 3:
                        logger.debug(
                            "Ignoring duplicate stream-ended event chat=%s",
                            update.chat_id,
                        )
                        return
                    self._last_stream_end[update.chat_id] = now
                    await self.play_next(update.chat_id)
            elif isinstance(update, types.ChatUpdate):
                if update.status in [
                    types.ChatUpdate.Status.KICKED,
                    types.ChatUpdate.Status.LEFT_GROUP,
                    types.ChatUpdate.Status.CLOSED_VOICE_CHAT,
                ]:
                    await self.stop(update.chat_id, leave_call=False)

    async def boot(self) -> None:
        PyTgCallsSession.notice_displayed = True
        for ub in userbot.clients:
            await self.add_client(ub)
        logger.info("PyTgCalls client(s) started.")

    async def add_client(self, ub) -> None:
        PyTgCallsSession.notice_displayed = True
        client = PyTgCalls(ub, cache_duration=100)
        client.session_slot = ub.session_slot
        await client.start()
        self.clients.append(client)
        await self.decorators(client)
