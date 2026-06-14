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

echo "[1/4] Stopping previous service if it exists"
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true

echo "[2/4] Writing service unit: ${SERVICE_FILE}"
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

echo "[3/4] Reloading systemd and enabling service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

echo "[4/4] Starting service"
systemctl restart "${SERVICE_NAME}"

echo "Install complete. Check status with: sudo systemctl status ${SERVICE_NAME}"
echo "Logs: sudo journalctl -u ${SERVICE_NAME} -f"
