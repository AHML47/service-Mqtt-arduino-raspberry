"""
HydroponicBridgeService — orchestrates serial, MQTT, camera, and cycles.

Topic structure:  /hydroponic/{connectionString}/{suffix}

Inbound topics (subscribed):
    ping            {}                              → pong: {status, time}
    readSensor      {"sensor": "<name>"}            → sensorData: {<name>: val, time}
    readAllSensors  {}                              → sensorData (one per sensor)
    command         {"command": "<arduino_cmd>"}    → commandResponse: {command, response, time}
    capturePhoto    {"time": <delay_s>}             → capturedPhoto: <jpeg bytes>

Outbound topics (published):
    sensorData      {"<sensor_name>": <value>, "time": "<iso>"}
    capturedPhoto   <jpeg bytes>
    commandResponse {"command": "...", "response": "...", "time": "..."}
    pong            {"status": "online", "time": "..."}
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone

from .photo_capture import PhotoCaptureError, PhotoCaptureService
from .serial_conn import SerialConnection
from .mqtt_client import MQTTClient
from .sensor_registry import SensorRegistry
from .topic_router import TopicRouter
from .cycle_manager import CycleManager

logger = logging.getLogger(__name__)


class HydroponicBridgeService:
    def __init__(self, config: dict):
        self._config = config
        self._running = False

        mcfg = config["mqtt"]
        self._prefix = mcfg.get("topic_prefix", "hydroponic/default")
        self._publish_sensor_data = bool(mcfg.get("publish_sensor_data", False))

        # ── Sensor registry ───────────────────────────────────
        self._sensors = SensorRegistry(config.get("sensors", []))

        # ── Serial ───────────────────────────────────────────
        scfg = config["serial"]
        self._serial = SerialConnection(
            port=scfg["port"],
            baudrate=scfg["baudrate"],
            timeout=scfg.get("timeout", 1.0),
            response_timeout=scfg.get("response_timeout", 2.0),
        )
        self._serial.on_push = self._handle_push

        # ── Topic router ─────────────────────────────────────
        self._router = TopicRouter()

        # ── MQTT ─────────────────────────────────────────────
        self._mqtt = MQTTClient(
            host=mcfg["host"],
            port=mcfg["port"],
            router=self._router,
            username=mcfg.get("username"),
            password=mcfg.get("password"),
            client_id=mcfg.get("client_id", "hydroponic-bridge"),
            topic_prefix=self._prefix,
            qos=mcfg.get("qos", 1),
            keepalive=mcfg.get("keepalive", 60),
            connect_timeout=mcfg.get("connect_timeout", 30),
            connect_retries=mcfg.get("connect_retries", 1),
        )

        # ── Camera ───────────────────────────────────────────
        pcfg = config.get("photo", {})
        resolution = pcfg.get("resolution", [1280, 720])
        raw_resolution = pcfg.get("raw_resolution", [4608, 2592])
        self._photo = PhotoCaptureService(
            output_dir=pcfg.get("output_dir", "photos"),
            resolution=(int(resolution[0]), int(resolution[1])),
            raw_resolution=(int(raw_resolution[0]), int(raw_resolution[1])),
            warmup_s=float(pcfg.get("warmup_s", 2.0)),
            autofocus=bool(pcfg.get("autofocus", True)),
        )
        self._photo_lock = threading.Lock()

        # ── Register topic handlers ───────────────────────────
        # To add a new inbound topic: call self._router.register() here.
        self._router.register(
            "ping",
            self._handle_ping,
            description="Connection check: {} → pong/{status,time}",
        )
        self._router.register(
            "readSensor",
            self._handle_read_sensor,
            description='Read one sensor: {"sensor": "<name>"} → sensorData',
        )
        self._router.register(
            "readAllSensors",
            self._handle_read_all_sensors,
            description="Read all sensors: {} → sensorData (one message per sensor)",
        )
        self._router.register(
            "command",
            self._handle_command,
            description='Send raw Arduino command: {"command": "<cmd>"} → commandResponse',
        )
        self._router.register(
            "capturePhoto",
            self._handle_capture_photo,
            description='Capture a photo: {"time": <delay_s>} → capturedPhoto',
        )

        # ── Cycle manager ─────────────────────────────────────
        self._cycle_manager = CycleManager(
            cycles_config=config.get("cycles", []),
            serial=self._serial,
            mqtt=self._mqtt,
            topic_prefix=self._prefix,
            sensor_registry=self._sensors,
            publish_sensor_data=self._publish_sensor_data,
        )
        self._cycle_manager.on_capture_photo = lambda: self._capture_and_publish(0.0)

    # ── Lifecycle ─────────────────────────────────────────────

    def run(self):
        logger.info("=" * 50)
        logger.info("  Hydroponic Bridge Service starting")
        logger.info("=" * 50)
        logger.info("Topic prefix: %s", self._prefix)
        logger.info("Registered inbound topics:")
        for line in self._router.describe():
            logger.info(line)

        try:
            self._photo.start()
            self._serial.start()
            self._mqtt.start()
            self._cycle_manager.start()
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
        self._cycle_manager.stop()
        self._mqtt.stop()
        self._serial.stop()
        self._photo.stop()
        logger.info("Service stopped")

    # ── Inbound topic handlers ────────────────────────────────

    def _handle_ping(self, _payload: dict):
        """payload: {} — responds with online status."""
        self._mqtt.publish_raw(
            f"{self._prefix}/pong",
            json.dumps({"status": "online", "time": _now_iso()}),
        )

    def _handle_read_sensor(self, payload: dict):
        """payload: {"sensor": "<name>"} — reads one sensor and publishes to sensorData."""
        sensor_name = payload.get("sensor", "").strip()
        if not sensor_name:
            logger.warning("readSensor: payload missing 'sensor' key")
            return
        value = self._sensors.read_sensor(sensor_name, self._serial)
        if value is not None:
            self._mqtt.publish_sensor_data(sensor_name, value)

    def _handle_read_all_sensors(self, _payload: dict):
        """payload: {} — reads every registered sensor and publishes each to sensorData."""
        for sensor in self._sensors.all():
            value = self._sensors.read_sensor(sensor.name, self._serial)
            if value is not None:
                self._mqtt.publish_sensor_data(sensor.name, value)

    def _handle_command(self, payload: dict):
        """payload: {"command": "<arduino_cmd>"} — forwards command and publishes response."""
        command = payload.get("command", "").strip()
        if not command:
            logger.warning("command: payload missing 'command' key")
            return
        response = self._serial.send(command)
        self._mqtt.publish_raw(
            f"{self._prefix}/commandResponse",
            json.dumps({"command": command, "response": response, "time": _now_iso()}),
        )

    def _handle_capture_photo(self, payload: dict):
        """payload: {"time": <delay_s>} — captures a photo after optional delay."""
        delay_s = _parse_delay(payload)
        logger.info("capturePhoto requested; delay=%.3fs", delay_s)
        threading.Thread(
            target=self._capture_and_publish,
            args=(delay_s,),
            daemon=True,
            name="photo-capture",
        ).start()

    # ── Serial push → MQTT sensorData ─────────────────────────

    def _handle_push(self, device: str, payload: str):
        """Receive unsolicited Arduino push, map via registry, publish to sensorData."""
        if not self._publish_sensor_data:
            return
        for sensor_name, value in self._sensors.parse_push(device, payload):
            self._mqtt.publish_sensor_data(sensor_name, value)

    # ── Photo helpers ─────────────────────────────────────────

    def _capture_and_publish(self, delay_s: float):
        try:
            result = self._photo.capture(delay_s=delay_s)
            with self._photo_lock:
                self._mqtt.publish_captured_photo(result.content)
            logger.info("Photo published: %s (%d bytes)", result.filename, len(result.content))
        except PhotoCaptureError as exc:
            logger.error("Photo capture failed: %s", exc)
        except Exception as exc:
            logger.exception("Unexpected photo capture error: %s", exc)


# ── Utilities ─────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
