# Arduino Bridge Service

A Raspberry Pi service that bridges an Arduino (via serial) to MQTT,
with configurable scheduled timers for periodic commands.

## Architecture

```
MQTT Broker
    ↕ (paho-mqtt)
┌─────────────────────────────┐
│       ArduinoBridgeService  │
│  ┌───────────┐ ┌──────────┐│
│  │ MQTTClient│ │SerialConn││
│  └─────┬─────┘ └────┬─────┘│
│        │             │      │
│  ┌─────┴─────────────┴────┐ │
│  │     CommandRouter      │ │
│  └─────────┬──────────────┘ │
│  ┌─────────┴──────────────┐ │
│  │     TimerManager       │ │
│  └────────────────────────┘ │
└─────────────────────────────┘
    ↕ (serial)
  Arduino Uno
```

## MQTT Topic Map

| Topic                          | Dir | Payload                          | Description                    |
|--------------------------------|-----|----------------------------------|--------------------------------|
| `arduino/cmd`                  | IN  | `device:action:param1:param2`    | Send raw command to Arduino    |
| `arduino/resp`                 | OUT | `{"cmd":"...","resp":"...","ts"}` | Command response               |
| `arduino/push/{device}`        | OUT | `{"value":"...","ts":"..."}`     | Unsolicited Arduino push data  |
| `arduino/sensor/temperature`   | OUT | `{"value":24.5,"ts":"..."}`      | Parsed temperature reading     |
| `arduino/sensor/humidity`      | OUT | `{"value":55.0,"ts":"..."}`      | Parsed humidity reading        |
| `arduino/timer/set`            | IN  | JSON (see below)                 | Create / update a timer        |
| `arduino/timer/delete`         | IN  | `{"id":"timer_id"}`              | Delete a timer                 |
| `arduino/timer/list`           | IN  | _(empty)_                        | Request timer list              |
| `arduino/timer/status`         | OUT | JSON list of active timers       | Published timer list            |
| `arduino/status`               | OUT | `{"state":"online/offline"}`     | Service lifecycle               |

### Timer set payload

```json
{
  "id":          "dht_read",
  "command":     "dht1:READ",
  "interval_s":  30,
  "enabled":     true,
  "publish_to":  "arduino/sensor/raw"
}
```

## Installation

```bash
# On your Raspberry Pi
sudo apt install -y python3 python3-pip python3-venv

# Copy the project
scp -r arduino-bridge/ pi@<ip>:~/arduino-bridge/

# Go to project directory
cd ~/arduino-bridge

# Edit config
nano service/config.yaml

# Install as systemd service (creates venv, installs deps, enables + starts service)
chmod +x install_service.sh uninstall_service.sh
sudo ./install_service.sh

# Check status and logs
sudo systemctl status arduino-bridge
sudo journalctl -u arduino-bridge -f

# Uninstall service only
sudo ./uninstall_service.sh

# Uninstall service and remove virtual environment
sudo ./uninstall_service.sh --purge-venv
```
