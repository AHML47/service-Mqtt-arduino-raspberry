"""
HydroponicBridgeService — orchestrates serial, MQTT, camera, and cycles.

Topic structure:  /hydroponic/{connectionString}/{suffix}

Inbound topics (subscribed):
    ping            {}                              → pong: {status, time}
    readSensor      {"sensor": "<name>"}            → sensorData: {<name>: val, time}
    readAllSensors  {}                              → sensorData (one per sensor)
    command         {"command": "<arduino_cmd>"}    → commandResponse: {command, response, time}
    {cameraName}/capturePhoto                         → capturedPhoto: <jpeg bytes>
                    payload: {"id": ..., "param": {"time": <delay_s>}}

Outbound topics (published):
    sensorData      {"<sensor_name>": <value>, "time": "<iso>"}
    capturedPhoto   <jpeg bytes>
    commandResponse {"command": "...", "response": "...", "time": "..."}
    pong            {"status": "online", "time": "..."}
    ai/alert        {"image": "<base64-jpeg>", "descreption": "<class: confidence>"}
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
import functools
from pathlib import Path

from .photo_capture import PhotoCaptureError, PhotoCaptureResult
from .camera_stream import CameraStreamServer
from .serial_conn import SerialConnection
from .mqtt_client import MQTTClient
from .camera_registry import CameraRegistry
from .sensor_registry import SensorRegistry
from .operator_registry import OperatorRegistry
from .topic_router import TopicRouter
from .cycle_manager import CycleManager
from .ai_inference import PlantDiseaseDES
from .config import _build_dynamic_cycle

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
        # ── Operator registry ──────────────────────────────────
        self._operators = OperatorRegistry(config.get("operators", []))

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

        # ── Local ONNX AI inference ──────────────────────────────
        acfg = config.get("ai", {})
        self._ai = None
        self._ai_enabled = bool(acfg.get("enabled", False))
        if self._ai_enabled:
            try:
                self._ai = PlantDiseaseDES(
                    models_dir=Path(acfg.get("models_dir", "onnx_raspberry_pi")),
                    cpu_threads=int(acfg.get("threads", 4)),
                )
            except Exception:
                logger.exception("Failed to initialize local ONNX AI pipeline")
                if bool(acfg.get("required", True)):
                    raise
                logger.warning("Continuing with AI detection disabled")
                self._ai_enabled = False

        # ── Camera registry ───────────────────────────────────
        pcfg = config.get("photo", {})
        resolution = pcfg.get("resolution", [1280, 720])
        raw_resolution = pcfg.get("raw_resolution", [4608, 2592])
        scfg = config.get("streaming", {})
        stream_enabled = bool(scfg.get("enabled", False))
        photo_defaults = dict(pcfg)
        photo_defaults["streaming"] = stream_enabled
        photo_defaults.setdefault("name", "camera1")
        self._cameras = CameraRegistry(config.get("cameras", []), photo_defaults)
        self._stream_server = (
            CameraStreamServer(
                port=int(scfg.get("port", 8000)),
                photo_dir=pcfg.get("output_dir", "photos"),
            )
            if stream_enabled
            else None
        )

        # ── Register topic handlers ───────────────────────────
        # To add a new inbound topic: call self._router.register() here.
        default_camera_name = self._cameras.default_name()
        if default_camera_name:
            self._router.register(
                "ai/scan",
                self._handle_ai_scan,
                description="Trigger camera capture and AI analysis: {} → ai/alert",
            )

        # ── Operator topics ────────────────────────────────────
        for op in self._operators.all():
            self._router.register(
                f"{op.name}/cmd",
                functools.partial(self._handle_operator_cmd, op),
                description=f'Operator {op.name}: {{"id","param":{{...}}}} -> {op.name}/resp',
            )

        # ── Cycle manager ─────────────────────────────────────
        self._static_cycles = list(config.get("cycles", []))
        self._cycle_manager = CycleManager(
            cycles_config=config.get("cycles", []),
            serial=self._serial,
            mqtt=self._mqtt,
            topic_prefix=self._prefix,
            sensor_registry=self._sensors,
            publish_sensor_data=self._publish_sensor_data,
            operator_registry=self._operators,
        )
        default_camera = self._cameras.default_name()
        if default_camera:
            self._cycle_manager.on_capture_photo = lambda: self._capture_and_publish(default_camera, 0.0)

        # ── Dynamic Cycle Config topic ────────────────────────
        self._router.register(
            "config/cycle",
            self._handle_config_cycle,
            description="Dynamically reloads the active production cycle parameters: {}",
        )

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
            self._cameras.start_all()
            if self._stream_server:
                primary_name = self._cameras.default_name()
                primary_camera = self._cameras.get(primary_name) if primary_name else None
                if primary_camera is None:
                    raise RuntimeError("No camera configured for streaming")
                self._stream_server.start(
                    primary_camera.service.streaming_output,
                    primary_camera.service.camera_state,
                    on_capture=self._handle_stream_capture,
                )
            self._serial.start()
            self._mqtt.start()
            self._cycle_manager.start()
            
            # Start heartbeat thread
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()
            
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
        if self._stream_server:
            self._stream_server.stop()
        self._cameras.stop_all()
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
        params = payload.get("param") or {}
        sensor_name = (payload.get("sensor") or params.get("sensor") or "").strip()
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

    def _handle_operator_cmd(self, op, payload: dict):
        """Handle incoming operator command by dispatching to a thread.

        The backend sends payloads in two possible formats:
          1. Direct serial: {"id":"…","param":{"command":"lamp1:ON"}}
          2. Action-based:  {"id":"…","param":{"action":"on","duration":5}}

        Format 1 is used by the Spring Boot SendDeviceCommandUseCase.
        Format 2 is the original RPi operator system.
        We try Format 1 first, then fall back to Format 2.
        """
        cmd_id = payload.get("id")
        param = payload.get("param") or {}
        threading.Thread(
            target=self._run_operator_cmd,
            args=(op, cmd_id, param),
            daemon=True,
            name=f"op-{op.name}-{cmd_id}",
        ).start()

    def _run_operator_cmd(self, op, cmd_id, param: dict):
        try:
            # ── Path 1: Backend sends a direct serial command ─────────
            direct_command = None
            if isinstance(param, dict):
                # Backend payload is e.g. {"command": "lamp1:ON"} or {"payload": "lamp1:ON"}
                direct_command = (
                    param.get("command")
                    or param.get("payload")
                    or param.get("value")
                )
            elif isinstance(param, str):
                direct_command = param

            if direct_command and isinstance(direct_command, str):
                direct_command = direct_command.strip()
                if direct_command:
                    logger.info("Operator %s: executing direct command '%s'", op.name, direct_command)
                    response = self._serial.send(direct_command)
                    result = "OK" if response and response.startswith("OK") else f"DONE:{response}"
                    self._mqtt.publish_raw(
                        f"{self._prefix}/{op.name}/resp",
                        json.dumps({"id": cmd_id, "response": result}),
                    )
                    return

            # ── Path 2: Original action-based dispatch ────────────────
            result = self._operators.execute(op, param, self._serial)
        except Exception as exc:
            logger.exception("Operator %s failed", op.name)
            result = f"ERROR:internal:{exc.__class__.__name__}"
        self._mqtt.publish_raw(
            f"{self._prefix}/{op.name}/resp",
            json.dumps({"id": cmd_id, "response": result}),
        )

    def _handle_capture_photo(self, camera_name: str, payload: dict):
        """payload: {"id":...,"param":{"time":<delay_s>}} — capture a photo and publish to capturedPhoto."""
        params = payload.get("param") or {}
        delay_s = float(params.get("time", 0.0))
        self._capture_and_publish(camera_name, delay_s)

    def _handle_ai_scan(self, _payload: dict):
        """payload: {} — capture a photo, run AI analysis and publish to ai/alert."""
        default_camera = self._cameras.default_name()
        if default_camera:
            logger.info("Received ai/scan command. Triggering capture and AI.")
            self._capture_and_publish(default_camera, 0.0)
        else:
            logger.error("Cannot scan: no default camera configured")
            self._mqtt.publish_ai_error("No default camera configured on the Raspberry Pi.")

    # ── Heartbeat ──────────────────────────────────────────────────

    def _heartbeat_loop(self):
        """Periodically publish a heartbeat to indicate the system is online."""
        while self._running:
            try:
                if self._mqtt._connected:
                    self._mqtt.publish_system_status("online")
            except Exception as e:
                logger.debug("Heartbeat error: %s", e)
            
            # Sleep in chunks to allow fast shutdown
            for _ in range(30):
                if not self._running:
                    break
                time.sleep(1)

    # ── Serial push → MQTT sensorData ─────────────────────────

    def _handle_push(self, device: str, payload: str):
        """Receive unsolicited Arduino push, map via registry, publish to sensorData."""
        if not self._publish_sensor_data:
            return
        try:
            for sensor_name, value in self._sensors.parse_push(device, payload):
                self._mqtt.publish_sensor_data(sensor_name, value)
        except Exception as exc:
            logger.error("Failed to process push from device '%s': %s", device, exc)

    # ── Photo helpers ─────────────────────────────────────────

    def _capture_and_publish(self, camera_name: str, delay_s: float):
        try:
            result = self._cameras.capture(camera_name, delay_s=delay_s)
            self._mqtt.publish_captured_photo(
                result.content,
                camera_name=camera_name,
            )
            logger.info("Photo published from %s: %s (%d bytes)", camera_name, result.filename, len(result.content))
            self._analyse_and_publish_ai_alert(result)
        except PhotoCaptureError as exc:
            logger.error("Photo capture failed: %s", exc)
            self._mqtt.publish_ai_error(f"Camera error: {exc}")
        except Exception as exc:
            logger.exception("Unexpected photo capture error: %s", exc)
            self._mqtt.publish_ai_error(f"Internal error: {exc}")

    def _handle_stream_capture(self, path: str, filename: str):
        """Publish and analyse a snapshot captured from the browser stream page."""
        try:
            image_path = Path(path)
            image_bytes = image_path.read_bytes()
            camera_name = self._cameras.default_name()
            result = PhotoCaptureResult(
                path=image_path,
                filename=filename,
                content=image_bytes,
                captured_at=_now_iso(),
            )
            self._mqtt.publish_captured_photo(image_bytes, camera_name=camera_name)
            logger.info(
                "Stream photo published from %s: %s (%d bytes)",
                camera_name,
                filename,
                len(image_bytes),
            )
            self._analyse_and_publish_ai_alert(result)
        except Exception as exc:
            logger.exception("Stream photo publish / AI analysis failed: %s", exc)

    def _analyse_and_publish_ai_alert(self, photo_result):
        """Run local ONNX inference for a captured image and publish its alert."""
        if not self._ai_enabled or self._ai is None:
            logger.debug("AI detection disabled; skipping %s", photo_result.filename)
            return

        try:
            detection = self._ai.classify(photo_result.path)
            self._log_ai_class_probabilities(detection)

            self._mqtt.publish_ai_alert(
                image_bytes=photo_result.content,
                pathology=detection["class_name"],
                confidence=detection["probability"] * 100.0,
            )
            logger.info(
                "AI detection: %s via %s in %.3fs",
                detection["description"],
                detection["selected_expert_name"],
                detection["timings"]["total_seconds"],
            )
        except Exception as exc:
            logger.exception("AI detection failed for %s: %s", photo_result.filename, exc)
            self._mqtt.publish_ai_error(f"AI Model error: {exc}")

    def _log_ai_class_probabilities(self, detection: dict):
        """Log all class probabilities without changing the MQTT AI alert payload."""
        probabilities = detection.get("ranked_class_probabilities") or detection.get("class_probabilities")
        if not probabilities:
            logger.info("AI class probabilities are not available in the inference result")
            return

        # If only the unsorted list is available, sort it for easier reading in journalctl.
        probabilities = sorted(
            probabilities,
            key=lambda item: float(item.get("probability", 0.0)),
            reverse=True,
        )

        logger.info("AI class probabilities (%d classes):", len(probabilities))
        for item in probabilities:
            probability = float(item.get("probability", 0.0))
            percentage = float(item.get("percentage", probability * 100.0))
            logger.info(
                "  [%02d] %s: %.6f (%.3f%%)",
                int(item.get("class_index", -1)),
                str(item.get("class_name", "<unknown>")),
                probability,
                percentage,
            )

    def _handle_config_cycle(self, payload: dict):
        """payload: {"cycleId":..., "plantType":..., "irrigationFrequencyMinutes":..., "irrigationDurationSeconds":...}
        Dynamically rebuild and reload execution cycles based on the published production cycle.
        """
        logger.info("Received cycle configuration update via MQTT: %s", payload)

        status = payload.get("status")
        if status == "INACTIVE" or not payload.get("cycleId"):
            logger.info("No active production cycle. Reloading to static baseline cycles only.")
            self._cycle_manager.reload_cycles(self._static_cycles)
            return

        # Build backend_data map structure expected by config._build_dynamic_cycle
        backend_data = {
            "activeCycle": {
                "irrigationFrequencyMinutes": payload.get("irrigationFrequencyMinutes"),
                "irrigationDurationSeconds": payload.get("irrigationDurationSeconds"),
                "targetTemperatureMin": payload.get("tempMin"),
                "targetTemperatureMax": payload.get("tempMax"),
                "targetHumidity": payload.get("humTarget"),
                "targetLightHours": payload.get("lightHours"),
            }
        }

        # Build dynamic cycles
        dynamic_cycles = _build_dynamic_cycle(
            self._config.get("sensors", []),
            self._config.get("operators", []),
            backend_data
        )

        # Combine static cycles and dynamic cycles, avoiding duplicates
        combined = list(self._static_cycles)
        for dc in dynamic_cycles:
            # Check if cycle name is already present
            if not any(sc.get("name") == dc.get("name") for sc in combined):
                # If it is auto_sensor_publish, skip if we already have a sensor polling cycle
                if dc.get("name") == "auto_sensor_publish":
                    has_sensor_cycle = any(
                        any(c.get("type") == "sensor" for c in sc.get("commands", []))
                        for sc in combined
                    )
                    if has_sensor_cycle:
                        continue
                combined.append(dc)

        logger.info("Dynamic cycle reload: updating CycleManager with %d combined cycles", len(combined))
        self._cycle_manager.reload_cycles(combined)


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
