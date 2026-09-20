#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo: sudo deploy/azure/install.sh [--refresh-env]" >&2
  exit 2
fi

install_user=${SUDO_USER:-shivank}
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
refresh_env=false
if [[ ${1:-} == "--refresh-env" ]]; then
  refresh_env=true
elif [[ $# -gt 0 ]]; then
  echo "Unknown option: $1" >&2
  exit 2
fi

if ! command -v python3.12 >/dev/null; then
  echo "Python 3.12 is required; install python3.12 and python3.12-venv first." >&2
  exit 1
fi
if [[ ! -f "${repo_root}/requirements-lock.txt" ]]; then
  echo "Run this script from a complete checkout containing requirements-lock.txt." >&2
  exit 1
fi

install -d -m 0750 -o root -g "${install_user}" /etc/algotrader
if [[ ! -f /etc/algotrader/fyers.env || ${refresh_env} == true ]]; then
  if [[ ! -f "${repo_root}/.env" ]]; then
    echo "A checkout .env is required only for first install or --refresh-env." >&2
    exit 1
  fi
  install -m 0600 -o "${install_user}" -g "${install_user}" "${repo_root}/.env" /etc/algotrader/fyers.env
fi

venv_python="${repo_root}/.venv312/bin/python"
if [[ -x "${venv_python}" ]] && ! "${venv_python}" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))'; then
  mv "${repo_root}/.venv312" "${repo_root}/.venv-incompatible-$(date +%Y%m%dT%H%M%S)"
fi
if [[ ! -x "${venv_python}" ]]; then
  sudo -u "${install_user}" python3.12 -m venv "${repo_root}/.venv312"
fi
sudo -u "${install_user}" "${venv_python}" -m pip install --upgrade pip
sudo -u "${install_user}" "${venv_python}" -m pip install -r "${repo_root}/requirements-lock.txt"
sudo -u "${install_user}" "${venv_python}" -m pip check

render_unit() {
  local source=$1
  local target=$2
  sed -e "s|/home/shivank/AlgoTrader|${repo_root}|g" \
      -e "s|User=shivank|User=${install_user}|g" \
      -e "s|Group=shivank|Group=${install_user}|g" \
      "${source}" > "${target}.tmp"
  install -m 0644 "${target}.tmp" "${target}"
  rm -f "${target}.tmp"
}

render_unit "${repo_root}/deploy/azure/fyers-dashboard.service" /etc/systemd/system/fyers-dashboard.service
render_unit "${repo_root}/deploy/azure/fyers-backup.service" /etc/systemd/system/fyers-backup.service
render_unit "${repo_root}/deploy/azure/fyers-health.service" /etc/systemd/system/fyers-health.service
install -m 0644 "${repo_root}/deploy/azure/fyers-backup.timer" /etc/systemd/system/fyers-backup.timer
install -m 0644 "${repo_root}/deploy/azure/fyers-health.timer" /etc/systemd/system/fyers-health.timer

systemctl daemon-reload
systemctl enable --now fyers-dashboard.service fyers-backup.timer fyers-health.timer
for _ in $(seq 1 20); do
  if curl --fail --silent --show-error http://127.0.0.1:8000/api/fyers/dashboard >/dev/null; then
    echo "FYERS dashboard verified on VM loopback. Live execution remains disabled."
    exit 0
  fi
  sleep 1
done
systemctl status fyers-dashboard.service --no-pager -l || true
journalctl -u fyers-dashboard.service -n 100 --no-pager || true
exit 1
