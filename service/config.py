"""
Configuration loader — YAML file with environment variable overrides.
"""

import glob
import os
import logging
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
        "timeout": 1.0,          # read timeout (seconds)
        "response_timeout": 2.0, # max wait for Arduino reply
    },
    "mqtt": {
        "host": "165.232.139.240",
        "port": 1883,
        "username": "backend",
        "password": "backend",
        "client_id": "arduino-bridge",
        "topic_prefix": "arduino",
        "connect_timeout": 30,
        "connect_retries": 10,
        "qos": 1,
        "keepalive": 6000,
    },
    "timers": {
        "persist_file": "timers.json",
        "defaults": [
            {
                "id": "dht_temp",
                "command": "dht1:TEMP",
                "interval_s": 30,
                "enabled": True,
                "publish_to": "arduino/sensor/temperature",
                "parse": "float",
            },
            {
                "id": "dht_hum",
                "command": "dht1:HUM",
                "interval_s": 30,
                "enabled": True,
                "publish_to": "arduino/sensor/humidity",
                "parse": "float",
            },
        ],
    },
    "logging": {
        "level": "DEBUG",
    },
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


def load_config(path: str = "config.yaml") -> dict:
    """
    Load configuration from YAML file (if present), merged on top of defaults.
    Environment variables override individual keys:
      ARDUINO_SERIAL_PORT  →  config["serial"]["port"]
      ARDUINO_MQTT_HOST    →  config["mqtt"]["host"]
      ARDUINO_MQTT_PORT    →  config["mqtt"]["port"]
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

    # Load environment variables from an env file if present (useful for systemd
    # EnvironmentFile-style setups). Check common locations; ignore parse errors.
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

    # Environment overrides
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

    # If a connection string is provided as an environment variable, use the
    # requested topic format: `hydroponic/{connectionString}` as the MQTT prefix.
    # Accept a few common env var names for compatibility.
    conn = (
        env_file_values.get("CONNECTION_STRING")
        or env_file_values.get("ARDUINO_CONNECTION_STRING")
        or env_file_values.get("HYDROPONIC_CONNECTION")
        or os.environ.get("CONNECTION_STRING")
        or os.environ.get("ARDUINO_CONNECTION_STRING")
        or os.environ.get("HYDROPONIC_CONNECTION")
    )
    if conn:
        config["mqtt"]["topic_prefix"] = f"hydroponic/{conn}"

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
