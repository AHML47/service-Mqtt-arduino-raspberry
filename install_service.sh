#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="arduino-bridge"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}"
VENV_DIR="${PROJECT_DIR}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUN_USER="${RUN_USER:-pi}"
CONFIG_FILE="${CONFIG_FILE:-${PROJECT_DIR}/service/config.yaml}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root: sudo ./install_service.sh"
  exit 1
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "${PYTHON_BIN} is not installed. Install Python 3 first."
  exit 1
fi

if [[ ! -f "${PROJECT_DIR}/requirements.txt" ]]; then
  echo "requirements.txt not found in ${PROJECT_DIR}."
  exit 1
fi

if [[ ! -d "${PROJECT_DIR}/service" ]]; then
  echo "Python package folder 'service' not found in ${PROJECT_DIR}."
  exit 1
fi

echo "[1/5] Installing python venv tooling"
apt-get update -y
apt-get install -y python3-venv

echo "[2/5] Creating/updating virtual environment: ${VENV_DIR}"
"${PYTHON_BIN}" -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"

echo "[3/5] Creating systemd unit: ${SERVICE_FILE}"
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Arduino Bridge Service (Serial <-> MQTT)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${VENV_DIR}/bin/python -m service --config ${CONFIG_FILE}
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
