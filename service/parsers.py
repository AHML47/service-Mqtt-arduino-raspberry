"""
Response parser — extracts values from Arduino serial responses.

Response format:  OK:value  |  OK:v1:v2  |  ERR:message
"""

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def parse_response(response: str) -> Tuple[str, Optional[str]]:
    if ":" in response:
        status, _, value = response.partition(":")
        return status.strip(), value.strip()
    return response.strip(), None


def parse_numeric(value_str: str) -> float:
    return float(value_str.strip())
