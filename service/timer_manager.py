"""
Timer Manager — schedules periodic Arduino commands.

Each timer has:
  - id:          unique name ("dht_temp")
  - command:     Arduino protocol string ("dht1:TEMP")
  - interval_s:  seconds between executions
  - enabled:     bool
  - publish_to:  optional MQTT topic for the result
  - parse:       optional result parser ("float", "int", "raw")

Timers are persisted to a JSON file so they survive restarts.
"""

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Callback type: (timer_id, command, publish_to, parse_mode)
TimerFireCallback = Callable[[str, str, Optional[str], Optional[str]], None]


@dataclass
class TimerEntry:
    id: str
    command: str
    interval_s: float
    enabled: bool = True
    publish_to: Optional[str] = None
    parse: Optional[str] = None  # "float", "int", "raw"
    _next_fire: float = field(default=0.0, repr=False, compare=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "command": self.command,
            "interval_s": self.interval_s,
            "enabled": self.enabled,
            "publish_to": self.publish_to,
            "parse": self.parse,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TimerEntry":
        return cls(
            id=d["id"],
            command=d["command"],
            interval_s=d.get("interval_s", 30),
            enabled=d.get("enabled", True),
            publish_to=d.get("publish_to"),
            parse=d.get("parse"),
        )


class TimerManager:
    """
    Manages a collection of periodic timers.

    Usage:
        mgr = TimerManager(persist_file="timers.json")
        mgr.on_fire = my_callback
        mgr.load_defaults(config_list)
        mgr.start()

        mgr.set_timer(TimerEntry(...))   # add / update
        mgr.delete_timer("dht_temp")     # remove
        mgr.list_timers()                # snapshot
    """

    def __init__(self, persist_file: str = "timers.json"):
        self._timers: Dict[str, TimerEntry] = {}
        self._persist_file = persist_file
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # External callback fired when a timer triggers
        self.on_fire: Optional[TimerFireCallback] = None

    # ── Lifecycle ────────────────────────────────────────────

    def load_defaults(self, defaults: List[dict]):
        """
        Load default timers from config, but don't overwrite any
        that were already restored from the persist file.
        """
        self._restore()
        with self._lock:
            for d in defaults:
                entry = TimerEntry.from_dict(d)
                if entry.id not in self._timers:
                    self._timers[entry.id] = entry
                    logger.info("Default timer loaded: %s", entry.id)
        self._persist()

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="timer-manager"
        )
        self._thread.start()
        logger.info("Timer manager started (%d timers)", len(self._timers))

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        self._persist()
        logger.info("Timer manager stopped")

    # ── CRUD ─────────────────────────────────────────────────

    def set_timer(self, entry: TimerEntry):
        """Create or update a timer."""
        with self._lock:
            existing = self._timers.get(entry.id)
            if existing:
                entry._next_fire = existing._next_fire
            self._timers[entry.id] = entry
        self._persist()
        logger.info("Timer set: %s → %s every %ds",
                     entry.id, entry.command, entry.interval_s)

    def delete_timer(self, timer_id: str) -> bool:
        with self._lock:
            removed = self._timers.pop(timer_id, None)
        if removed:
            self._persist()
            logger.info("Timer deleted: %s", timer_id)
            return True
        return False

    def list_timers(self) -> List[dict]:
        with self._lock:
            return [t.to_dict() for t in self._timers.values()]

    # ── Core loop ────────────────────────────────────────────

    def _run_loop(self):
        while self._running:
            now = time.monotonic()
            timers_to_fire: List[TimerEntry] = []

            with self._lock:
                for t in self._timers.values():
                    if not t.enabled:
                        continue
                    if now >= t._next_fire:
                        timers_to_fire.append(t)
                        t._next_fire = now + t.interval_s

            for t in timers_to_fire:
                if self.on_fire:
                    try:
                        self.on_fire(t.id, t.command, t.publish_to, t.parse)
                    except Exception as e:
                        logger.error("Timer fire callback error [%s]: %s", t.id, e)

            time.sleep(0.5)  # tick resolution

    # ── Persistence ──────────────────────────────────────────

    def _persist(self):
        try:
            data = self.list_timers()
            with open(self._persist_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error("Failed to persist timers: %s", e)

    def _restore(self):
        if not os.path.isfile(self._persist_file):
            return
        try:
            with open(self._persist_file) as f:
                data = json.load(f)
            with self._lock:
                for d in data:
                    entry = TimerEntry.from_dict(d)
                    self._timers[entry.id] = entry
            logger.info("Restored %d timers from %s", len(data), self._persist_file)
        except Exception as e:
            logger.warning("Could not restore timers: %s", e)
