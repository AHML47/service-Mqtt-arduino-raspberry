"""
Topic router — maps MQTT topic suffixes to handler callables.

Adding a new inbound topic is a single router.register() call in service.py.
Each handler receives a parsed dict payload and returns nothing.

Usage:
    router = TopicRouter()
    router.register("ping", handle_ping, description="Check connection: {}")
    router.register("readSensor", handle_read, description='{"sensor": "<name>"}')

    # MQTT on_message dispatches via:
    router.route("ping", {})
"""

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List

logger = logging.getLogger(__name__)

Handler = Callable[[dict], None]


@dataclass
class Route:
    suffix: str
    handler: Handler
    description: str = ""  # documents the expected payload shape


class TopicRouter:
    def __init__(self):
        self._routes: Dict[str, Route] = {}

    def register(self, suffix: str, handler: Handler, description: str = "") -> None:
        self._routes[suffix] = Route(suffix=suffix, handler=handler, description=description)
        logger.debug("TopicRouter: registered handler for '%s'", suffix)

    def route(self, suffix: str, payload: dict) -> bool:
        """Dispatch payload to the handler for suffix. Returns False if unhandled."""
        route = self._routes.get(suffix)
        if route is None:
            logger.warning("TopicRouter: no handler for suffix '%s'", suffix)
            return False
        try:
            route.handler(payload)
            return True
        except Exception:
            logger.exception("TopicRouter: handler for '%s' raised an exception", suffix)
            return False

    def registered_suffixes(self) -> List[str]:
        return list(self._routes.keys())

    def describe(self) -> List[str]:
        """Return human-readable lines describing each registered route."""
        return [
            f"  {r.suffix}: {r.description}" for r in self._routes.values()
        ]
