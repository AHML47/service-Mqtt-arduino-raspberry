"""
HydroponicBridgeService — wires serial (Arduino) + MQTT + camera.

Topics:
    PUB  {prefix}/sensorData    → {"sensorname":"<name>","value":<v>,"time":"<iso>"}
    SUB  {prefix}/capturePhoto  → {"time": <delay_seconds>}
    PUB  {prefix}/capturedPhoto → JPEG bytes
"""

import logging
import threading
import time
from datetime import datetime, timezone

from .photo_capture import PhotoCaptureError, PhotoCaptureService
from .serial_conn import SerialConnection
from .mqtt_client import MQTTClient

logger = logging.getLogger(__name__)


class HydroponicBridgeService:
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
            client_id=mcfg.get("client_id", "hydroponic-bridge"),
            topic_prefix=mcfg.get("topic_prefix", "hydroponic/default"),
            qos=mcfg.get("qos", 1),
            keepalive=mcfg.get("keepalive", 60),
            connect_timeout=mcfg.get("connect_timeout", 30),
            connect_retries=mcfg.get("connect_retries", 1),
        )
        self._mqtt.on_capture_photo = self._handle_capture_photo

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

    # ── Lifecycle ────────────────────────────────────────────

    def run(self):
        logger.info("=" * 50)
        logger.info("  Hydroponic Bridge Service starting")
        logger.info("=" * 50)
        try:
            self._photo_capture.start()
            self._serial.start()
            self._mqtt.start()
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
        self._mqtt.stop()
        self._serial.stop()
        self._photo_capture.stop()
        logger.info("Service stopped")

    # ── Arduino → MQTT (sensor data) ─────────────────────────

    def _handle_push(self, device: str, payload: str):
        """Parse unsolicited Arduino push and publish to sensorData."""
        # DHT sensor: "OK:temperature:humidity"
        if device.lower().startswith("dht") and payload.startswith("OK:"):
            parts = payload[3:].split(":")
            if len(parts) >= 2:
                try:
                    self._mqtt.publish_sensor_data("temperature", float(parts[0]))
                    self._mqtt.publish_sensor_data("humidity", float(parts[1]))
                    return
                except ValueError:
                    pass

        # Generic sensor: "OK:value" or bare value
        raw_value = payload[3:] if payload.startswith("OK:") else payload
        try:
            value = float(raw_value)
        except ValueError:
            value = raw_value

        self._mqtt.publish_sensor_data(device, value)

    # ── MQTT → Camera ─────────────────────────────────────────

    def _handle_capture_photo(self, data: dict):
        """Schedule a photo capture after the delay in the payload."""
        delay_s = _parse_delay(data)
        logger.info("capturePhoto received; delay=%.3fs", delay_s)
        threading.Thread(
            target=self._capture_and_publish,
            args=(delay_s,),
            daemon=True,
            name="photo-capture",
        ).start()

    def _capture_and_publish(self, delay_s: float):
        try:
            result = self._photo_capture.capture(delay_s=delay_s)
            with self._photo_lock:
                self._mqtt.publish_captured_photo(result.content)
            logger.info("Photo published: %s (%d bytes)", result.filename, len(result.content))
        except PhotoCaptureError as exc:
            logger.error("Photo capture failed: %s", exc)
        except Exception as exc:
            logger.exception("Unexpected photo capture error: %s", exc)


def _parse_delay(data: dict) -> float:
    raw = data.get("time", 0)
    if raw is None or raw == "":
        return 0.0
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    if isinstance(raw, str):
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            target = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            now = datetime.now(target.tzinfo or timezone.utc)
            return max(0.0, (target - now).total_seconds())
        except ValueError:
            pass
    return 0.0
