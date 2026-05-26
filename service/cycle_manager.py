"""
CycleManager — executes command groups on a fixed interval.

Each cycle group runs in its own daemon thread. Commands can target the
Arduino (serial), publish directly to MQTT, or trigger a photo capture.

Config structure (config.yaml → cycles):
    - name: "group_name"
      interval: 3600          # seconds between executions
      run_on_start: false     # execute immediately on start(), then repeat
      commands:
        - type: serial
          command: "dht11:READ"
          publish_response: true   # parse OK response → sensorData
          delay_after: 2           # seconds to wait after this command
        - type: photo
          delay_after: 2
        - type: mqtt
          topic: "{prefix}/cycleEvent"
          payload: '{"event": "ping"}'
"""

import logging
import threading
import time
from typing import Callable, List, Optional

from .serial_conn import SerialConnection
from .mqtt_client import MQTTClient
from .sensor_registry import SensorRegistry

logger = logging.getLogger(__name__)

CaptureCallback = Callable[[], None]


class CycleManager:
    def __init__(
        self,
        cycles_config: list,
        serial: SerialConnection,
        mqtt: MQTTClient,
        topic_prefix: str,
        sensor_registry: SensorRegistry,
        publish_sensor_data: bool = False,
    ):
        self._cycles = cycles_config
        self._serial = serial
        self._mqtt = mqtt
        self._prefix = topic_prefix
        self._sensor_registry = sensor_registry
        self._publish_sensor_data = publish_sensor_data
        self._stop_events: List[threading.Event] = []
        self._threads: List[threading.Thread] = []

        self.on_capture_photo: Optional[CaptureCallback] = None

    # ── Lifecycle ────────────────────────────────────────────

    def start(self):
        if not self._cycles:
            logger.info("CycleManager: no cycles configured")
            return

        for group in self._cycles:
            stop_event = threading.Event()
            self._stop_events.append(stop_event)
            name = group.get("name", "unnamed")
            t = threading.Thread(
                target=self._cycle_loop,
                args=(group, stop_event),
                daemon=True,
                name=f"cycle-{name}",
            )
            self._threads.append(t)
            t.start()

        logger.info("CycleManager started: %d group(s)", len(self._cycles))

    def stop(self):
        for event in self._stop_events:
            event.set()
        for t in self._threads:
            t.join(timeout=5)
        logger.info("CycleManager stopped")

    # ── Internal: cycle loop ─────────────────────────────────

    def _cycle_loop(self, group: dict, stop_event: threading.Event):
        name = group.get("name", "unnamed")
        interval = float(group.get("interval", 3600))
        run_on_start = bool(group.get("run_on_start", False))
        commands = group.get("commands", [])

        logger.info(
            "Cycle '%s': interval=%.0fs, commands=%d, run_on_start=%s",
            name, interval, len(commands), run_on_start,
        )

        if run_on_start:
            self._execute_group(name, commands)

        while not stop_event.wait(timeout=interval):
            self._execute_group(name, commands)

    def _execute_group(self, name: str, commands: list):
        logger.info("Cycle '%s': running %d command(s)", name, len(commands))
        for cmd in commands:
            try:
                self._execute_command(cmd)
            except Exception as exc:
                logger.exception("Cycle '%s': command failed: %s", name, exc)
            delay = float(cmd.get("delay_after", 0))
            if delay > 0:
                time.sleep(delay)

    def _execute_command(self, cmd: dict):
        cmd_type = cmd.get("type", "serial")

        if cmd_type == "serial":
            command = cmd.get("command", "").strip()
            if not command:
                logger.warning("Cycle: serial entry missing 'command' field")
                return
            response = self._serial.send(command)
            logger.debug("Cycle serial: %s → %s", command, response)
            if cmd.get("publish_response", False):
                self._publish_serial_response(command, response)

        elif cmd_type == "mqtt":
            topic = cmd.get("topic", "").replace("{prefix}", self._prefix)
            payload = cmd.get("payload", "")
            if not topic:
                logger.warning("Cycle: mqtt entry missing 'topic' field")
                return
            self._mqtt.publish_raw(topic, payload)
            logger.debug("Cycle mqtt: %s → %s", topic, str(payload)[:120])

        elif cmd_type == "photo":
            if self.on_capture_photo:
                self.on_capture_photo()
            else:
                logger.warning("Cycle: photo command but on_capture_photo is not set")

        else:
            logger.warning("Cycle: unknown command type '%s'", cmd_type)

    # ── Serial response → MQTT sensorData ────────────────────

    def _publish_serial_response(self, command: str, response: str):
        if not self._publish_sensor_data:
            logger.debug("Cycle: sensorData publishing disabled; skipping '%s'", command)
            return

        if not response.startswith("OK:"):
            logger.warning("Cycle: non-OK response for '%s': %s", command, response)
            return

        device = command.split(":")[0].lower()
        for sensor_name, value in self._sensor_registry.parse_push(device, response):
            self._mqtt.publish_sensor_data(sensor_name, value)
