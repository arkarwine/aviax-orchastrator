from pathlib import Path


async def maintenance_status_text(chat_id: int, maintenance_id: str | None = None) -> str:
    from anony import anon, config, db, queue

    marker_exists = Path.cwd().joinpath(".restart-when-idle").exists()
    deferred = queue.get_deferred(chat_id)
    position = queue.deferred_position(chat_id, maintenance_id) if maintenance_id else 0
    active_streams = len(db.active_calls)
    grace = anon.maintenance_grace_remaining()

    if marker_exists and grace:
        phase = "🟡 Grace period: existing playback may continue"
        timing = f"approximately <code>{max(1, (grace + 59) // 60)}</code> minute(s) remain"
    elif marker_exists:
        phase = "🟠 Draining: waiting only for currently playing tracks to finish"
        timing = "maintenance begins as soon as those current tracks finish"
    elif deferred:
        phase = "🟢 Maintenance completed: saved requests are being restored"
        timing = "the bot will retry restoration automatically if a voice chat is unavailable"
    else:
        phase = "✅ No maintenance restart is pending"
        timing = "new playback requests can start normally"

    text = (
        "🛠️ <b>Maintenance status</b>\n\n"
        f"{phase}\n"
        f"⏱️ {timing.capitalize()}.\n"
        f"🎙️ Active streams: <code>{active_streams}</code>\n"
        f"💾 Saved requests in this chat: <code>{len(deferred)}</code>\n"
        f"⚙️ Grace period setting: <code>{config.MAINTENANCE_GRACE_MINUTES} minutes</code>\n"
        "🔔 Requesters are notified automatically when saved playback resumes."
    )
    if position:
        text += f"\n📍 Your saved request position: <code>{position}</code>"
    return text
