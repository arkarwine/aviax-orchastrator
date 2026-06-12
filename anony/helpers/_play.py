# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import asyncio
from pathlib import Path

from pyrogram import enums, errors, types

from anony import app, config, db, logger, queue, yt
from anony.helpers import utils


def checkUB(play):
    async def wrapper(_, m: types.Message):
        if not m.from_user:
            return await m.reply_text(m.lang["play_user_invalid"])

        chat_id = m.chat.id
        if m.chat.type != enums.ChatType.SUPERGROUP:
            await m.reply_text(m.lang["play_chat_invalid"])
            return await app.leave_chat(chat_id)

        if Path(".restart-when-idle").exists():
            return await m.reply_text(
                "⏳ A restart is waiting for the current streams to finish.\n\n"
                "💡 New playback requests are paused until the restart completes."
            )

        if not m.reply_to_message and (
            len(m.command) < 2 or (len(m.command) == 2 and m.command[1] == "-f")
        ):
            return await m.reply_text(m.lang["play_usage"])

        if len(queue.get_queue(chat_id)) >= config.QUEUE_LIMIT:
            return await m.reply_text(m.lang["play_queue_full"].format(config.QUEUE_LIMIT))

        force = m.command[0].endswith("force") or (
            len(m.command) > 1 and "-f" in m.command[1]
        )
        video = m.command[0][0] == "v" and config.VIDEO_PLAY
        url = utils.get_url(m)
        if url and yt.invalid(url):
            return await m.reply_text(m.lang["play_not_found"].format(config.SUPPORT_CHAT))
        m3u8 = url and not yt.valid(url)

        play_mode = await db.get_play_mode(chat_id)
        if play_mode or force:
            adminlist = await db.get_admins(chat_id)
            if (
                m.from_user.id not in adminlist
                and not await db.is_auth(chat_id, m.from_user.id)
                and not m.from_user.id in app.sudoers
            ):
                return await m.reply_text(m.lang["play_admin"])

        if chat_id not in db.active_calls:
            try:
                client = await db.get_client(chat_id)
            except Exception:
                logger.exception("Could not select an assistant for chat %s", chat_id)
                return await m.reply_text(
                    "❌ No connected assistant session is available for playback.\n\n"
                    "💡 Check <code>/sessions</code>, then restart the deployed bot if a configured session is offline."
                )
            try:
                member = await app.get_chat_member(chat_id, client.id)
                if member.status in [
                    enums.ChatMemberStatus.BANNED,
                    enums.ChatMemberStatus.RESTRICTED,
                ]:
                    try:
                        await app.unban_chat_member(
                            chat_id=chat_id, user_id=client.id
                        )
                    except Exception:
                        return await m.reply_text(
                            m.lang["play_banned"].format(
                                app.name,
                                client.id,
                                client.mention,
                                f"@{client.username}" if client.username else None,
                            )
                        )
            except errors.ChatAdminRequired:
                return await m.reply_text(
                    "🛡️ I need admin access to prepare the assistant.\n\n"
                    "💡 Promote the bot as admin with invite-user permissions, then try /play again."
                )
            except (errors.UserNotParticipant, errors.exceptions.bad_request_400.UserNotParticipant):
                if m.chat.username:
                    invite_link = m.chat.username
                    try:
                        await client.resolve_peer(invite_link)
                    except Exception:
                        pass
                else:
                    try:
                        invite_link = (await app.get_chat(chat_id)).invite_link
                        if not invite_link:
                            invite_link = await app.export_chat_invite_link(chat_id)
                    except errors.ChatAdminRequired:
                        return await m.reply_text(
                            "🛡️ I need admin access to create an invite link for the assistant.\n\n"
                            "💡 Promote the bot as admin with invite-user permissions, then try again."
                        )
                    except Exception:
                        return await m.reply_text(
                            "❌ I could not prepare an invite link for the assistant.\n\n"
                            "💡 Create a public username or invite link for this group, then try /play again."
                        )

                umm = await m.reply_text("🤝 Inviting the assistant to this group...")
                await asyncio.sleep(2)
                try:
                    await client.join_chat(invite_link)
                except errors.UserAlreadyParticipant:
                    pass
                except errors.InviteRequestSent:
                    await asyncio.sleep(2)
                    try:
                        await app.approve_chat_join_request(chat_id, client.id)
                    except errors.HideRequesterMissing:
                        pass
                    except Exception:
                        return await umm.edit_text(
                            "❌ The assistant's join request could not be approved.\n\n"
                            "💡 Check the bot's admin permissions, then try /play again."
                        )
                except Exception:
                    logger.exception("Assistant failed to join chat %s", chat_id)
                    return await umm.edit_text(
                        "❌ The assistant could not join this group.\n\n"
                        "💡 Check the invite link, remove any assistant ban, and try /play again."
                    )

                await umm.delete()
                await client.resolve_peer(chat_id)

        if await db.get_cmd_delete(chat_id):
            try:
                await m.delete()
            except Exception:
                pass

        return await play(_, m, force, m3u8, video, url)

    return wrapper
