"""
Response parser — extracts meaningful values from Arduino responses.

Arduino responses follow the pattern:
    OK:value
    OK:value1:value2
    ERR:message
    BUSY:remaining
"""

import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


def parse_response(response: str) -> Tuple[str, Optional[str]]:
    """
    Split a response into (status, value).

    Examples:
        "OK:24.5"       → ("OK", "24.5")
        "OK:24.5:55.0"  → ("OK", "24.5:55.0")
        "ERR:dht_fail"  → ("ERR", "dht_fail")
        "BUSY:100"      → ("BUSY", "100")
    """
    if ":" in response:
        status, _, value = response.partition(":")
        return status.strip(), value.strip()
    return response.strip(), None


def extract_value(response: str, parse_mode: Optional[str] = None) -> Any:
    """
    Extract and optionally cast the value from a response.

    parse_mode:
        "float"  → float(value)
        "int"    → int(value)
        "raw"    → string as-is
        None     → string as-is
    """
    status, value = parse_response(response)
    if status != "OK" or value is None:
        return None

    if parse_mode == "float":
        try:
            return float(value)
        except ValueError:
            logger.warning("Cannot parse float from: %s", value)
            return None
    elif parse_mode == "int":
        try:
            return int(value)
        except ValueError:
            logger.warning("Cannot parse int from: %s", value)
            return None
    return value


def parse_dht_push(payload: str) -> Optional[dict]:
    """
    Parse a DHT auto-push payload.
    Input:  "OK:24.5:55.0"
    Output: {"temperature": 24.5, "humidity": 55.0}
    """
    status, value = parse_response(payload)
    if status != "OK" or value is None:
        return None
    parts = value.split(":")
    if len(parts) >= 2:
        try:
            return {
                "temperature": float(parts[0].strip()),
                "humidity": float(parts[1].strip()),
            }
        except ValueError:
            return None
    return None
