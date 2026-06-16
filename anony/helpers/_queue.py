# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import json
import math
import threading
from collections import defaultdict, deque
from dataclasses import fields
from enum import Enum
from pathlib import Path
from typing import Any, Union

from ._dataclass import Media, Track

MediaItem = Union[Media, Track]


class Queue:
    def __init__(self):
        self.queues: dict[int, deque[MediaItem]] = defaultdict(deque)
        self.deferred: dict[int, deque[MediaItem]] = defaultdict(deque)
        self.deferred_path = Path.cwd() / ".maintenance-queue.json"
        self.live_path = Path.cwd() / ".playback-queue.json"
        self._deferred_persist_lock = threading.RLock()
        self._live_persist_lock = threading.Lock()
        self._live_persist_dirty = False
        self._live_persist_running = False
        self.load_deferred()
        self.recover_live()

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else 0
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, Enum):
            return Queue._json_safe(value.value)
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, dict):
            return {
                str(key): Queue._json_safe(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, deque)):
            return [Queue._json_safe(item) for item in value]
        return str(value)

    @classmethod
    def _serialize(cls, item: MediaItem) -> dict:
        return {
            "type": type(item).__name__,
            "data": {
                field.name: cls._json_safe(getattr(item, field.name))
                for field in fields(item)
            },
        }

    @staticmethod
    def _deserialize(item: dict) -> MediaItem:
        item_type = Track if item.get("type") == "Track" else Media
        return item_type(**item["data"])

    def save_deferred(self) -> None:
        with self._deferred_persist_lock:
            data = {
                str(chat_id): [self._serialize(item) for item in items]
                for chat_id, items in self.deferred.items()
                if items
            }
            temporary = self.deferred_path.with_suffix(".json.tmp")
            try:
                temporary.write_text(
                    json.dumps(data, ensure_ascii=True),
                    encoding="utf-8",
                )
                temporary.replace(self.deferred_path)
            finally:
                temporary.unlink(missing_ok=True)

    def save_live(self) -> None:
        with self._live_persist_lock:
            self._live_persist_dirty = True
            if self._live_persist_running:
                return
            self._live_persist_running = True
        threading.Thread(
            target=self._live_persist_worker,
            name="playback-queue-persist",
            daemon=True,
        ).start()

    def _live_persist_worker(self) -> None:
        while True:
            with self._live_persist_lock:
                if not self._live_persist_dirty:
                    self._live_persist_running = False
                    return
                self._live_persist_dirty = False
            try:
                data = {
                    str(chat_id): [self._serialize(item) for item in list(items)]
                    for chat_id, items in list(self.queues.items())
                    if items
                }
                temporary = self.live_path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
                temporary.replace(self.live_path)
            except Exception:
                # The next queue mutation retries persistence without blocking playback.
                pass

    def recover_live(self) -> None:
        try:
            data = json.loads(self.live_path.read_text(encoding="utf-8"))
            for chat_id, items in data.items():
                recovered = [self._deserialize(item) for item in items]
                if recovered:
                    self.deferred[int(chat_id)] = deque(
                        [*recovered, *self.deferred[int(chat_id)]]
                    )
            self.save_deferred()
            self.live_path.unlink(missing_ok=True)
        except FileNotFoundError:
            return
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            try:
                self.live_path.replace(self.live_path.with_suffix(".invalid.json"))
            except OSError:
                pass

    def load_deferred(self) -> None:
        try:
            data = json.loads(self.deferred_path.read_text(encoding="utf-8"))
            for chat_id, items in data.items():
                self.deferred[int(chat_id)].extend(
                    self._deserialize(item) for item in items
                )
        except FileNotFoundError:
            return
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            try:
                self.deferred_path.replace(self.deferred_path.with_suffix(".invalid.json"))
            except OSError:
                pass

    def defer(self, chat_id: int, item: MediaItem) -> int:
        return self.defer_many(chat_id, [item])[0]

    def defer_many(self, chat_id: int, items: list[MediaItem]) -> list[int]:
        with self._deferred_persist_lock:
            original_length = len(self.deferred[chat_id])
            positions = []
            for item in items:
                self.deferred[chat_id].append(item)
                positions.append(len(self.deferred[chat_id]))
            try:
                self.save_deferred()
            except Exception:
                while len(self.deferred[chat_id]) > original_length:
                    self.deferred[chat_id].pop()
                raise
            return positions

    def get_deferred(self, chat_id: int) -> list[MediaItem]:
        return list(self.deferred[chat_id])

    def deferred_chats(self) -> list[int]:
        return [chat_id for chat_id, items in self.deferred.items() if items]

    def deferred_count(self) -> int:
        return sum(len(items) for items in self.deferred.values())

    def live_count(self) -> int:
        return sum(len(items) for items in self.queues.values())

    def deferred_position(self, chat_id: int, maintenance_id: str) -> int:
        return next(
            (
                position
                for position, item in enumerate(self.deferred[chat_id], start=1)
                if item.maintenance_id == maintenance_id
            ),
            0,
        )

    def remove_deferred(self, chat_id: int, maintenance_id: str) -> MediaItem | None:
        items = self.deferred[chat_id]
        for item in items:
            if item.maintenance_id == maintenance_id:
                items.remove(item)
                self.save_deferred()
                return item
        return None

    def defer_live_remaining(self, chat_id: int) -> int:
        live = self.queues[chat_id]
        if len(live) <= 1:
            return 0
        remaining = list(live)[1:]
        self.deferred[chat_id] = deque([*remaining, *self.deferred[chat_id]])
        while len(live) > 1:
            live.pop()
        self.save_deferred()
        self.save_live()
        return len(remaining)

    def pop_deferred(self, chat_id: int) -> list[MediaItem]:
        items = list(self.deferred.pop(chat_id, []))
        self.save_deferred()
        return items

    def add(self, chat_id: int, item: MediaItem) -> int:
        """Add an item to the queue and return its position (1-based)."""
        self.queues[chat_id].append(item)
        self.save_live()
        return len(self.queues[chat_id]) - 1

    def duplicate_position(self, chat_id: int, item_id: str) -> int:
        combined = [*self.queues[chat_id], *self.deferred[chat_id]]
        return next(
            (position for position, item in enumerate(combined) if item.id == item_id),
            -1,
        )

    def requester_pending_count(self, chat_id: int, requester_id: int) -> int:
        live_pending = list(self.queues[chat_id])[1:]
        return sum(
            item.requester_id == requester_id
            for item in [*live_pending, *self.deferred[chat_id]]
        )

    def position(self, chat_id: int, queue_id: str) -> int:
        return next(
            (
                position
                for position, item in enumerate(self.queues[chat_id])
                if item.queue_id == queue_id
            ),
            -1,
        )

    def remove(self, chat_id: int, queue_id: str) -> MediaItem | None:
        for position, item in enumerate(self.queues[chat_id]):
            if item.queue_id == queue_id and position > 0:
                self.queues[chat_id].remove(item)
                self.save_live()
                return item
        return None

    def estimated_wait(self, chat_id: int, position: int) -> int:
        if position <= 0:
            return 0
        items = list(self.queues[chat_id])[:position]
        if not items:
            return 0
        current = items[0]
        total = max(0, current.duration_sec - current.time)
        total += sum(max(0, item.duration_sec) for item in items[1:])
        return total

    def check_item(self, chat_id: int, item_id: str) -> tuple[int, MediaItem | None]:
        """Check if an item with the given ID exists in the queue."""
        pos, track = next(
            (
                (i, track)
                for i, track in enumerate(list(self.queues[chat_id]))
                if track.id == item_id
            ),
            (-1, None),
        )
        return pos, track

    def force_add(
        self, chat_id: int, item: MediaItem, remove: int | bool = False
    ) -> None:
        """Replace the currently playing item with a new one."""
        self.remove_current(chat_id)
        self.queues[chat_id].appendleft(item)
        if remove:
            self.queues[chat_id].rotate(-remove)
            self.queues[chat_id].popleft()
            self.queues[chat_id].rotate(remove)
        self.save_live()

    def get_current(self, chat_id: int) -> MediaItem | None:
        """Return the currently playing item (first in queue), if any."""
        return self.queues[chat_id][0] if self.queues[chat_id] else None

    def get_next(self, chat_id: int, check: bool = False) -> MediaItem | None:
        """Remove current item and return the next one, or None if empty."""
        if not self.queues[chat_id]:
            return None
        if check:
            return self.queues[chat_id][1] if len(self.queues[chat_id]) > 1 else None

        self.queues[chat_id].popleft()
        self.save_live()
        return self.queues[chat_id][0] if self.queues[chat_id] else None

    def get_queue(self, chat_id: int) -> list[MediaItem]:
        """Return the full queue including the currently playing item."""
        return list(self.queues[chat_id])

    def remove_current(self, chat_id: int) -> None:
        """Remove the currently playing item only (if exists)."""
        if self.queues[chat_id]:
            self.queues[chat_id].popleft()
            self.save_live()

    def remove_current_if(self, chat_id: int, queue_id: str | None) -> bool:
        """Remove the current item only when it still matches the expected request."""
        if self.queues[chat_id] and self.queues[chat_id][0].queue_id == queue_id:
            self.queues[chat_id].popleft()
            self.save_live()
            return True
        return False

    def clear(self, chat_id: int) -> None:
        """Clear the entire queue."""
        self.queues[chat_id].clear()
        self.save_live()
