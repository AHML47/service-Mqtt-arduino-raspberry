# Plan: First-class Operators with per-operator MQTT topics

## Context

Today, **sensors** are a first-class abstraction in this project: [SensorDef](service/sensor_registry.py#L27-L32) + [SensorRegistry](service/sensor_registry.py#L35-L122), declared in `config.yaml` under `sensors:`, read via `{prefix}/readSensor` → reply on `{prefix}/sensorData`.

**Operators** (DC motor `dc1`, stepper `step1`, future relays/valves/LEDs) have **no abstraction at all**. They are either raw strings sent through the generic `{prefix}/command` topic ([service.py:189-199](service/service.py#L189-L199)) or hardcoded `type: serial` entries in cycles ([cycle_manager.py:117-128](service/cycle_manager.py#L117-L128)). The reply on `{prefix}/commandResponse` only echoes the raw command — a backend with several concurrent requests in flight cannot correlate replies to requests.

### Goal

Lift operators to the same first-class status as sensors:

1. **Per-operator MQTT topics**
   - Inbound:  `{prefix}/{operatorName}/cmd`
   - Outbound: `{prefix}/{operatorName}/resp`
2. **ID-based correlation** — `id` in the command payload is echoed back in the response.
3. **Operator-specific parameters** — each operator has its own `param` schema; the service knows how to translate `param` into the Arduino serial command for that operator.
4. **Still config-driven and well-structured** — adding a new operator is a `config.yaml` entry, not a code change.

### Decisions (from clarification round)

| Topic | Decision |
|---|---|
| `param` shape | **Single object** — `{"id":15,"param":{"action":"on","duration":100}}` |
| Long-running actions (`duration`) | **Service-side timer** — service sends start command → sleeps `duration` → sends stop command → publishes `resp` |
| Existing `{prefix}/command` & `type: serial` cycles | **Keep both, add operators alongside** — zero breakage |
| Arduino firmware | **No changes** — service owns id↔response correlation; the id never crosses serial |

---

## Design

### Wire-level contract

**Inbound** `{prefix}/{operatorName}/cmd`:
```json
{ "id": 15, "param": { "action": "on", "duration": 100 } }
```

**Outbound** `{prefix}/{operatorName}/resp`:
```json
{ "id": 15, "response": "OK" }
```

`response` values: `"OK"` on success, `"ERROR:<reason>"` otherwise (`unknown_action`, `missing_param`, `timeout`, `write_fail`, etc., derived from `serial.send()` return).

### Files

| File | Action | Purpose |
|---|---|---|
| [service/operator_registry.py](service/operator_registry.py) | **NEW** | `OperatorActionDef`, `OperatorDef`, `OperatorRegistry` — mirrors `sensor_registry.py` |
| [service/config.py](service/config.py) | edit | Add `"operators": []` to `DEFAULT_CONFIG` (around [config.py:100](service/config.py#L100)) |
| [service/config.yaml](service/config.yaml) | edit | Declare `motor1` (→ `dc1`) and `stepper1` (→ `step1`) under a new `operators:` block |
| [service/service.py](service/service.py) | edit | Instantiate `OperatorRegistry`, register one route per operator, add `_handle_operator_cmd` |
| [service/cycle_manager.py](service/cycle_manager.py) | edit | Add `type: operator` branch alongside `type: serial` |

### `operator_registry.py` (mirrors `sensor_registry.py`)

```python
@dataclass
class OperatorActionDef:
    name: str                              # e.g. "on"
    command: str                           # template, e.g. "{device}:ON"
    duration_param: Optional[str] = None   # e.g. "duration" — param key holding seconds to wait
    followup: Optional[str] = None         # template sent after the wait, e.g. "{device}:OFF"

@dataclass
class OperatorDef:
    name: str                              # MQTT-facing name, e.g. "motor1"
    arduino_device: str                    # e.g. "dc1"
    actions: Dict[str, OperatorActionDef]

class OperatorRegistry:
    def __init__(self, operators_config: list): ...
    def all(self) -> List[OperatorDef]: ...
    def get(self, name: str) -> Optional[OperatorDef]: ...
    def execute(self, op: OperatorDef, param: dict, serial) -> str:
        """
        1. Look up action = op.actions[param["action"]]            -> ERROR:unknown_action
        2. Build cmd = action.command.format(device=op.arduino_device, **param)
        3. resp = serial.send(cmd)                                  -> propagate "ERR:..." as ERROR:...
        4. If action.duration_param and param[duration_param] > 0 and action.followup:
              time.sleep(param[duration_param])
              followup_cmd = action.followup.format(device=op.arduino_device, **param)
              resp2 = serial.send(followup_cmd)                     -> ERROR:... if not OK
        5. Return "OK" on success.
        """
```

Template placeholders supported: `{device}` and any key from `param` (e.g. `{steps}`, `{speed}`). `str.format(...)` with a defensive `KeyError` → `"ERROR:missing_param:<name>"`.

### `config.yaml` (new section, sensors-style)

```yaml
operators:
  - name: motor1
    arduino_device: dc1
    actions:
      on:
        command: "{device}:ON"
        duration_param: duration   # if param.duration > 0, wait then send followup
        followup: "{device}:OFF"
      off:
        command: "{device}:OFF"

  - name: stepper1
    arduino_device: step1
    actions:
      move:
        command: "{device}:MOVE:{steps}"   # Arduino blocks until done; no service timer
```

### Service wiring ([service.py](service/service.py))

After the sensor registry, around line 47:
```python
self._operators = OperatorRegistry(config.get("operators", []))
```

After the existing `self._router.register(...)` block (~[L91-L115](service/service.py#L91-L115)):
```python
for op in self._operators.all():
    self._router.register(
        f"{op.name}/cmd",
        functools.partial(self._handle_operator_cmd, op),
        description=f'Operator {op.name}: {{"id","param":{{...}}}} -> {op.name}/resp',
    )
```

`registered_suffixes()` already drives MQTT subscription ([mqtt_client.py:136-140](service/mqtt_client.py#L136-L140)), so each `{name}/cmd` becomes a real subscription with no MQTT-layer change.

### Handler — **threaded**, because `duration` can be long

If `_handle_operator_cmd` ran inline, a `duration: 100` command would block the MQTT message loop for 100s, freezing every other inbound topic. We dispatch each command to a daemon thread (same pattern as [`_handle_capture_photo`](service/service.py#L201-L210)):

```python
def _handle_operator_cmd(self, op: OperatorDef, payload: dict):
    cmd_id = payload.get("id")
    param  = payload.get("param") or {}
    threading.Thread(
        target=self._run_operator_cmd,
        args=(op, cmd_id, param),
        daemon=True,
        name=f"op-{op.name}-{cmd_id}",
    ).start()

def _run_operator_cmd(self, op, cmd_id, param):
    try:
        result = self._operators.execute(op, param, self._serial)  # blocks for `duration` if any
    except Exception as exc:
        logger.exception("Operator %s failed", op.name)
        result = f"ERROR:internal:{exc.__class__.__name__}"
    self._mqtt.publish_raw(
        f"{self._prefix}/{op.name}/resp",
        json.dumps({"id": cmd_id, "response": result}),
    )
```

Serial access stays safe because `SerialConnection.send()` already serializes with `self._lock` ([serial_conn.py:50](service/serial_conn.py#L50)): two concurrent operator threads will queue cleanly at the serial layer, no extra locking needed.

### Cycle integration ([cycle_manager.py](service/cycle_manager.py#L117-L146))

Add a third branch to `_execute_command`:
```python
elif cmd_type == "operator":
    op_name = cmd.get("operator", "").strip()
    op = self._operators.get(op_name)
    if not op:
        logger.warning("Cycle: unknown operator '%s'", op_name)
        return
    self._operators.execute(op, cmd.get("param", {}) or {}, self._serial)
```

Requires passing `OperatorRegistry` into `CycleManager.__init__` (one extra param, alongside `sensor_registry`). Old `type: serial` keeps working — the existing `tri_hourly_cultivation` cycle in `config.yaml` does not need to change.

### Subtle bits to get right

- **`functools.partial` capture in the route registration loop** — using `partial` (or a default-arg lambda) avoids the classic late-binding bug where every handler ends up bound to the last `op`.
- **Operator name vs Arduino device name** — these are independent. `motor1` (MQTT name) → `dc1` (Arduino device). Mirrors how sensors have `name` separate from `arduino_device`.
- **Topic suffix is two-segment (`motor1/cmd`)** — `TopicRouter` stores suffixes by exact string, and `_on_message` builds the suffix as `topic[len(prefix)+1:]` ([mqtt_client.py:151](service/mqtt_client.py#L151)), so `"motor1/cmd"` works as a key with no parsing tweaks.
- **`resp` topic is publish-only** — no subscription, no router entry; it's just a `publish_raw` target.
- **Error mapping** — keep it small and consistent (`ERROR:unknown_action`, `ERROR:missing_param:<key>`, `ERROR:timeout`, `ERROR:write_fail`, `ERROR:internal:<ExcName>`). Documented next to `OperatorRegistry.execute`.

---

## Verification

1. **Unit-level** (interactive `python -m service` smoke test is fine; no test harness exists today):
   - Instantiate `OperatorRegistry` with sample config; verify `execute()` of `motor1` with `{"action":"on"}` calls `serial.send("dc1:ON")` and returns `"OK"` in fake-data mode.
   - Verify `{"action":"on","duration":2}` sleeps ~2s and then sends `dc1:OFF`.
   - Verify `{"action":"bogus"}` → `"ERROR:unknown_action"`.
2. **Service startup**: run `python -m service`, confirm logs show new subscriptions:
   - `Subscribed: hydroponic/.../motor1/cmd`
   - `Subscribed: hydroponic/.../stepper1/cmd`
3. **End-to-end happy path** with `mosquitto_pub` / `mosquitto_sub` (or `service/test_publisher.py`):
   - Subscribe to `{prefix}/motor1/resp`.
   - Publish `{"id":15,"param":{"action":"on","duration":3}}` to `{prefix}/motor1/cmd`.
   - Expect `{"id":15,"response":"OK"}` ~3 seconds later.
4. **Concurrent IDs**: publish `id=20` (stepper move) and `id=21` (motor on/off) within 100 ms; both replies must come back with correct ids, no crossover.
5. **MQTT loop stays responsive during duration**: while a `duration:60` motor command is pending, publish to `{prefix}/ping` — `pong` must still come back immediately (proves threading works).
6. **Backward compat**: existing `{prefix}/command` topic still echoes to `commandResponse`; existing `tri_hourly_cultivation` cycle still drives `dc1:ON` / `step1:MOVE:...` via `type: serial`.
