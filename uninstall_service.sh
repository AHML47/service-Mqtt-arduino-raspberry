#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="arduino-bridge"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}"
VENV_DIR="${PROJECT_DIR}/.venv"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root: sudo ./uninstall_service.sh"
  exit 1
fi

echo "Stopping and disabling service if present"
if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
  systemctl stop "${SERVICE_NAME}" || true
  systemctl disable "${SERVICE_NAME}" || true
fi

echo "Removing unit file if present"
if [[ -f "${SERVICE_FILE}" ]]; then
  rm -f "${SERVICE_FILE}"
fi

systemctl daemon-reload
systemctl reset-failed

if [[ "${1:-}" == "--purge-venv" ]]; then
  echo "Removing virtual environment: ${VENV_DIR}"
  rm -rf "${VENV_DIR}"
fi

echo "Uninstall complete."
if [[ "${1:-}" != "--purge-venv" ]]; then
  echo "To remove python environment too, run: sudo ./uninstall_service.sh --purge-venv"
fi
