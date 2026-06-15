# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import asyncio
import time
from pathlib import Path

from pyrogram import errors, filters, types

from anony import app, db, lang, logger


FLOOD_RETRIES = 3
PROGRESS_INTERVAL = 5
SEND_DELAY = 0.15
broadcast_state = None
runtime_broadcast_task: asyncio.Task | None = None


def broadcast_active() -> bool:
    return bool(
        broadcast_state
        or (runtime_broadcast_task and not runtime_broadcast_task.done())
    )


def progress_text(state: dict, *, paused_for: int | None = None) -> str:
    processed = state["processed"]
    total = state["total"]
    percent = int(processed * 100 / total) if total else 100
    elapsed = max(time.monotonic() - state["started"], 0.1)
    rate = processed / elapsed
    remaining = total - processed
    eta = int(remaining / rate) if rate else 0
    text = (
        "<b>📣 Broadcast in progress</b>\n\n"
        f"📬 Progress: <code>{processed}/{total}</code> ({percent}%)\n"
        f"✅ Delivered: <code>{state['delivered']}</code>\n"
        f"❌ Failed: <code>{state['failed']}</code>\n"
        f"⏱️ Elapsed: <code>{int(elapsed)}s</code>\n"
        f"🕒 Estimated remaining: <code>{eta}s</code>"
    )
    if paused_for is not None:
        text += (
            "\n\n🚦 Telegram asked the bot to slow down.\n"
            f"⏳ Retrying the current recipient in <code>{paused_for}s</code>."
        )
    return text + "\n\n🛑 Use <code>/stop_broadcast</code> to cancel."


async def update_progress(state: dict, *, force: bool = False, paused_for: int | None = None) -> None:
    now = time.monotonic()
    if not force and paused_for is None and now - state["last_update"] < PROGRESS_INTERVAL:
        return
    try:
        await state["status"].edit_text(progress_text(state, paused_for=paused_for))
        state["last_update"] = now
    except errors.MessageNotModified:
        pass
    except Exception:
        logger.debug("Could not update broadcast progress", exc_info=True)


async def interruptible_wait(stop_event: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return False
    except asyncio.TimeoutError:
        return True


async def send_runtime_text(chat_id: int, text: str) -> tuple[bool, str | None]:
    for attempt in range(FLOOD_RETRIES + 1):
        try:
            await app.send_message(chat_id, text)
            return True, None
        except (errors.FloodWait, errors.FloodPremiumWait) as flood:
            if attempt >= FLOOD_RETRIES:
                return False, f"Flood wait remained after {FLOOD_RETRIES} retries."
            await asyncio.sleep(max(int(flood.value), 1) + 1)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
    return False, "Delivery retries exhausted."


async def run_runtime_broadcast(
    *,
    text: str,
    recipients: list[int],
    group_ids: set[int],
    requested_by: int,
) -> None:
    global runtime_broadcast_task
    delivered = failed = groups = users = 0
    errors_found = []
    started = time.monotonic()
    try:
        for chat_id in recipients:
            success, error = await send_runtime_text(chat_id, text)
            if success:
                delivered += 1
                if chat_id in group_ids:
                    groups += 1
                else:
                    users += 1
            else:
                failed += 1
                errors_found.append(f"{chat_id} - {error}")
            await asyncio.sleep(SEND_DELAY)
        summary = (
            f"✅ <b>{app.name} completed the manager broadcast.</b>\n\n"
            f"📬 Recipients: <code>{len(recipients)}</code>\n"
            f"✅ Delivered: <code>{delivered}</code>\n"
            f"👥 Groups reached: <code>{groups}</code>\n"
            f"👤 Users reached: <code>{users}</code>\n"
            f"❌ Failed: <code>{failed}</code>\n"
            f"⏱️ Duration: <code>{int(time.monotonic() - started)}s</code>"
        )
        if requested_by:
            try:
                await app.send_message(requested_by, summary)
            except Exception:
                logger.warning("Could not deliver runtime broadcast summary to %s.", requested_by)
        logger.info(
            "Manager broadcast completed recipients=%s delivered=%s failed=%s.",
            len(recipients),
            delivered,
            failed,
        )
        if errors_found:
            logger.warning("Manager broadcast delivery failures: %s", "; ".join(errors_found[:20]))
    finally:
        runtime_broadcast_task = None


async def start_runtime_broadcast(
    *,
    text: str,
    include_users: bool,
    exclude_groups: bool,
    requested_by: int,
) -> dict:
    global runtime_broadcast_task
    if not text or len(text) > 4096:
        raise ValueError("broadcast text must contain between 1 and 4096 characters")
    if broadcast_state or (runtime_broadcast_task and not runtime_broadcast_task.done()):
        raise RuntimeError("another broadcast is already active")

    groups = list(await db.get_chats()) if not exclude_groups else []
    users = list(await db.get_users()) if include_users else []
    recipients = list(dict.fromkeys([*groups, *users]))
    runtime_broadcast_task = asyncio.create_task(
        run_runtime_broadcast(
            text=text,
            recipients=recipients,
            group_ids=set(groups),
            requested_by=requested_by,
        ),
        name="manager-broadcast",
    )
    return {
        "recipient_count": len(recipients),
        "group_count": len(groups),
        "user_count": len(users),
    }


async def send_to_recipient(msg: types.Message, chat_id: int, copy: bool, state: dict) -> tuple[bool, str | None]:
    for attempt in range(FLOOD_RETRIES + 1):
        if state["stop_event"].is_set():
            return False, "Broadcast cancelled before delivery."
        try:
            if copy:
                await msg.copy(chat_id, reply_markup=msg.reply_markup)
            else:
                await msg.forward(chat_id)
            return True, None
        except (errors.FloodWait, errors.FloodPremiumWait) as flood:
            if attempt >= FLOOD_RETRIES:
                return False, f"Flood wait remained after {FLOOD_RETRIES} retries."
            wait = max(int(flood.value), 1) + 1
            await update_progress(state, force=True, paused_for=wait)
            if not await interruptible_wait(state["stop_event"], wait):
                return False, "Broadcast cancelled during flood wait."
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
    return False, "Delivery retries exhausted."


async def log_broadcast_start(message: types.Message, msg: types.Message) -> None:
    try:
        await msg.forward(app.logger)
        log_message = await app.send_message(
            chat_id=app.logger,
            text=message.lang["gcast_log"].format(
                message.from_user.id,
                message.from_user.mention,
                message.text,
            ),
        )
        await log_message.pin(disable_notification=False)
    except Exception:
        logger.exception("Could not write broadcast start log")


@app.on_message(filters.command(["broadcast"]) & app.sudoers)
@lang.language()
async def _broadcast(_, message: types.Message):
    global broadcast_state
    if not message.reply_to_message:
        return await message.reply_text(
            "📣 Reply to the message you want to broadcast.\n\n"
            "💡 Add <code>-user</code> to include users, <code>-nochat</code> to exclude groups, "
            "or <code>-copy</code> to remove the forwarded label."
        )
    if broadcast_state or (runtime_broadcast_task and not runtime_broadcast_task.done()):
        return await message.reply_text(
            (
                progress_text(broadcast_state)
                if broadcast_state
                else "📣 A manager broadcast is currently running."
            )
            + "\n\n⚠️ Only one broadcast can run at a time."
        )

    status = await message.reply_text("🔎 Collecting broadcast recipients...")
    groups = list(await db.get_chats()) if "-nochat" not in message.command else []
    users = list(await db.get_users()) if "-user" in message.command else []
    group_ids = set(groups)
    recipients = list(dict.fromkeys(groups + users))
    if not recipients:
        return await status.edit_text(
            "📭 No recipients matched this broadcast.\n\n"
            "💡 Include groups or add <code>-user</code> to include served users."
        )

    state = {
        "status": status,
        "stop_event": asyncio.Event(),
        "started": time.monotonic(),
        "last_update": 0.0,
        "total": len(recipients),
        "processed": 0,
        "delivered": 0,
        "failed": 0,
        "groups": 0,
        "users": 0,
        "errors": [],
    }
    broadcast_state = state
    await update_progress(state, force=True)
    await log_broadcast_start(message, message.reply_to_message)

    try:
        for chat_id in recipients:
            if state["stop_event"].is_set():
                break
            delivered, error = await send_to_recipient(
                message.reply_to_message,
                chat_id,
                "-copy" in message.command,
                state,
            )
            if state["stop_event"].is_set() and not delivered:
                break
            state["processed"] += 1
            if delivered:
                state["delivered"] += 1
                if chat_id in group_ids:
                    state["groups"] += 1
                else:
                    state["users"] += 1
            else:
                state["failed"] += 1
                state["errors"].append(f"{chat_id} - {error}")
            await update_progress(state)
            if not await interruptible_wait(state["stop_event"], SEND_DELAY):
                break
    finally:
        cancelled = state["stop_event"].is_set()
        elapsed = int(time.monotonic() - state["started"])
        summary = (
            f"{'🛑 Broadcast cancelled.' if cancelled else '✅ Broadcast completed.'}\n\n"
            f"📬 Processed: <code>{state['processed']}/{state['total']}</code>\n"
            f"👥 Groups reached: <code>{state['groups']}</code>\n"
            f"👤 Users reached: <code>{state['users']}</code>\n"
            f"❌ Failed: <code>{state['failed']}</code>\n"
            f"⏱️ Duration: <code>{elapsed}s</code>"
        )
        try:
            if state["errors"]:
                error_file = Path.cwd() / "cache" / "broadcast-errors.txt"
                error_file.parent.mkdir(parents=True, exist_ok=True)
                error_file.write_text("\n".join(state["errors"]), encoding="utf-8")
                try:
                    await message.reply_document(str(error_file), caption=summary)
                except Exception:
                    logger.exception("Could not send broadcast error report")
                finally:
                    error_file.unlink(missing_ok=True)
            try:
                await status.edit_text(summary)
            except Exception:
                logger.exception("Could not send broadcast final summary")
        finally:
            broadcast_state = None


@app.on_message(filters.command(["stop_gcast", "stop_broadcast"]) & app.sudoers)
@lang.language()
async def _stop_gcast(_, message: types.Message):
    if not broadcast_state:
        return await message.reply_text("📭 There is no active broadcast to stop.")
    broadcast_state["stop_event"].set()
    await message.reply_text(
        "🛑 Cancellation requested.\n\n"
        "⏳ The broadcast worker is stopping now and will post its final delivery summary."
    )
