#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="arduino-bridge"
# Directory where system units are normally written. Can be overridden by env var SYSTEMD_DIR
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
# Allow overriding the final service file directly via SERVICE_FILE env var
SERVICE_FILE="${SERVICE_FILE:-${SYSTEMD_DIR}/${SERVICE_NAME}.service}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-${SCRIPT_DIR}}"
VENV_DIR="${VENV_DIR:-${PROJECT_DIR}/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUN_USER="${RUN_USER:-ahml47}"
CONFIG_FILE="${CONFIG_FILE:-${PROJECT_DIR}/service/config.yaml}"

# Determine whether we can write the configured service file and call systemctl
SERVICE_DIR="$(dirname "${SERVICE_FILE}")"
DO_SYSTEMD=0
if command -v systemctl >/dev/null 2>&1 && [ -w "${SERVICE_DIR}" ]; then
  DO_SYSTEMD=1
fi

# If the configured service path is not writable, fall back to a local file and skip systemctl
if [ "${DO_SYSTEMD}" -ne 1 ]; then
  echo "Note: ${SERVICE_DIR} is not writable or systemctl unavailable."
  echo "Falling back to creating a local unit file and skipping systemctl enable/start."
  SERVICE_FILE="${PROJECT_DIR}/${SERVICE_NAME}.service"
else
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Please run as root: sudo ./install_service.sh"
    exit 1
  fi
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

if [[ "${EUID}" -eq 0 ]]; then
  echo "[1/5] Installing python venv tooling"
  apt-get update -y
  apt-get install -y python3-venv
else
  echo "[1/5] Skipping apt-get; not running as root. Ensure python3-venv is installed if needed."
fi

echo "[2/5] Creating/updating virtual environment: ${VENV_DIR}"
"${PYTHON_BIN}" -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"

[ -n "${SERVICE_FILE}" ] || { echo "No service file path configured"; exit 1; }
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
ExecStart=${VENV_DIR}/bin/python -m service --config ${CONFIG_FILE}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

if [ "${DO_SYSTEMD}" -eq 1 ]; then
  echo "[4/5] Reloading systemd and enabling service"
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"

  echo "[5/5] Starting service"
  systemctl restart "${SERVICE_NAME}"

  echo "Install complete. Check status with: sudo systemctl status ${SERVICE_NAME}"
  echo "Logs: sudo journalctl -u ${SERVICE_NAME} -f"
else
  echo "Skipped systemctl actions because ${SERVICE_DIR} is not writable or systemctl is unavailable."
  echo "Local unit file written to: ${SERVICE_FILE}"
  echo "To install system-wide (requires root), copy the file and enable the service:"
  echo "  sudo cp ${SERVICE_FILE} /etc/systemd/system/${SERVICE_NAME}.service"
  echo "  sudo systemctl daemon-reload"
  echo "  sudo systemctl enable ${SERVICE_NAME}"
  echo "  sudo systemctl restart ${SERVICE_NAME}"
fi
