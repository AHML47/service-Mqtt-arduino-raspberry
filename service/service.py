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
from datetime import datetime, timezone
from typing import Optional

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

        # ── Timers ───────────────────────────────────────────
        tcfg = config.get("timers", {})
        self._timers = TimerManager(
            persist_file=tcfg.get("persist_file", "timers.json"),
        )
        self._timers.on_fire = self._handle_timer_fire
        self._timer_defaults = tcfg.get("defaults", [])

    # ── Lifecycle ────────────────────────────────────────────

    def run(self):
        """Start all subsystems and block until interrupted."""
        logger.info("=" * 50)
        logger.info("  Arduino Bridge Service starting")
        logger.info("=" * 50)

        try:
            self._serial.start()
            self._mqtt.start()
            self._timers.load_defaults(self._timer_defaults)
            self._timers.start()

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
        Parse it and publish to appropriate MQTT topics.
        """
        self._mqtt.publish(f"push/{device}", {
            "value": payload,
            "time": _now_iso(),
        })

        # Special handling for DHT pushes → split into temp/humidity topics
        if device.startswith("dht"):
            parsed = parse_dht_push(payload)
            if parsed:
                # Publish sensor data under the unified `sensorData` topic as
                # { "sensor name": value } per requested format.
                self._mqtt.publish("sensorData", {
                    "temperature": parsed["temperature"],
                })
                self._mqtt.publish("sensorData", {
                    "humidity": parsed["humidity"],
                })

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
