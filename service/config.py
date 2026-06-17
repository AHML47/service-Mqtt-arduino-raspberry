"""
Configuration loader — YAML file with environment variable overrides
and optional dynamic config fetch from the Spring Boot backend.

Priority (highest to lowest):
  1. Environment variables (CONNECTION_STRING, ZONE_NAME, BACKEND_URL, …)
  2. YAML config file (config.yaml)
  3. Dynamic config fetched from backend at startup (if BACKEND_URL is set)
  4. Built-in defaults

Dynamic config (backend):
  If BACKEND_URL and CONNECTION_STRING are both set, the service will call
  GET {BACKEND_URL}/api/pi-config/{CONNECTION_STRING}
  and merge the returned sensor/operator list into the running config.
  This means you never have to hardcode sensor serial numbers on the Pi.
  New sensors added through the web UI are automatically picked up on the
  next service restart.
"""

import glob
import os
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Common USB vendor IDs seen with Arduino boards and popular USB-serial chips.
ARDUINO_VIDS = {
    "2341",  # Arduino SA
    "2a03",  # Arduino SA
    "1a86",  # CH340/CH341
    "0403",  # FTDI
    "10c4",  # Silicon Labs CP210x
    "067b",  # Prolific
}


# ---------------------------------------------------------------------------
# USB device discovery
# ---------------------------------------------------------------------------
def find_arduino_port() -> Optional[str]:
    # Walk /sys looking for a USB device with a matching vendor id
    for vid_file in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        try:
            vid = Path(vid_file).read_text().strip().lower()
        except OSError:
            continue
        if vid not in ARDUINO_VIDS:
            continue
        base = Path(vid_file).parent
        # tty subdir can be at varying depths
        for tty_dir in base.glob("**/tty/tty*"):
            dev = f"/dev/{tty_dir.name}"
            if Path(dev).exists():
                return dev

    # Fallback: usual suspects
    for candidate in ("/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyACM0", "/dev/ttyACM1"):
        if Path(candidate).exists():
            return candidate
    return None

# ── Defaults ─────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "serial": {
        "port": "auto",
        "baudrate": 9600,
        "timeout": 1.0,
        "response_timeout": 2.0,
    },
    "mqtt": {
        "host": "165.232.139.240",
        "port": 1883,
        "username": "backend",
        "password": "backend",
        "client_id": "hydroponic-bridge",
        # Set at runtime by CONNECTION_STRING + ZONE_NAME env vars
        "topic_prefix": "hydroponic/default",
        # When true, unsolicited Arduino push lines are forwarded to sensorData
        "publish_sensor_data": True,
        "connect_timeout": 30,
        "connect_retries": 10,
        "qos": 1,
        "keepalive": 6000,
    },
    "photo": {
        "output_dir": "photos",
        "resolution": [1280, 720],
        "raw_resolution": [4608, 2592],
        "warmup_s": 2.0,
        "autofocus": True,
    },
    "cameras": [],
    "streaming": {
        "enabled": True,
        "port": 8000,
    },
    "ai": {
        "enabled": True,
        "required": False,
        "models_dir": "onnx_raspberry_pi",
        "threads": 4,
    },
    "logging": {
        "level": "DEBUG",
    },
    # Empty by default — populated dynamically from backend or config.yaml
    "sensors": [],
    "cycles": [],
    "operators": [],
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    merged = base.copy()
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


# ---------------------------------------------------------------------------
# Dynamic config fetch from Spring Boot backend
# ---------------------------------------------------------------------------
def _fetch_backend_config(backend_url: str, connection_string: str, retries: int = 3) -> Optional[dict]:
    """
    Call GET {backend_url}/api/pi-config/{connection_string} and return
    the parsed JSON, or None on failure.

    Retries up to `retries` times with 5-second delays.
    """
    url = f"{backend_url.rstrip('/')}/api/pi-config/{connection_string}"
    logger.info("Fetching Pi config from backend: %s", url)

    for attempt in range(1, retries + 1):
        try:
            import urllib.request
            import json as _json
            with urllib.request.urlopen(url, timeout=10) as resp:
                if resp.status == 200:
                    data = _json.loads(resp.read().decode("utf-8"))
                    logger.info(
                        "Backend config fetched: connectionString=%s, zones=%d",
                        data.get("connectionString"),
                        len(data.get("zones", [])),
                    )
                    return data
                elif resp.status == 404:
                    logger.warning(
                        "Backend returned 404 for connectionString=%s — "
                        "make sure this greenhouse exists in the database.",
                        connection_string,
                    )
                    return None
        except Exception as exc:
            logger.warning(
                "Backend config fetch attempt %d/%d failed: %s",
                attempt, retries, exc,
            )
            if attempt < retries:
                time.sleep(5)

    logger.error(
        "Could not fetch Pi config from backend after %d attempt(s). "
        "Running with local config only.",
        retries,
    )
    return None


def _build_sensors_from_backend(backend_data: dict, zone_name: Optional[str] = None) -> list:
    """
    Convert the backend pi-config response into a list of sensor defs
    compatible with SensorRegistry.

    Each zone can have sensors. We publish all of them under their
    serialNumber (which is the MQTT sensor name).

    The arduino_device and command are set to the serialNumber itself —
    the actual Arduino command is whatever the Arduino firmware requires
    (e.g. "SN_xxxx:READ"). If different, override in config.yaml.
    """
    sensors = []
    for zone in backend_data.get("zones", []):
        if zone_name and zone.get("name", "").lower() != zone_name.lower():
            continue
        for s in zone.get("sensors", []):
            sn = s.get("serialNumber", "").strip()
            if not sn:
                continue
            sensors.append({
                "name": sn,
                # The arduino_device maps to the actual arduino component id.
                # By convention we use the serialNumber as device id; the Arduino
                # firmware uses the same id to respond. Override in config.yaml if needed.
                "arduino_device": sn,
                "field_index": 0,
                "command": f"{sn}:READ",
            })
            logger.debug("Registered sensor from backend: %s", sn)
    return sensors


def _build_operators_from_backend(backend_data: dict, zone_name: Optional[str] = None) -> list:
    """
    Convert the backend pi-config response into a list of operator defs
    compatible with OperatorRegistry.
    """
    operators = []
    for zone in backend_data.get("zones", []):
        if zone_name and zone.get("name", "").lower() != zone_name.lower():
            continue
        for op in zone.get("operators", []):
            sn = op.get("serialNumber", "").strip()
            if not sn:
                continue
            operators.append({
                "name": sn,
                "arduino_device": sn,
                "actions": {
                    "on": {
                        "command": "{device}:ON",
                        "duration_param": "duration",
                        "followup": "{device}:OFF",
                    },
                    "off": {
                        "command": "{device}:OFF",
                    },
                },
            })
            logger.debug("Registered operator from backend: %s", sn)
    return operators


def _build_dynamic_cycle(sensors: list, operators: list, backend_data: dict) -> list:
    """
    Build local execution cycles based on the backend ProductionCycle
    parameters. This guarantees that automation runs locally on the Pi
    even if the network goes down.
    """
    cycles = []
    
    # 1. Base cycle: Always read sensors every 60 seconds
    if sensors:
        cycles.append({
            "name": "auto_sensor_publish",
            "interval": 60,
            "run_on_start": True,
            "commands": [{"type": "sensor"}]
        })
        
    # 2. Automation cycles: if there is an active ProductionCycle
    active_cycle = backend_data.get("activeCycle")
    if active_cycle and operators:
        # Build Irrigation Cycle
        freq_mins = active_cycle.get("irrigationFrequencyMinutes")
        dur_secs = active_cycle.get("irrigationDurationSeconds")
        
        if freq_mins and dur_secs:
            # Try to find a pump operator
            pump = next((op for op in operators if "pump" in op["name"].lower() or "eau" in op["name"].lower() or "sn_" in op["name"].lower()), None)
            if pump:
                cycles.append({
                    "name": "auto_irrigation",
                    "interval": freq_mins * 60,
                    "run_on_start": True,
                    "commands": [
                        {
                            "type": "serial",
                            "command": f"{pump['arduino_device']}:ON",
                            "publish_response": False,
                            "delay_after": dur_secs
                        },
                        {
                            "type": "serial",
                            "command": f"{pump['arduino_device']}:OFF",
                            "publish_response": False
                        }
                    ]
                })
                logger.info("Generated auto_irrigation cycle: %ds every %dm", dur_secs, freq_mins)

    return cycles

# ---------------------------------------------------------------------------
# Main loader
# ---------------------------------------------------------------------------
def load_config(path: str = "config.yaml") -> dict:
    """
    Load configuration from YAML file merged on top of defaults.
    Then optionally fetch sensor/operator list from the Spring Boot backend.

    Environment variable overrides:
      ARDUINO_SERIAL_PORT  → config["serial"]["port"]
      ARDUINO_MQTT_HOST    → config["mqtt"]["host"]
      ARDUINO_MQTT_PORT    → config["mqtt"]["port"]
      CONNECTION_STRING    → MQTT prefix: hydroponic/{CONNECTION_STRING}/{ZONE_NAME}
      ZONE_NAME            → Optional zone segment in topic prefix
      BACKEND_URL          → If set, fetch dynamic config from backend REST API
                             e.g. http://192.168.1.10:8082
    """
    config = DEFAULT_CONFIG.copy()

    # Try YAML file
    if os.path.isfile(path):
        try:
            import yaml
            with open(path) as f:
                file_cfg = yaml.safe_load(f) or {}
            config = _deep_merge(config, file_cfg)
            logger.info("Loaded config from %s", path)
        except ImportError:
            logger.warning("PyYAML not installed — using defaults only")
        except Exception as e:
            logger.warning("Failed to read %s: %s — using defaults", path, e)

    # Load environment variables from /etc/environment if present
    env_file_values = {}
    for env_file in ("/etc/environment",):
        if os.path.isfile(env_file):
            try:
                with open(env_file) as ef:
                    for line in ef:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            k, v = line.split("=", 1)
                            env_file_values[k.strip()] = v.strip().strip("'\"")
                logger.info("Loaded environment variables from %s", env_file)
                break
            except Exception as e:
                logger.warning("Failed to load env file %s: %s", env_file, e)

    # Apply simple environment overrides
    env_map = {
        "ARDUINO_SERIAL_PORT":  ("serial", "port"),
        "ARDUINO_SERIAL_BAUD":  ("serial", "baudrate", int),
        "ARDUINO_MQTT_HOST":    ("mqtt", "host"),
        "ARDUINO_MQTT_PORT":    ("mqtt", "port", int),
        "ARDUINO_MQTT_USER":    ("mqtt", "username"),
        "ARDUINO_MQTT_PASS":    ("mqtt", "password"),
        "ARDUINO_LOG_LEVEL":    ("logging", "level"),
    }
    for env_key, spec in env_map.items():
        val = env_file_values.get(env_key, os.environ.get(env_key))
        if val is not None:
            section, key = spec[0], spec[1]
            cast = spec[2] if len(spec) > 2 else str
            config[section][key] = cast(val)

    # Resolve CONNECTION_STRING + ZONE_NAME → topic_prefix
    conn = (
        env_file_values.get("CONNECTION_STRING")
        or env_file_values.get("ARDUINO_CONNECTION_STRING")
        or env_file_values.get("HYDROPONIC_CONNECTION")
        or os.environ.get("CONNECTION_STRING")
        or os.environ.get("ARDUINO_CONNECTION_STRING")
        or os.environ.get("HYDROPONIC_CONNECTION")
    )
    zone = (
        env_file_values.get("ZONE_NAME")
        or os.environ.get("ZONE_NAME")
    )
    if conn and zone:
        config["mqtt"]["topic_prefix"] = f"hydroponic/{conn}/{zone}"
    elif conn:
        config["mqtt"]["topic_prefix"] = f"hydroponic/{conn}"

    # ── Dynamic config from Spring Boot backend ─────────────────
    backend_url = (
        env_file_values.get("BACKEND_URL")
        or os.environ.get("BACKEND_URL")
    )
    if backend_url and conn:
        backend_data = _fetch_backend_config(backend_url, conn)
        if backend_data:
            # If a zone is configured, look up its specific topicPrefix in the zones list
            authoritative_prefix = None
            if zone:
                for z in backend_data.get("zones", []):
                    if z.get("name", "").lower() == zone.lower():
                        authoritative_prefix = z.get("topicPrefix")
                        break
            if not authoritative_prefix:
                authoritative_prefix = backend_data.get("topicPrefix")

            if authoritative_prefix:
                config["mqtt"]["topic_prefix"] = authoritative_prefix
                logger.info("Topic prefix set from backend: %s", authoritative_prefix)

            # Only populate sensors/operators from backend if not already
            # configured in config.yaml (config.yaml takes priority)
            if not config.get("sensors"):
                backend_sensors = _build_sensors_from_backend(backend_data, zone)
                if backend_sensors:
                    config["sensors"] = backend_sensors
                    logger.info(
                        "Loaded %d sensor(s) from backend config", len(backend_sensors)
                    )

            if not config.get("operators"):
                backend_operators = _build_operators_from_backend(backend_data, zone)
                if backend_operators:
                    config["operators"] = backend_operators
                    logger.info(
                        "Loaded %d operator(s) from backend config", len(backend_operators)
                    )

            # Generate and merge dynamic execution cycles from active backend cycle
            auto_cycles = _build_dynamic_cycle(
                config.get("sensors", []),
                config.get("operators", []),
                backend_data
            )
            if auto_cycles:
                if "cycles" not in config or not config["cycles"]:
                    config["cycles"] = auto_cycles
                else:
                    combined = list(config["cycles"])
                    for dc in auto_cycles:
                        if not any(sc.get("name") == dc.get("name") for sc in combined):
                            if dc.get("name") == "auto_sensor_publish":
                                has_sensor_cycle = any(
                                    any(c.get("type") == "sensor" for c in sc.get("commands", []))
                                    for sc in combined
                                )
                                if has_sensor_cycle:
                                    continue
                            combined.append(dc)
                    config["cycles"] = combined
                logger.info("Merged %d dynamic local cycle(s) from backend config", len(auto_cycles))
        else:
            logger.warning(
                "Backend config unavailable — make sure the greenhouse with "
                "connectionString=%s exists, and that BACKEND_URL=%s is reachable.",
                conn, backend_url,
            )
    elif conn and not backend_url:
        logger.info(
            "BACKEND_URL not set — skipping dynamic config fetch. "
            "Set BACKEND_URL=http://<server>:8082 on the Pi to enable auto-discovery."
        )

    # Auto-detect the Arduino serial port when not explicitly configured.
    port = config.get("serial", {}).get("port")
    if not port or str(port).lower() == "auto":
        detected = find_arduino_port()
        if detected:
            config["serial"]["port"] = detected
            logger.info("Auto-detected Arduino serial port: %s", detected)
        else:
            logger.warning(
                "Could not auto-detect Arduino serial port; keeping configured value: %s",
                port,
            )

    return config
