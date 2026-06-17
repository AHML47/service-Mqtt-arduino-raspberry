"""
Sensor registry — maps semantic sensor names to Arduino device/field definitions.

Config example (config.yaml → sensors):
    sensors:
      - name: temperature
        arduino_device: dht1
        field_index: 0          # index in the colon-separated OK response
        command: "dht1:READ"
      - name: humidity
        arduino_device: dht1
        field_index: 1
        command: "dht1:READ"
      - name: water_level
        arduino_device: level1
        field_index: 0
        command: "level1:READ"
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class SensorDef:
    name: str            # semantic name shown on MQTT, e.g. "temperature"
    arduino_device: str  # Arduino component id (lowercase), e.g. "dht1"
    field_index: int     # index into "OK:v0:v1:v2" response fields
    command: str         # full command string sent to Arduino, e.g. "dht1:READ"


class SensorRegistry:
    def __init__(self, sensors_config: list):
        self._by_name: Dict[str, SensorDef] = {}
        self._by_device: Dict[str, List[SensorDef]] = {}

        for cfg in sensors_config:
            name = cfg.get("name", "").strip()
            device = cfg.get("arduino_device", "").strip().lower()
            if not name or not device:
                logger.warning("Skipping sensor entry missing name or arduino_device: %s", cfg)
                continue
            sensor = SensorDef(
                name=name,
                arduino_device=device,
                field_index=int(cfg.get("field_index", 0)),
                command=cfg.get("command", f"{device}:READ"),
            )
            self._by_name[name] = sensor
            self._by_device.setdefault(device, []).append(sensor)

        logger.info("SensorRegistry: %d sensor(s) registered", len(self._by_name))

    # ── Lookups ──────────────────────────────────────────────

    def get(self, name: str) -> Optional[SensorDef]:
        return self._by_name.get(name)

    def all(self) -> List[SensorDef]:
        return list(self._by_name.values())

    def by_device(self, device: str) -> List[SensorDef]:
        return self._by_device.get(device.lower(), [])

    # ── Parsing ──────────────────────────────────────────────

    def parse_push(self, device: str, payload: str) -> List[Tuple[str, object]]:
        """
        Parse an unsolicited Arduino push line for a device.

        payload format: "OK:v0:v1" or bare "v0:v1"
        Returns: [(sensor_name, value), ...]

        Falls back to (device_name, raw_value) when the device has no mapping.
        """
        raw = payload[3:] if payload.startswith("OK:") else payload
        parts = raw.split(":")

        sensors = self.by_device(device)
        if not sensors:
            # Unmapped device — publish under the device name itself
            if not parts or not parts[0]:
                logger.debug("Empty payload for unmapped device '%s'", device)
                return []
            try:
                value: object = float(parts[0])
            except (ValueError, IndexError):
                value = parts[0] if parts else payload
            return [(device, value)]

        results = []
        for sensor in sensors:
            if sensor.field_index >= len(parts):
                logger.warning(
                    "Field index %d out of range for sensor '%s' "
                    "(payload has %d part(s): '%s')",
                    sensor.field_index, sensor.name, len(parts), payload,
                )
                continue
            try:
                results.append((sensor.name, float(parts[sensor.field_index])))
            except ValueError:
                logger.warning(
                    "Cannot parse field %d for sensor '%s' from payload '%s'",
                    sensor.field_index, sensor.name, payload,
                )
        return results

    # ── Direct read ──────────────────────────────────────────

    def read_sensor(self, name: str, serial) -> Optional[float]:
        """Send the read command for a sensor and parse the Arduino response."""
        sensor = self.get(name)
        if not sensor:
            logger.warning("SensorRegistry: unknown sensor '%s'", name)
            return None

        response = serial.send(sensor.command)
        if not response.startswith("OK:"):
            logger.warning("Sensor '%s' read failed: %s", name, response)
            return None

        parts = response[3:].split(":")
        if sensor.field_index >= len(parts):
            logger.warning(
                "Sensor '%s' field_index %d out of range (response has %d part(s): '%s')",
                name, sensor.field_index, len(parts), response,
            )
            return None
        try:
            return float(parts[sensor.field_index])
        except ValueError:
            logger.warning("Cannot parse value for sensor '%s': %s", name, response)
            return None
