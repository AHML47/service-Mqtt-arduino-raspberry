# Python MQTT + Serial Communication Testing Plan

## Project Context

This project contains:
- MQTT communication service
- Serial/UART communication
- Raspberry Pi deployment
- OTA executable delivery
- Python-based backend/service

---

# 1. Testing Objectives

The testing strategy must validate:

- Communication reliability
- Fault tolerance
- Packet correctness
- Reconnection behavior
- Parsing robustness
- Hardware interaction
- OTA package stability
- Long-term service stability

---

# 2. Test Categories

| Category | Goal |
|---|---|
| Unit Tests | Validate isolated logic |
| Integration Tests | Validate interaction between modules |
| End-to-End Tests | Validate full workflows |
| Hardware Tests | Validate real device behavior |
| Stress Tests | Validate stability under load |
| Fault Injection Tests | Validate recovery mechanisms |

---

# 3. MQTT Tests

## 3.1 MQTT Connection Tests

### Test Type
- Integration Test

### What To Test
- Successful broker connection
- Wrong broker address
- Authentication failure
- TLS connection
- Reconnection after disconnect
- Keepalive behavior

### Expected Result
- Client reconnects automatically
- Errors handled correctly
- No application crash

---

## 3.2 MQTT Publish Tests

### Test Type
- Unit Test
- Integration Test

### What To Test
- Topic correctness
- QoS behavior
- Retained messages
- Invalid payloads
- JSON serialization

### Expected Result
- Correct message delivery
- Correct payload structure
- Retry logic works

---

## 3.3 MQTT Subscribe Tests

### Test Type
- Integration Test

### What To Test
- Topic subscription
- Wildcard topics
- Callback execution
- Duplicate messages
- Malformed messages

### Expected Result
- Correct handler execution
- Invalid messages safely rejected

---

## 3.4 MQTT Message Processing Tests

### Test Type
- Unit Test

### What To Test
- JSON parsing
- Missing fields
- Invalid schema
- Command routing
- Business logic triggering

### Expected Result
- Correct parsing
- Correct command dispatching
- Proper error handling

---

## 3.5 MQTT Failure Tests

### Test Type
- Fault Injection Test
- Integration Test

### What To Test
- Broker unavailable
- Network interruption
- Disconnect during publish
- Slow broker response

### Expected Result
- Automatic recovery
- No deadlocks
- No infinite blocking

---

# 4. Serial Communication Tests

## 4.1 Serial Port Management Tests

### Test Type
- Integration Test

### What To Test
- Port opening
- Port closing
- Invalid port
- Permission denied
- Port already in use

### Expected Result
- Safe error handling
- Proper cleanup

---

## 4.2 Serial Read Tests

### Test Type
- Integration Test

### What To Test
- Packet reception
- Timeout behavior
- Partial packets
- Corrupted packets
- Buffer overflow

### Expected Result
- Parser remains stable
- Invalid packets rejected safely

---

## 4.3 Serial Write Tests

### Test Type
- Integration Test

### What To Test
- Packet encoding
- Checksum/CRC generation
- Retry behavior
- Binary data handling

### Expected Result
- Correct transmission
- Correct protocol formatting

---

## 4.4 Protocol Parsing Tests

### Test Type
- Unit Test

### What To Test
- Frame delimiters
- Packet fragmentation
- CRC validation
- Invalid frames
- Packet reconstruction

### Expected Result
- Robust parser behavior
- Corrupted packets rejected

---

## 4.5 Hardware Failure Tests

### Test Type
- Hardware Test
- Fault Injection Test

### What To Test
- Device unplugging
- Baudrate mismatch
- Device freeze
- Electrical noise simulation

### Expected Result
- Automatic recovery
- Safe timeout handling

---

# 5. End-to-End Workflow Tests

## 5.1 MQTT → Serial → MQTT Workflow

### Test Type
- End-to-End Test

### Workflow
1. MQTT command received
2. Command parsed
3. Serial packet generated
4. Device response received
5. MQTT response published

### Expected Result
- Full workflow succeeds
- Correct state transitions

---

## 5.2 OTA Update Workflow

### Test Type
- End-to-End Test

### Workflow
1. OTA package downloaded
2. Package validated
3. Executable replaced
4. Service restarted
5. Health check performed

### Expected Result
- Successful update
- Rollback possible on failure

---

# 6. Stress Tests

## 6.1 MQTT Load Tests

### Test Type
- Stress Test

### What To Test
- High message frequency
- Burst traffic
- Large payloads
- Concurrent publishes

### Expected Result
- No memory leaks
- Stable throughput

---

## 6.2 Long Runtime Tests

### Test Type
- Stability Test

### What To Test
- 24h runtime
- Continuous communication
- Repeated reconnects

### Expected Result
- No resource leaks
- Stable memory usage

---

# 7. State Machine Tests

## Test Type
- Unit Test

## What To Test
- DISCONNECTED → CONNECTING
- CONNECTING → CONNECTED
- CONNECTED → ERROR
- ERROR → RECONNECTING

## Expected Result
- Valid transitions only
- No invalid states

---

# 8. Recommended Testing Tools

| Purpose | Tool |
|---|---|
| Unit Testing | pytest |
| Mocking | unittest.mock |
| MQTT Broker | Mosquitto |
| MQTT Client | paho-mqtt |
| Serial Communication | pyserial |
| Virtual Serial Ports | socat |
| Coverage | pytest-cov |
| Linting | ruff |
| Type Checking | mypy |

---

# 9. GitHub Actions Strategy

## GitHub Hosted Runner Tests

Run automatically:
- Unit tests
- MQTT integration tests
- Virtual serial tests
- Linting
- Packaging tests

---

## Self-Hosted Raspberry Pi Tests

Run on real hardware:
- Real UART tests
- GPIO tests
- ARM64 executable validation
- Real device communication

---

# 10. Recommended Project Structure

```text
project/
│
├── app/
│   ├── mqtt/
│   ├── serial/
│   ├── protocol/
│   └── services/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   ├── stress/
│   └── hardware/
│
├── requirements.txt
└── pyproject.toml