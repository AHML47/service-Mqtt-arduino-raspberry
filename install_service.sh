#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="arduino-bridge"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SERVICE_FILE="${SERVICE_FILE:-${SYSTEMD_DIR}/${SERVICE_NAME}.service}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-${SCRIPT_DIR}}"
RUN_USER="${RUN_USER:-ahml47}"
APP_BIN="${APP_BIN:-${PROJECT_DIR}/ota-service}"
CONFIG_FILE="${CONFIG_FILE:-${PROJECT_DIR}/service/config.yaml}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root: sudo ./install_service.sh"
  exit 1
fi

if [[ ! -f "${APP_BIN}" ]]; then
  echo "Compiled executable not found: ${APP_BIN}"
  exit 1
fi

if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "Config file not found: ${CONFIG_FILE}"
  exit 1
fi

chmod +x "${APP_BIN}"

echo "[1/5] Stopping previous service if it exists"
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true

echo "[2/5] Freeing the serial port from ModemManager"
# ModemManager probes /dev/ttyACM* (it thinks the Arduino is a modem) and
# steals the port, causing pyserial errors like:
#   "device reports readiness to read but returned no data
#    (device disconnected or multiple access on port?)"
# On a dedicated controller Pi we don't need it at all.
if systemctl list-unit-files | grep -q '^ModemManager\.service'; then
  systemctl disable --now ModemManager 2>/dev/null || true
  echo "  ModemManager disabled"
fi
# Belt-and-suspenders: even if ModemManager gets reinstalled, tell it to
# ignore USB serial (CDC-ACM) devices via a udev rule.
UDEV_RULE="/etc/udev/rules.d/99-arduino-bridge-mm-ignore.rules"
cat > "${UDEV_RULE}" <<'UDEV'
# Keep ModemManager away from the Arduino serial port
ACTION=="add|change", SUBSYSTEM=="tty", ENV{ID_USB_DRIVER}=="cdc_acm", ENV{ID_MM_DEVICE_IGNORE}="1"
UDEV
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger 2>/dev/null || true

echo "[3/5] Writing service unit: ${SERVICE_FILE}"
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Arduino Bridge Service (Serial <-> MQTT)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${APP_BIN} --config ${CONFIG_FILE}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "[4/5] Reloading systemd and enabling service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

echo "[5/5] Starting service"
systemctl restart "${SERVICE_NAME}"

echo "Install complete. Check status with: sudo systemctl status ${SERVICE_NAME}"
echo "Logs: sudo journalctl -u ${SERVICE_NAME} -f"