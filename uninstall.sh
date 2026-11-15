#!/usr/bin/env bash
# Remove SysAdmin.IA. Pass --purge to also delete config, data and the service user.
set -euo pipefail
APP_NAME="sysadmin-ia"
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }

if command -v systemctl >/dev/null; then
  systemctl disable --now "$APP_NAME" 2>/dev/null || true
  rm -f "/etc/systemd/system/${APP_NAME}.service"
  systemctl daemon-reload || true
fi
rm -f "/etc/sudoers.d/${APP_NAME}" "/etc/sudoers.d/${APP_NAME}-hostname" /usr/local/bin/sysadmin-ia \
      "/usr/local/sbin/${APP_NAME}-hostname"
# The LAN announcement of the web UI (the hostname and avahi itself are left as they are).
if [[ -f "/etc/avahi/services/${APP_NAME}.service" ]]; then
  rm -f "/etc/avahi/services/${APP_NAME}.service"
  systemctl reload avahi-daemon 2>/dev/null || true
fi
rm -rf "/opt/${APP_NAME}"

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "/etc/${APP_NAME}" "/var/lib/${APP_NAME}"
  userdel sysadmin-ia 2>/dev/null || deluser sysadmin-ia 2>/dev/null || true
  echo "Removed application, configuration, data and the 'sysadmin-ia' user."
else
  echo "Removed application. Config (/etc/${APP_NAME}) and data (/var/lib/${APP_NAME}) were kept; use --purge to delete them."
fi
