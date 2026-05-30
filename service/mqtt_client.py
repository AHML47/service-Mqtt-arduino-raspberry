"""
MQTT client for the hydroponic bridge.

Inbound topics are dispatched through a TopicRouter — no handlers are
hardcoded here. Registering a new topic requires only a router.register()
call in service.py before start() is called.

Publications (outgoing):
    {prefix}/sensorData       → {"<name>": <value>, "time": "<iso>"}
    {prefix}/{camera}/capturedPhoto → JPEG bytes (binary) for named cameras
    {prefix}/commandResponse  → {"command": "...", "response": "...", "time": "..."}
    {prefix}/pong             → {"status": "online", "time": "..."}
"""

import ast
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt

from .topic_router import TopicRouter

logger = logging.getLogger(__name__)


class MQTTClient:
    def __init__(
        self,
        host: str,
        port: int,
        router: TopicRouter,
        username: Optional[str] = None,
        password: Optional[str] = None,
        client_id: str = "hydroponic-bridge",
        topic_prefix: str = "hydroponic/default",
        qos: int = 1,
        keepalive: int = 60,
        connect_timeout: int = 30,
        connect_retries: int = 1,
    ):
        self._host = host
        self._port = port
        self._router = router
        self._prefix = topic_prefix
        self._qos = qos
        self._keepalive = keepalive
        self._connect_timeout = connect_timeout
        self._connect_retries = connect_retries
        self._connected = False

        self._client = mqtt.Client(
            client_id=client_id,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        if username:
            self._client.username_pw_set(username, password)

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    # ── Lifecycle ────────────────────────────────────────────

    def start(self):
        logger.info("Connecting to MQTT broker %s:%d ...", self._host, self._port)
        attempt = 0
        while attempt < max(1, self._connect_retries):
            attempt += 1
            try:
                self._client.connect(self._host, self._port, self._keepalive)
                self._client.loop_start()

                waited = 0.0
                interval = 0.1
                while waited < float(self._connect_timeout):
                    if self._connected:
                        return
                    time.sleep(interval)
                    waited += interval

                logger.warning(
                    "Timeout waiting for MQTT connection (attempt %d/%d)",
                    attempt, self._connect_retries,
                )
                try:
                    self._client.loop_stop()
                    self._client.disconnect()
                except Exception:
                    pass

            except Exception as e:
                logger.exception("MQTT connect attempt %d failed: %s", attempt, e)

        logger.error("Failed to connect to MQTT broker after %d attempt(s)", self._connect_retries)

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()
        logger.info("MQTT disconnected")

    # ── Publish helpers ──────────────────────────────────────

    def publish_sensor_data(self, sensor_name: str, value: object):
        """Publish one sensor reading to {prefix}/sensorData."""
        topic = f"{self._prefix}/sensorData"
        payload = json.dumps({
            sensor_name: value,
            "time": datetime.now(timezone.utc).isoformat(),
        })
        self._client.publish(topic, payload, qos=self._qos)
        logger.debug("PUB %s → %s", topic, payload[:120])

    def publish_captured_photo(self, image_bytes: bytes, camera_name: Optional[str] = None):
        topic = f"{self._prefix}/{camera_name}/capturedPhoto" if camera_name else f"{self._prefix}/capturedPhoto"
        self._client.publish(topic, image_bytes, qos=self._qos)
        logger.debug("PUB %s → <binary %d bytes>", topic, len(image_bytes))

    def publish_raw(self, topic: str, payload: str):
        """Publish a raw string payload to an explicit topic (absolute path)."""
        self._client.publish(topic, payload, qos=self._qos)
        logger.debug("PUB %s → %s", topic, str(payload)[:120])

    # ── MQTT callbacks ───────────────────────────────────────

    def _on_connect(self, client, _userdata, _flags, rc, _properties=None):
        if rc != 0:
            logger.error("MQTT connect failed, rc=%d", rc)
            self._connected = False
            return

        self._connected = True
        logger.info("MQTT connected (prefix: %s)", self._prefix)

        # Subscribe to every suffix the router knows about
        for suffix in self._router.registered_suffixes():
            topic = f"{self._prefix}/{suffix}"
            client.subscribe(topic, qos=self._qos)
            logger.info("Subscribed: %s", topic)

    def _on_message(self, _client, _userdata, msg):
        topic = msg.topic
        raw = msg.payload.decode("utf-8", errors="replace").strip()
        logger.debug("MSG %s → %s", topic, raw[:120])

        prefix_with_slash = self._prefix + "/"
        if not topic.startswith(prefix_with_slash):
            return

        suffix = topic[len(prefix_with_slash):]
        payload = _parse_payload(raw)
        self._router.route(suffix, payload)


def _parse_payload(raw: str) -> dict:
    """Parse JSON payload, falling back to ast.literal_eval for single-quoted strings."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        result = ast.literal_eval(raw)
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    logger.warning("Cannot parse payload as JSON or dict literal: %s", raw[:120])
    return {}
