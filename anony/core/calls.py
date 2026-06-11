# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import asyncio
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from ntgcalls import (ConnectionNotFound, TelegramServerError,
                      RTMPStreamingUnsupported, ConnectionError)
from pyrogram import raw
from pyrogram.errors import (ChatSendMediaForbidden, ChatSendPhotosForbidden,
                             MessageIdInvalid)
from pyrogram.types import InputMediaPhoto, Message
from pytgcalls import PyTgCalls, exceptions, types
from pytgcalls.pytgcalls_session import PyTgCallsSession

from anony import (app, config, db, lang, logger,
                   queue, thumb, userbot, yt)
from anony.helpers import Media, Track, buttons


class TgCall(PyTgCalls):
    def __init__(self):
        self.clients = []
        self._locks = defaultdict(asyncio.Lock)
        self._operations = {}
        self._last_stream_end = {}
        self.operation_timeout = 75

    def active_operations(self) -> dict:
        now = time.monotonic()
        return {
            str(chat_id): {
                "stage": operation["stage"],
                "seconds": round(now - operation["started"], 1),
            }
            for chat_id, operation in self._operations.items()
        }

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
                return await asyncio.wait_for(client.pause(chat_id), self.operation_timeout)
            except asyncio.TimeoutError:
                await db.playing(chat_id, paused=False)
                logger.error("Voice call pause timed out chat=%s", chat_id)
                raise

    async def resume(self, chat_id: int) -> bool:
        async with self._operation(chat_id, "resume"):
            client = await db.get_assistant(chat_id)
            await db.playing(chat_id, paused=False)
            try:
                return await asyncio.wait_for(client.resume(chat_id), self.operation_timeout)
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

    async def has_active_group_call(self, chat_id: int) -> bool:
        try:
            peer = await asyncio.wait_for(app.resolve_peer(chat_id), timeout=30)
            channel = raw.types.InputChannel(
                channel_id=peer.channel_id,
                access_hash=peer.access_hash,
            )
            full_chat = await asyncio.wait_for(
                app.invoke(raw.functions.channels.GetFullChannel(channel=channel)),
                timeout=30,
            )
            return bool(getattr(full_chat.full_chat, "call", None))
        except Exception as exc:
            logger.debug("Could not preflight voice chat %s: %s", chat_id, exc)
            return True


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
        _thumb = (
            await thumb.generate(media)
            if isinstance(media, Track)
            else config.DEFAULT_THUMB
        ) if config.THUMB_GEN else None

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
        try:
            await asyncio.wait_for(
                client.play(
                    chat_id=chat_id,
                    stream=stream,
                    config=types.GroupCallConfig(auto_start=False),
                ),
                timeout=self.operation_timeout,
            )
            if not seek_time:
                media.time = 1
                await db.add_call(chat_id)
                text = _lang["play_media"].format(
                    media.url,
                    media.title,
                    media.duration,
                    media.user,
                )
                keyboard = buttons.controls(chat_id)
                try:
                    if _thumb:
                        await message.edit_media(
                            media=InputMediaPhoto(
                                media=_thumb,
                                caption=text,
                            ),
                            reply_markup=keyboard,
                        )
                    else:
                        await message.edit_text(text, reply_markup=keyboard)
                except (ChatSendMediaForbidden, ChatSendPhotosForbidden, MessageIdInvalid):
                    if _thumb:
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
        except asyncio.TimeoutError:
            logger.error("Playback start timed out chat=%s", chat_id)
            await self._stop(chat_id, leave_call=False)
            raise


    async def replay(self, chat_id: int) -> None:
        async with self._operation(chat_id, "replay"):
            await self._replay(chat_id)

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
            await self._play_next(chat_id)

    async def _play_next(self, chat_id: int) -> None:
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
                logger.error("Queued track download timed out chat=%s media=%s", chat_id, media.id)
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
                        logger.debug("Ignoring duplicate stream-ended event chat=%s", update.chat_id)
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
        await client.start()
        self.clients.append(client)
        await self.decorators(client)
