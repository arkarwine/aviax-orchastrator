# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import asyncio
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pyrogram import errors, filters, types

from anony import app, db, lang, logger, tasks

FLOOD_RETRIES = 3
PROGRESS_INTERVAL = 5
SEND_DELAY = 0.15
SCHEDULE_POLL_INTERVAL = 15
broadcast_state = None
runtime_broadcast_task: asyncio.Task | None = None
runtime_broadcast_stop_event: asyncio.Event | None = None


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


async def update_progress(
    state: dict,
    *,
    force: bool = False,
    paused_for: int | None = None,
) -> None:
    now = time.monotonic()
    if (
        not force
        and paused_for is None
        and now - state["last_update"] < PROGRESS_INTERVAL
    ):
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


async def collect_recipients(
    *,
    include_users: bool,
    exclude_groups: bool,
) -> tuple[list[int], list[int], set[int]]:
    groups = list(await db.get_chats()) if not exclude_groups else []
    users = list(await db.get_users()) if include_users else []
    recipients = list(dict.fromkeys([*groups, *users]))
    return recipients, groups, set(groups)


async def send_runtime_text(chat_id: int, text: str) -> tuple[bool, str | None]:
    for attempt in range(FLOOD_RETRIES + 1):
        try:
            await app.send_message(chat_id, text)
            return True, None
        except (errors.FloodWait, errors.FloodPremiumWait) as flood:
            if attempt >= FLOOD_RETRIES:
                return False, f"Flood wait remained after {FLOOD_RETRIES} retries."
            wait_seconds = max(int(getattr(flood, "value", 1) or 1), 1) + 1
            await asyncio.sleep(wait_seconds)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
    return False, "Delivery retries exhausted."


async def run_runtime_broadcast(
    *,
    text: str,
    recipients: list[int],
    group_ids: set[int],
    requested_by: int,
    stop_event: asyncio.Event,
    label: str = "broadcast",
) -> None:
    global runtime_broadcast_task, runtime_broadcast_stop_event
    delivered = failed = groups = users = 0
    errors_found = []
    started = time.monotonic()
    try:
        for chat_id in recipients:
            if stop_event.is_set():
                break
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
            if not await interruptible_wait(stop_event, SEND_DELAY):
                break
        summary = (
            f"{'🛑' if stop_event.is_set() else '✅'} <b>{app.name} {'cancelled' if stop_event.is_set() else 'completed'} the {label}.</b>\n\n"
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
                logger.warning(
                    "Could not deliver runtime broadcast summary to %s.", requested_by
                )
        logger.info(
            "%s completed recipients=%s delivered=%s failed=%s.",
            label.title(),
            len(recipients),
            delivered,
            failed,
        )
        if errors_found:
            logger.warning(
                "%s delivery failures: %s",
                label.title(),
                "; ".join(errors_found[:20]),
            )
    finally:
        runtime_broadcast_task = None
        runtime_broadcast_stop_event = None


async def start_runtime_broadcast(
    *,
    text: str,
    include_users: bool,
    exclude_groups: bool,
    requested_by: int,
    label: str = "broadcast",
) -> dict:
    global runtime_broadcast_task, runtime_broadcast_stop_event
    if not text or len(text) > 4096:
        raise ValueError("broadcast text must contain between 1 and 4096 characters")
    if broadcast_state or (
        runtime_broadcast_task and not runtime_broadcast_task.done()
    ):
        raise RuntimeError("another broadcast is already active")

    recipients, groups, group_ids = await collect_recipients(
        include_users=include_users,
        exclude_groups=exclude_groups,
    )
    runtime_broadcast_stop_event = asyncio.Event()
    runtime_broadcast_task = asyncio.create_task(
        run_runtime_broadcast(
            text=text,
            recipients=recipients,
            group_ids=group_ids,
            requested_by=requested_by,
            stop_event=runtime_broadcast_stop_event,
            label=label,
        ),
        name=f"{label.replace(' ', '-')}-runtime",
    )
    return {
        "recipient_count": len(recipients),
        "group_count": len(groups),
        "user_count": max(0, len(recipients) - len(groups)),
    }


async def send_to_recipient(
    msg: types.Message,
    chat_id: int,
    copy: bool,
    state: dict,
) -> tuple[bool, str | None]:
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
            wait = max(int(getattr(flood, "value", 1) or 1), 1) + 1
            await update_progress(state, force=True, paused_for=wait)
            if not await interruptible_wait(state["stop_event"], wait):
                return False, "Broadcast cancelled during flood wait."
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
    return False, "Delivery retries exhausted."


async def log_broadcast_start(message: types.Message, msg: types.Message) -> None:
    if not message.from_user:
        return
    try:
        await msg.forward(app.logger)
        lang_map = getattr(message, "lang", {}) or {}
        template = lang_map.get(
            "gcast_log",
            "Broadcast by {} ({})\n\n{}",
        )
        log_message = await app.send_message(
            chat_id=app.logger,
            text=template.format(
                message.from_user.id,
                message.from_user.mention,
                message.text or "",
            ),
        )
        await log_message.pin(disable_notification=False)
    except Exception:
        logger.exception("Could not write broadcast start log")


def format_run_at(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def parse_scheduled_broadcast(
    command_text: str,
) -> tuple[datetime | None, str, str | None]:
    command_text = command_text.strip()
    relative = re.match(r"^in\s+(\d+)\s*([mhd])\b\s*(.*)$", command_text, re.I | re.S)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        seconds = amount * {"m": 60, "h": 3600, "d": 86400}[unit]
        if seconds < 60:
            return None, "", "Use at least 1 minute for a scheduled broadcast."
        return (
            datetime.fromtimestamp(time.time() + seconds, timezone.utc),
            relative.group(3),
            None,
        )

    absolute = re.match(
        r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})(?:\s+|$)(.*)$",
        command_text,
        re.S,
    )
    if absolute:
        try:
            local_time = datetime.strptime(
                absolute.group(1).replace("T", " "), "%Y-%m-%d %H:%M"
            ).astimezone()
        except ValueError:
            return None, "", "Use time as <code>YYYY-MM-DD HH:MM</code>."
        return local_time.astimezone(timezone.utc), absolute.group(2), None

    return (
        None,
        "",
        (
            "Use <code>in 30m</code>, <code>in 2h</code>, or "
            "<code>YYYY-MM-DD HH:MM</code> before the message."
        ),
    )


async def dispatch_scheduled_broadcast(item: dict) -> bool:
    requested_by = int(item.get("requested_by") or 0)
    scheduled_id = str(item.get("id") or "unknown")
    try:
        summary = await start_runtime_broadcast(
            text=str(item.get("text") or ""),
            include_users=bool(item.get("include_users")),
            exclude_groups=bool(item.get("exclude_groups")),
            requested_by=requested_by,
            label="scheduled broadcast",
        )
    except RuntimeError:
        return False
    except Exception as exc:
        logger.exception("Scheduled broadcast %s could not start.", scheduled_id)
        if requested_by:
            try:
                await app.send_message(
                    requested_by,
                    "❌ <b>Scheduled broadcast failed.</b>\n\n"
                    f"🆔 ID: <code>{scheduled_id}</code>\n"
                    f"Reason: <code>{type(exc).__name__}: {exc}</code>",
                )
            except Exception:
                logger.warning(
                    "Could not notify %s about scheduled broadcast failure.",
                    requested_by,
                )
        return True

    if requested_by:
        try:
            await app.send_message(
                requested_by,
                "📣 <b>Scheduled broadcast started.</b>\n\n"
                f"🆔 ID: <code>{scheduled_id}</code>\n"
                f"🕒 Scheduled for: <code>{format_run_at(str(item.get('run_at') or ''))}</code>\n"
                f"📬 Recipients: <code>{summary['recipient_count']}</code>",
            )
        except Exception:
            logger.warning(
                "Could not notify %s about scheduled broadcast start.",
                requested_by,
            )
    return True


async def scheduled_broadcast_worker() -> None:
    while True:
        try:
            broadcasts = list(await db.get_scheduled_broadcasts())
            if not broadcasts:
                await asyncio.sleep(SCHEDULE_POLL_INTERVAL)
                continue

            valid = []
            invalid_found = False
            due_item = None
            now = datetime.now(timezone.utc)

            for item in sorted(
                broadcasts, key=lambda entry: str(entry.get("run_at", ""))
            ):
                run_at_raw = str(item.get("run_at") or "")
                try:
                    run_at = datetime.fromisoformat(run_at_raw)
                except (TypeError, ValueError):
                    invalid_found = True
                    logger.warning(
                        "Dropping invalid scheduled broadcast entry: %s", item
                    )
                    continue
                valid.append(item)
                if due_item is None and run_at <= now:
                    due_item = item

            if invalid_found and valid != broadcasts:
                await db.save_scheduled_broadcasts(valid)

            if not due_item or broadcast_active():
                await asyncio.sleep(SCHEDULE_POLL_INTERVAL)
                continue

            started_or_consumed = await dispatch_scheduled_broadcast(due_item)
            if started_or_consumed:
                await db.save_scheduled_broadcasts(
                    [item for item in valid if item.get("id") != due_item.get("id")]
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduled broadcast worker failed.")
        await asyncio.sleep(SCHEDULE_POLL_INTERVAL)


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
    if broadcast_state or (
        runtime_broadcast_task and not runtime_broadcast_task.done()
    ):
        return await message.reply_text(
            (
                progress_text(broadcast_state)
                if broadcast_state
                else "📣 Another text broadcast is currently running."
            )
            + "\n\n⚠️ Only one broadcast can run at a time."
        )

    command_parts = list(message.command or [])
    status = await message.reply_text("🔎 Collecting broadcast recipients...")
    recipients, _, group_ids = await collect_recipients(
        include_users="-user" in command_parts,
        exclude_groups="-nochat" in command_parts,
    )
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
                "-copy" in command_parts,
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


@app.on_message(
    filters.command(["schedulebroadcast", "schedulecast"])
    & filters.private
    & app.sudoers
)
@lang.language()
async def _schedule_broadcast(_, message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    run_at, remainder, error = parse_scheduled_broadcast(
        parts[1] if len(parts) > 1 else ""
    )
    if error:
        return await message.reply_text(
            "🕒 <b>Scheduled broadcast usage</b>\n\n"
            "<code>/schedulebroadcast in 30m [-user] [-nochat] message</code>\n"
            "<code>/schedulebroadcast 2026-06-19 21:30 [-user] [-nochat] message</code>\n\n"
            f"💡 {error}"
        )

    include_users = "-user" in remainder.split()
    exclude_groups = "-nochat" in remainder.split()
    text = re.sub(r"(?<!\S)-(?:user|nochat)(?!\S)", "", remainder).strip()
    if message.reply_to_message:
        text = message.reply_to_message.text or message.reply_to_message.caption or text
    if not text:
        return await message.reply_text(
            "📣 <b>Broadcast text is required.</b>\n\n"
            "Reply to a text message or include text after the scheduled time."
        )
    if len(text) > 4096:
        return await message.reply_text(
            "❌ <b>The broadcast is too long.</b>\n\n"
            "💡 Keep the message within Telegram's 4096-character text limit."
        )
    if not run_at or run_at <= datetime.now(timezone.utc):
        return await message.reply_text("❌ <b>The scheduled time is already past.</b>")

    item = {
        "id": uuid.uuid4().hex[:8],
        "run_at": run_at.isoformat(),
        "text": text,
        "include_users": include_users,
        "exclude_groups": exclude_groups,
        "requested_by": message.from_user.id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    broadcasts = list(await db.get_scheduled_broadcasts())
    broadcasts.append(item)
    broadcasts.sort(key=lambda entry: str(entry.get("run_at", "")))
    await db.save_scheduled_broadcasts(broadcasts)
    await message.reply_text(
        f"✅ <b>Broadcast scheduled.</b>\n\n"
        f"🆔 ID: <code>{item['id']}</code>\n"
        f"🕒 Runs at: <code>{format_run_at(item['run_at'])}</code>\n"
        f"👤 Include users: <code>{'yes' if include_users else 'no'}</code>\n"
        f"👥 Include groups: <code>{'no' if exclude_groups else 'yes'}</code>"
    )


@app.on_message(
    filters.command(["scheduledbroadcasts", "broadcasts"])
    & filters.private
    & app.sudoers
)
@lang.language()
async def _scheduled_broadcasts(_, message: types.Message):
    broadcasts = list(await db.get_scheduled_broadcasts())
    if not broadcasts:
        return await message.reply_text("📭 No scheduled broadcasts.")
    lines = ["<b>📆 Scheduled Broadcasts</b>"]
    for item in sorted(broadcasts, key=lambda entry: str(entry.get("run_at", "")))[:20]:
        preview = str(item.get("text") or "")
        preview = preview[:60] + ("…" if len(preview) > 60 else "")
        lines.append(
            f"\n🆔 <code>{item.get('id')}</code>\n"
            f"🕒 <code>{format_run_at(str(item.get('run_at') or ''))}</code>\n"
            f"👤 Include users: <code>{'yes' if item.get('include_users') else 'no'}</code>\n"
            f"👥 Include groups: <code>{'no' if item.get('exclude_groups') else 'yes'}</code>\n"
            f"📣 {preview}"
        )
    if len(broadcasts) > 20:
        lines.append(f"\n…and <code>{len(broadcasts) - 20}</code> more.")
    await message.reply_text("\n".join(lines))


@app.on_message(
    filters.command(["cancelbroadcast", "cancelcast"]) & filters.private & app.sudoers
)
@lang.language()
async def _cancel_scheduled_broadcast(_, message: types.Message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text(
            "✖️ Usage: <code>/cancelbroadcast &lt;scheduled_id&gt;</code>"
        )
    target = args[1].strip()
    broadcasts = list(await db.get_scheduled_broadcasts())
    kept = [item for item in broadcasts if str(item.get("id")) != target]
    if len(kept) == len(broadcasts):
        return await message.reply_text(
            f"📭 Scheduled broadcast <code>{target}</code> was not found."
        )
    await db.save_scheduled_broadcasts(kept)
    await message.reply_text(f"✅ Cancelled scheduled broadcast <code>{target}</code>.")


@app.on_message(filters.command(["stop_gcast", "stop_broadcast"]) & app.sudoers)
@lang.language()
async def _stop_gcast(_, message: types.Message):
    if broadcast_state:
        broadcast_state["stop_event"].set()
        return await message.reply_text(
            "🛑 Cancellation requested.\n\n"
            "⏳ The broadcast worker is stopping now and will post its final delivery summary."
        )
    if (
        runtime_broadcast_task
        and not runtime_broadcast_task.done()
        and runtime_broadcast_stop_event
    ):
        runtime_broadcast_stop_event.set()
        return await message.reply_text(
            "🛑 Cancellation requested.\n\n"
            "⏳ The active text broadcast is stopping now and will send its final summary shortly."
        )
    await message.reply_text("📭 There is no active broadcast to stop.")


tasks.append(
    asyncio.create_task(scheduled_broadcast_worker(), name="scheduled-broadcast-worker")
)
