"""
MQTT Client — connects to the broker, subscribes to control topics,
and publishes sensor data / responses.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

# Callback types
CommandCallback = Callable[[str], None]             # raw Arduino command string
TimerSetCallback = Callable[[dict], None]            # timer JSON payload
TimerDeleteCallback = Callable[[str], None]          # timer id
TimerListCallback = Callable[[], None]               # no args
CaptureOrderCallback = Callable[[dict], None]        # capture order JSON payload


class MQTTClient:
    """
    Thin wrapper around paho-mqtt tailored to the Arduino bridge topics.

    Subscriptions (relative to prefix):
        {prefix}/cmd          → forward to Arduino
        {prefix}/captureorder → schedule a photo capture
        {prefix}/timer/set    → create/update timer
        {prefix}/timer/delete → delete timer
        {prefix}/timer/list   → request timer list

    Publications:
        {prefix}/resp         → command responses
        {prefix}/push/{dev}   → unsolicited Arduino pushes
        {prefix}/sensor/*     → parsed sensor values
        {prefix}/timer/status → timer list dump
        {prefix}/photo        → captured JPEG bytes
        {prefix}/status       → online / offline (LWT)
    """

    def __init__(
        self,
        host: str = "165.232.139.240",
        port: int = 1885,
        username: Optional[str] = None,
        password: Optional[str] = None,
        client_id: str = "arduino-bridge",
        topic_prefix: str = "arduino",
        qos: int = 1,
        keepalive: int = 60,
        connect_timeout: int = 30,
        connect_retries: int = 1,
    ):
        self._host = host
        self._port = port
        self._prefix = topic_prefix
        self._qos = qos
        self._keepalive = keepalive
        self._connect_timeout = connect_timeout
        self._connect_retries = connect_retries

        self._client = mqtt.Client(
            client_id=client_id,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        if username:
            self._client.username_pw_set(username, password)

        # Last Will — broker publishes this if we drop unexpectedly
        self._client.will_set(
            f"{self._prefix}/status",
            payload=json.dumps({"state": "offline"}),
            qos=1,
            retain=True,
        )

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        # Connection state
        self._connected = False

        # External callbacks — set by the service
        self.on_command: Optional[CommandCallback] = None
        self.on_timer_set: Optional[TimerSetCallback] = None
        self.on_timer_delete: Optional[TimerDeleteCallback] = None
        self.on_timer_list: Optional[TimerListCallback] = None
        self.on_capture_order: Optional[CaptureOrderCallback] = None

    # ── Lifecycle ────────────────────────────────────────────

    def start(self):
        logger.info("Connecting to MQTT broker %s:%d ...", self._host, self._port)

        attempt = 0
        while attempt < max(1, self._connect_retries):
            attempt += 1
            try:
                self._client.connect(self._host, self._port, self._keepalive)
                self._client.loop_start()

                # Wait for on_connect to set the flag
                waited = 0.0
                interval = 0.1
                timeout = float(self._connect_timeout)
                while waited < timeout:
                    if self._connected:
                        return
                    time.sleep(interval)
                    waited += interval

                logger.warning(
                    "Timeout waiting for MQTT connection (%.1fs) on attempt %d/%d",
                    timeout,
                    attempt,
                    self._connect_retries,
                )
                # If not connected, stop loop and retry (if retries left)
                try:
                    self._client.loop_stop()
                    self._client.disconnect()
                except Exception:
                    pass

            except Exception as e:
                logger.exception("MQTT connect attempt %d failed: %s", attempt, e)

        logger.error("Failed to connect to MQTT broker after %d attempts", self._connect_retries)

    def stop(self):
        self.publish("status", {"state": "offline"}, retain=True)
        self._client.loop_stop()
        self._client.disconnect()
        logger.info("MQTT disconnected")

    # ── Publish helpers ──────────────────────────────────────

    def publish(self, subtopic: str, payload, retain: bool = False):
        """
        Publish to {prefix}/{subtopic}.
        payload can be a dict (→ JSON), bytes, or a string.
        """
        topic = f"{self._prefix}/{subtopic}"
        if isinstance(payload, dict):
            # Copy to avoid mutating caller dict and ensure `time` is present
            p = payload.copy()
            if "time" not in p:
                p["time"] = datetime.now(timezone.utc).isoformat()
            data = json.dumps(p)
        elif isinstance(payload, (bytes, bytearray, memoryview)):
            data = bytes(payload)
        else:
            data = str(payload)
        self._client.publish(topic, data, qos=self._qos, retain=retain)
        self._log_publish(topic, data)

    def publish_raw(self, topic: str, payload, retain: bool = False):
        """Publish to an absolute topic (for custom timer publish_to)."""
        if isinstance(payload, dict):
            p = payload.copy()
            if "time" not in p:
                p["time"] = datetime.now(timezone.utc).isoformat()
            data = json.dumps(p)
        elif isinstance(payload, (bytes, bytearray, memoryview)):
            data = bytes(payload)
        else:
            data = str(payload)
        self._client.publish(topic, data, qos=self._qos, retain=retain)
        self._log_publish(topic, data)

    def _log_publish(self, topic: str, data):
        if isinstance(data, (bytes, bytearray)):
            logger.debug("PUB %s → <binary %d bytes>", topic, len(data))
        else:
            logger.debug("PUB %s → %s", topic, str(data)[:120])

    # ── MQTT callbacks ───────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            logger.info("MQTT connected")
            self._connected = True
            # Subscribe to control topics
            subs = [
                f"{self._prefix}/cmd",
                f"{self._prefix}/captureorder",
                f"{self._prefix}/timer/set",
                f"{self._prefix}/timer/delete",
                f"{self._prefix}/timer/list",
            ]
            for topic in subs:
                client.subscribe(topic, qos=self._qos)
                logger.debug("Subscribed: %s", topic)

            self.publish("status", {"state": "online"}, retain=True)
        else:
            logger.error("MQTT connect failed, rc=%d", rc)
            self._connected = False

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        logger.debug("MSG %s → %s", topic, payload[:120])

        suffix = topic[len(self._prefix) + 1:]  # strip "arduino/"

        try:
            if suffix == "cmd":
                if self.on_command:
                    self.on_command(payload)

            elif suffix == "captureorder":
                if self.on_capture_order:
                    data = json.loads(payload)
                    self.on_capture_order(data)

            elif suffix == "timer/set":
                if self.on_timer_set:
                    data = json.loads(payload)
                    self.on_timer_set(data)

            elif suffix == "timer/delete":
                if self.on_timer_delete:
                    data = json.loads(payload)
                    self.on_timer_delete(data.get("id", payload))

            elif suffix == "timer/list":
                if self.on_timer_list:
                    self.on_timer_list()

        except json.JSONDecodeError as e:
            logger.error("Bad JSON on %s: %s", topic, e)
        except Exception as e:
            logger.exception("Error handling message on %s: %s", topic, e)
