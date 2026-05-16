"""
Simple MQTT test publisher for the Arduino Bridge.

Run as a module from the repository root:

    python -m service.test_publisher --sensor testSensor --value 42

It will read configuration via `service.config.load_config()` so the
configured `mqtt` settings and `topic_prefix` (including `hydroponic/{conn}`)
are used automatically.
"""
import argparse
import json
import time
import logging
from typing import Any
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from .config import load_config

logger = logging.getLogger(__name__)


def publish_example(sensor: str, value: Any, count: int, interval: float, retain: bool):
    cfg = load_config()
    mcfg = cfg.get("mqtt", {})
    prefix = mcfg.get("topic_prefix", "arduino")
    topic = f"{prefix}/sensorData"

    client = mqtt.Client()
    if mcfg.get("username"):
        client.username_pw_set(mcfg.get("username"), mcfg.get("password"))

    host = mcfg.get("host", "localhost")
    port = int(mcfg.get("port", 1883))

    logger.info("Connecting to MQTT %s:%s (topic=%s)", host, port, topic)
    client.connect(host, port)
    client.loop_start()

    try:
        for i in range(count):
                payload = {sensor: value}
                if "time" not in payload:
                    payload["time"] = datetime.now(timezone.utc).isoformat()
                data = json.dumps(payload)
                client.publish(topic, data, qos=int(mcfg.get("qos", 1)), retain=retain)
                logger.info("Published %s → %s", topic, data)
                if i < count - 1:
                    time.sleep(interval)
    finally:
        client.loop_stop()
        client.disconnect()


def main():
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser(description="Publish test sensorData messages")
    p.add_argument("--sensor", default="testSensor", help="sensor name (JSON key)")
    p.add_argument("--value", default="42", help="value to publish")
    p.add_argument("--count", type=int, default=1, help="number of messages")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between messages")
    p.add_argument("--retain", action="store_true", help="retain messages on broker")
    args = p.parse_args()

    # Try to coerce numeric values where appropriate
    val: Any = args.value
    try:
        if "." in args.value:
            val = float(args.value)
        else:
            val = int(args.value)
    except Exception:
        val = args.value

    publish_example(args.sensor, val, args.count, args.interval, args.retain)


if __name__ == "__main__":
    main()
