"""
ArduinoBridgeService — the central coordinator.

Wires together:
    SerialConnection  ↔  Arduino hardware
    MQTTClient        ↔  MQTT broker
    TimerManager      ↔  scheduled commands

Data flow:

    MQTT cmd topic  →  SerialConnection.send()  →  Arduino
                                                      ↓
    MQTT resp topic ←  response parsing          ←  response

    TimerManager fires  →  SerialConnection.send()  →  Arduino
                                                          ↓
    MQTT publish_to     ←  parsed value              ←  response

    Arduino push (unsolicited)  →  parse  →  MQTT push/{device}
"""

import logging
import time
import threading
from datetime import datetime, timezone
from typing import Optional

from .photo_capture import PhotoCaptureError, PhotoCaptureService
from .serial_conn import SerialConnection
from .mqtt_client import MQTTClient
from .timer_manager import TimerManager, TimerEntry
from .parsers import parse_response, extract_value, parse_dht_push

logger = logging.getLogger(__name__)


class ArduinoBridgeService:
    """
    Top-level service. Instantiate with a config dict, call run().
    """

    def __init__(self, config: dict):
        self._config = config
        self._running = False

        # ── Serial ───────────────────────────────────────────
        scfg = config["serial"]
        self._serial = SerialConnection(
            port=scfg["port"],
            baudrate=scfg["baudrate"],
            timeout=scfg.get("timeout", 1.0),
            response_timeout=scfg.get("response_timeout", 2.0),
        )
        self._serial.on_push = self._handle_push

        # ── MQTT ─────────────────────────────────────────────
        mcfg = config["mqtt"]
        self._mqtt = MQTTClient(
            host=mcfg["host"],
            port=mcfg["port"],
            username=mcfg.get("username"),
            password=mcfg.get("password"),
            client_id=mcfg.get("client_id", "arduino-bridge"),
            topic_prefix=mcfg.get("topic_prefix", "arduino"),
            qos=mcfg.get("qos", 1),
            keepalive=mcfg.get("keepalive", 60),
            connect_timeout=mcfg.get("connect_timeout", 30),
            connect_retries=mcfg.get("connect_retries", 1),
        )
        self._mqtt.on_command = self._handle_mqtt_command
        self._mqtt.on_timer_set = self._handle_timer_set
        self._mqtt.on_timer_delete = self._handle_timer_delete
        self._mqtt.on_timer_list = self._handle_timer_list
        self._mqtt.on_capture_order = self._handle_capture_order

        # ── Timers ───────────────────────────────────────────
        tcfg = config.get("timers", {})
        self._timers = TimerManager(
            persist_file=tcfg.get("persist_file", "timers.json"),
        )
        self._timers.on_fire = self._handle_timer_fire
        self._timer_defaults = tcfg.get("defaults", [])
        # Flags to control timer behavior (can be set in config.yaml)
        self._timers_enabled = bool(tcfg.get("enabled", True))
        self._timers_load_defaults = bool(tcfg.get("load_defaults", True))

        # ── Camera ───────────────────────────────────────────
        pcfg = config.get("photo", {})
        resolution = pcfg.get("resolution", [4608, 2592])
        self._photo_capture = PhotoCaptureService(
            output_dir=pcfg.get("output_dir", "photos"),
            resolution=(int(resolution[0]), int(resolution[1])),
            warmup_s=float(pcfg.get("warmup_s", 2.0)),
            autofocus=bool(pcfg.get("autofocus", True)),
        )
        self._photo_lock = threading.Lock()
        # By default, automatically publish sensorData on unsolicited pushes.
        # This will be disabled when a timer is created via MQTT.
        self._auto_publish_sensor = True

    # ── Lifecycle ────────────────────────────────────────────

    def run(self):
        """Start all subsystems and block until interrupted."""
        logger.info("=" * 50)
        logger.info("  Arduino Bridge Service starting")
        logger.info("=" * 50)

        try:
            self._serial.start()
            self._mqtt.start()
            if self._timers_load_defaults:
                self._timers.load_defaults(self._timer_defaults)
            else:
                logger.info("Timer defaults loading disabled by config")

            if self._timers_enabled:
                self._timers.start()
            else:
                logger.info("Timer subsystem disabled by config; timers will not run")

            self._running = True
            logger.info("Service is running. Press Ctrl+C to stop.")

            while self._running:
                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Shutdown requested")
        except Exception as e:
            logger.exception("Fatal error: %s", e)
        finally:
            self._shutdown()

    def _shutdown(self):
        self._running = False
        if self._timers_enabled:
            self._timers.stop()
        self._mqtt.stop()
        self._serial.stop()
        logger.info("Service stopped")

    # ── MQTT → Arduino (command passthrough) ─────────────────

    def _handle_mqtt_command(self, command: str):
        """
        Received a raw command from MQTT (e.g. "dht1:READ").
        Forward it to Arduino and publish the response.
        """
        logger.info("MQTT cmd: %s", command)
        response = self._serial.send(command)

        self._mqtt.publish("resp", {
            "cmd": command,
            "resp": response,
            "time": _now_iso(),
        })

    # ── Arduino → MQTT (unsolicited push) ────────────────────

    def _handle_push(self, device: str, payload: str):
        """
        Arduino sent an unsolicited line like "dht1:OK:24.5:55.0".
        Parse it and publish `sensorData` messages (single-key JSON) when
        automatic publishing is enabled. This mirrors the format used by
        `service/test_publisher.py`.
        """
        # Only publish sensorData when auto-publish is enabled
        if not getattr(self, "_auto_publish_sensor", True):
            return

        # Special handling for DHT pushes → publish temperature and humidity
        if device.startswith("dht"):
            parsed = parse_dht_push(payload)
            if parsed:
                # Publish separate messages following test_publisher.py format
                self._mqtt.publish("sensorData", {"temperature": parsed["temperature"]})
                self._mqtt.publish("sensorData", {"humidity": parsed["humidity"]})

    # ── Timer fire → Arduino → MQTT ─────────────────────────

    def _handle_timer_fire(
        self,
        timer_id: str,
        command: str,
        publish_to: Optional[str],
        parse_mode: Optional[str],
    ):
        """A timer triggered — send the command and publish the result."""
        logger.debug("Timer [%s] firing: %s", timer_id, command)
        response = self._serial.send(command)

        status, _ = parse_response(response)
        value = extract_value(response, parse_mode)

        # Publish to the timer's custom topic (or default resp topic)
        result_payload = {
            "timer": timer_id,
            "cmd": command,
            "resp": response,
            "value": value,
            "time": _now_iso(),
        }

        if publish_to:
            # Normalize publish_to so it uses the current MQTT prefix.
            topic = publish_to
            prefix = self._mqtt._prefix
            if topic.startswith(prefix + "/"):
                final_topic = topic
            elif topic.startswith("arduino/"):
                # Replace legacy 'arduino' prefix with the current prefix
                final_topic = prefix + topic[len("arduino"):]
            else:
                # Treat as relative and prefix it
                final_topic = f"{prefix}/{topic.lstrip('/') }"

            self._mqtt.publish_raw(final_topic, result_payload)
        else:
            self._mqtt.publish("resp", result_payload)

    # ── Timer CRUD via MQTT ──────────────────────────────────

    def _handle_timer_set(self, data: dict):
        """Create or update a timer from MQTT payload."""
        try:
            entry = TimerEntry.from_dict(data)
            self._timers.set_timer(entry)
            # When a timer is set via MQTT, stop automatic unsolicited sensor publishes
            try:
                self._auto_publish_sensor = False
                logger.info("Automatic sensorData publishing disabled due to timer set: %s", entry.id)
            except Exception:
                pass
            self._mqtt.publish("timer/status", {
                "action": "set",
                "timer": entry.to_dict(),
                "time": _now_iso(),
            })
        except (KeyError, TypeError) as e:
            logger.error("Invalid timer payload: %s — %s", data, e)
            self._mqtt.publish("timer/status", {
                "error": f"invalid payload: {e}",
                "time": _now_iso(),
            })

    def _handle_timer_delete(self, timer_id: str):
        """Delete a timer by id."""
        success = self._timers.delete_timer(timer_id)
        self._mqtt.publish("timer/status", {
            "action": "delete",
            "id": timer_id,
            "success": success,
            "time": _now_iso(),
        })

    def _handle_timer_list(self):
        """Publish the full list of active timers."""
        timers = self._timers.list_timers()
        self._mqtt.publish("timer/status", {
            "action": "list",
            "timers": timers,
            "time": _now_iso(),
        })

    def _handle_capture_order(self, data: dict):
        """Schedule a photo capture from MQTT payload."""
        delay_s = _parse_capture_delay(data)
        logger.info("Capture order received; delaying %.3fs", delay_s)

        worker = threading.Thread(
            target=self._capture_and_publish_photo,
            args=(delay_s,),
            daemon=True,
            name="photo-capture",
        )
        worker.start()

    def _capture_and_publish_photo(self, delay_s: float):
        try:
            result = self._photo_capture.capture(delay_s=delay_s)
            with self._photo_lock:
                self._mqtt.publish("photo", result.content)

            logger.info(
                "Photo captured and published: %s (%d bytes)",
                result.filename,
                len(result.content),
            )
        except PhotoCaptureError as exc:
            logger.error("Photo capture failed: %s", exc)
        except Exception as exc:
            logger.exception("Unexpected photo capture error: %s", exc)


def _parse_capture_delay(data: dict) -> float:
    raw_value = data.get("time", 0)

    if raw_value is None or raw_value == "":
        return 0.0

    if isinstance(raw_value, (int, float)):
        return max(0.0, float(raw_value))

    if isinstance(raw_value, str):
        try:
            return max(0.0, float(raw_value))
        except ValueError:
            pass

        try:
            target = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            now = datetime.now(target.tzinfo or timezone.utc)
            return max(0.0, (target - now).total_seconds())
        except ValueError as exc:
            raise ValueError(
                "captureorder.time must be a delay in seconds or an ISO-8601 timestamp"
            ) from exc

    raise ValueError("captureorder.time must be numeric or a timestamp string")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
