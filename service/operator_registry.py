"""
Operator registry — maps semantic operator names to Arduino device actions.

Operators are actuators (DC motors, steppers, relays, valves, LEDs, ...). Each
operator gets its own MQTT command/response topic pair driven by the service:
    inbound:  {prefix}/{operator_name}/cmd
    outbound: {prefix}/{operator_name}/resp

Command payload (JSON):
    {"id": <int>, "param": {"action": "<name>", "<extra>": ...}}

Response payload (JSON):
    {"id": <int>, "response": "OK" | "ERROR:<reason>"}

Config example (config.yaml → operators):
    operators:
      - name: motor1
        arduino_device: dc1
        actions:
          on:
            command: "{device}:ON"
            duration_param: duration     # if param.duration > 0, wait then send followup
            followup: "{device}:OFF"
          off:
            command: "{device}:OFF"
      - name: stepper1
        arduino_device: step1
        actions:
          move:
            command: "{device}:MOVE:{steps}"
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class OperatorActionDef:
    name: str                              # action name, e.g. "on"
    command: str                           # template, e.g. "{device}:ON"
    duration_param: Optional[str] = None   # param key holding seconds to wait
    followup: Optional[str] = None         # template sent after the wait


@dataclass
class OperatorDef:
    name: str                              # MQTT name, e.g. "motor1"
    arduino_device: str                    # Arduino device id, e.g. "dc1"
    actions: Dict[str, OperatorActionDef] = field(default_factory=dict)


class OperatorRegistry:
    def __init__(self, operators_config: list):
        self._by_name: Dict[str, OperatorDef] = {}

        for cfg in operators_config or []:
            name = (cfg.get("name") or "").strip()
            device = (cfg.get("arduino_device") or "").strip().lower()
            if not name or not device:
                logger.warning("Skipping operator entry missing name or arduino_device: %s", cfg)
                continue

            actions_cfg = cfg.get("actions") or {}
            actions: Dict[str, OperatorActionDef] = {}
            for action_name, action_cfg in actions_cfg.items():
                action_name = str(action_name).strip()
                if isinstance(action_cfg, str):
                    # Shorthand: actions: { on: "{device}:ON" }
                    actions[action_name] = OperatorActionDef(name=action_name, command=action_cfg)
                    continue
                if not isinstance(action_cfg, dict):
                    logger.warning("Operator '%s': action '%s' must be string or dict", name, action_name)
                    continue
                command = (action_cfg.get("command") or "").strip()
                if not command:
                    logger.warning("Operator '%s': action '%s' missing 'command'", name, action_name)
                    continue
                actions[action_name] = OperatorActionDef(
                    name=action_name,
                    command=command,
                    duration_param=action_cfg.get("duration_param") or None,
                    followup=action_cfg.get("followup") or None,
                )

            if not actions:
                logger.warning("Operator '%s' has no valid actions; skipping", name)
                continue

            self._by_name[name] = OperatorDef(name=name, arduino_device=device, actions=actions)

        logger.info("OperatorRegistry: %d operator(s) registered", len(self._by_name))

    # ── Lookups ──────────────────────────────────────────────

    def get(self, name: str) -> Optional[OperatorDef]:
        return self._by_name.get(name)

    def all(self) -> List[OperatorDef]:
        return list(self._by_name.values())

    # ── Execution ────────────────────────────────────────────

    def execute(self, op: OperatorDef, param: dict, serial) -> str:
        """
        Execute the action named in `param["action"]` against the given operator.

        Returns "OK" on success or "ERROR:<reason>" otherwise. Blocks for
        `param[action.duration_param]` seconds when the action has a followup.
        """
        if not isinstance(param, dict):
            return "ERROR:bad_param"

        action_name = str(param.get("action") or "").strip()
        if not action_name:
            return "ERROR:missing_param:action"

        action = op.actions.get(action_name)
        if action is None:
            return "ERROR:unknown_action"

        try:
            command = action.command.format(device=op.arduino_device, **param)
        except KeyError as exc:
            return f"ERROR:missing_param:{exc.args[0]}"
        except (IndexError, ValueError) as exc:
            return f"ERROR:bad_template:{exc}"

        response = serial.send(command)
        err = _err_from_response(response)
        if err is not None:
            return err

        if action.duration_param and action.followup:
            duration = _coerce_duration(param.get(action.duration_param))
            if duration > 0:
                time.sleep(duration)
                try:
                    followup_cmd = action.followup.format(device=op.arduino_device, **param)
                except KeyError as exc:
                    return f"ERROR:missing_param:{exc.args[0]}"
                followup_resp = serial.send(followup_cmd)
                err = _err_from_response(followup_resp)
                if err is not None:
                    return err

        return "OK"


# ── Helpers ──────────────────────────────────────────────────

def _err_from_response(response: str) -> Optional[str]:
    """Map a SerialConnection.send() return into None (OK) or 'ERROR:<reason>'."""
    if not response:
        return "ERROR:empty"
    if response.startswith("OK"):
        return None
    if response.startswith("ERR:"):
        return "ERROR:" + response[4:]
    if response.startswith("ERR"):
        return "ERROR:" + response[3:].lstrip(":")
    return f"ERROR:unexpected:{response}"


def _coerce_duration(raw) -> float:
    if raw is None:
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0
