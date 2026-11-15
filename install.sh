#!/usr/bin/env bash
# SysAdmin.IA – installer / upgrader.
# Works on Debian/Ubuntu, RHEL/Fedora/Rocky/Alma, openSUSE, Arch (systemd). Alpine is
# supported without the service unit (run it manually or via OpenRC).
#
# Usage: sudo ./install.sh [options]
#   --api-key KEY        Anthropic API key (optional; can be added later in the web UI)
#   --port PORT          HTTPS port (default 8765)
#   --http-port PORT     plain-HTTP port served alongside HTTPS (default 8080)
#   --no-http            serve HTTPS only
#   --admin NAME         name of the first admin user (default: admin)
#   --full-access        give the service user passwordless sudo (Claude can change the system)
#   --no-full-access     don't (Claude can only act as the unprivileged service user)
#   --no-tls             serve plain HTTP (only behind a TLS reverse proxy / on localhost!)
#   --bind ADDR          listen address (default 0.0.0.0)
#   -y, --yes            non-interactive; accept defaults
set -euo pipefail

APP_NAME="sysadmin-ia"
SVC_USER="sysadmin-ia"
PREFIX="/opt/${APP_NAME}"
CONF_DIR="/etc/${APP_NAME}"
DATA_DIR="/var/lib/${APP_NAME}"
ENV_FILE="${CONF_DIR}/sysadmin-ia.env"
UNIT_FILE="/etc/systemd/system/${APP_NAME}.service"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

API_KEY="${ANTHROPIC_API_KEY:-}"
PORT="8765"
HTTP_PORT="8080"
HTTP_PORT_SET=0
BIND="0.0.0.0"
ADMIN_NAME="admin"
FULL_ACCESS=""
TLS=1
ASSUME_YES=0

c_info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
c_warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()    { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-key) API_KEY="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --http-port) HTTP_PORT="$2"; HTTP_PORT_SET=1; shift 2 ;;
    --no-http) HTTP_PORT="0"; HTTP_PORT_SET=1; shift ;;
    --bind) BIND="$2"; shift 2 ;;
    --admin) ADMIN_NAME="$2"; shift 2 ;;
    --full-access) FULL_ACCESS=1; shift ;;
    --no-full-access) FULL_ACCESS=0; shift ;;
    --no-tls) TLS=0; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "Run as root (sudo ./install.sh)."
[[ -f "${SRC_DIR}/app/main.py" ]] || die "Run this script from the project directory."
INTERACTIVE=0; [[ -t 0 && $ASSUME_YES -eq 0 ]] && INTERACTIVE=1

ask_yn() {  # ask_yn "question" default(y/n)
  local q="$1" def="$2" ans
  if [[ $INTERACTIVE -eq 0 ]]; then [[ "$def" == y ]]; return; fi
  read -r -p "$q [$( [[ $def == y ]] && echo Y/n || echo y/N )] " ans
  ans="${ans:-$def}"; [[ "$ans" =~ ^[Yy] ]]
}

# ------------------------------------------------------------------ migrate old install
# Earlier versions were called "Linux Server Control" (service linux-server-control, user lsc).
OLD_NAME="linux-server-control"
if [[ ( -d "/etc/${OLD_NAME}" || -d "/opt/${OLD_NAME}" ) && ! -d "$CONF_DIR" ]]; then
  c_info "Migrating existing '${OLD_NAME}' installation to '${APP_NAME}'"
  if command -v systemctl >/dev/null; then
    systemctl disable --now "${OLD_NAME}" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/${OLD_NAME}.service"
    systemctl daemon-reload || true
  fi
  if [[ -d "/opt/${OLD_NAME}" && ! -e "$PREFIX" ]]; then mv "/opt/${OLD_NAME}" "$PREFIX"; fi
  rm -rf "$PREFIX/.venv"   # venv scripts contain absolute paths; it is rebuilt below
  if [[ -d "/etc/${OLD_NAME}" ]]; then
    mv "/etc/${OLD_NAME}" "$CONF_DIR"
    if [[ -f "$CONF_DIR/lsc.env" ]]; then mv "$CONF_DIR/lsc.env" "$ENV_FILE"; fi
    sed -i -e 's/^LSC_/SAI_/' -e "s|/${OLD_NAME}|/${APP_NAME}|g" -e "s|=${OLD_NAME}\$|=${APP_NAME}|" \
           -e 's/^# Linux Server Control/# SysAdmin.IA/' "$ENV_FILE"
  fi
  if [[ -d "/var/lib/${OLD_NAME}" && ! -e "$DATA_DIR" ]]; then
    mv "/var/lib/${OLD_NAME}" "$DATA_DIR"
    for ext in "" -wal -shm; do
      if [[ -f "$DATA_DIR/lsc.sqlite3${ext}" ]]; then
        mv "$DATA_DIR/lsc.sqlite3${ext}" "$DATA_DIR/${APP_NAME}.sqlite3${ext}"
      fi
    done
  fi
  if id lsc >/dev/null 2>&1 && ! id "$SVC_USER" >/dev/null 2>&1; then
    usermod -l "$SVC_USER" -d "$DATA_DIR" lsc
    if getent group lsc >/dev/null; then groupmod -n "$SVC_USER" lsc; fi
  fi
  if [[ -f "/etc/sudoers.d/${OLD_NAME}" ]]; then
    rm -f "/etc/sudoers.d/${OLD_NAME}"
    if [[ -z "$FULL_ACCESS" ]]; then FULL_ACCESS=1; fi   # keep the access level granted before
  fi
  rm -f /usr/local/bin/lscctl
fi

# ------------------------------------------------------------------ packages
install_packages() {
  c_info "Installing system packages"
  if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip git openssl sudo ca-certificates >/dev/null
  elif command -v dnf >/dev/null; then
    dnf install -y -q python3 python3-pip git openssl sudo
  elif command -v yum >/dev/null; then
    yum install -y -q python3 python3-pip git openssl sudo
  elif command -v zypper >/dev/null; then
    zypper --non-interactive install -y python3 python3-pip git openssl sudo
  elif command -v pacman >/dev/null; then
    pacman -Sy --noconfirm --needed python python-pip git openssl sudo
  elif command -v apk >/dev/null; then
    apk add --no-cache python3 py3-pip git openssl sudo bash shadow util-linux
  else
    c_warn "Unknown package manager; make sure python3 (>=3.10, with venv), git, openssl and sudo are installed."
  fi
}
install_packages

PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || die "python3 not found."
"$PYTHON" - <<'EOF' || die "Python 3.10 or newer is required (found $("$PYTHON" -V 2>&1)). Install a newer python3 and re-run."
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF

# ------------------------------------------------------------------ user & dirs
if ! id "$SVC_USER" >/dev/null 2>&1; then
  c_info "Creating system user '$SVC_USER'"
  NOLOGIN="$(command -v nologin || echo /bin/false)"
  useradd --system --home-dir "$DATA_DIR" --create-home --shell "$NOLOGIN" "$SVC_USER" 2>/dev/null \
    || adduser -S -h "$DATA_DIR" -s "$NOLOGIN" "$SVC_USER"
fi
mkdir -p "$PREFIX" "$CONF_DIR" "$DATA_DIR"
chown "$SVC_USER:$SVC_USER" "$DATA_DIR"; chmod 700 "$DATA_DIR"

as_svc() {
  if command -v runuser >/dev/null; then runuser -u "$SVC_USER" -- env HOME="$DATA_DIR" "$@"
  else sudo -u "$SVC_USER" -- env HOME="$DATA_DIR" "$@"; fi
}

# ------------------------------------------------------------------ code
UPGRADE=0; [[ -f "$PREFIX/app/main.py" ]] && UPGRADE=1
if [[ $UPGRADE -eq 1 && -d "$PREFIX/.git" ]]; then
  c_info "Snapshotting current app state in git before upgrading"
  as_svc git -C "$PREFIX" add -A >/dev/null 2>&1 || true
  as_svc git -C "$PREFIX" commit -qm "Snapshot before upgrade $(date -Is)" >/dev/null 2>&1 || true
fi

c_info "Copying application to $PREFIX"
tar -C "$SRC_DIR" --exclude='.venv' --exclude='data' --exclude='.git' --exclude='__pycache__' --exclude='.env' -cf - . \
  | tar -C "$PREFIX" -xf -
chown -R "$SVC_USER:$SVC_USER" "$PREFIX"
chmod 755 "$PREFIX"

c_info "Creating Python virtualenv and installing dependencies"
[[ -x "$PREFIX/.venv/bin/python" ]] || as_svc "$PYTHON" -m venv "$PREFIX/.venv"
as_svc "$PREFIX/.venv/bin/pip" install -q --upgrade pip
as_svc "$PREFIX/.venv/bin/pip" install -q -r "$PREFIX/requirements.txt"

# Git repo so Claude's self-modifications can be reviewed and rolled back.
as_svc git config --global user.name  "SysAdmin.IA" >/dev/null 2>&1 || true
as_svc git config --global user.email "sysadmin-ia@$(hostname)" >/dev/null 2>&1 || true
as_svc git config --global --add safe.directory "$PREFIX"     >/dev/null 2>&1 || true
if [[ ! -d "$PREFIX/.git" ]]; then
  as_svc git -C "$PREFIX" init -q
  printf '.venv/\n__pycache__/\n*.pyc\ndata/\n.env\n' > "$PREFIX/.gitignore"; chown "$SVC_USER:$SVC_USER" "$PREFIX/.gitignore"
fi
as_svc git -C "$PREFIX" add -A
as_svc git -C "$PREFIX" commit -qm "$( [[ $UPGRADE -eq 1 ]] && echo Upgrade || echo Initial install ) $(date -Is)" >/dev/null 2>&1 || true

# ------------------------------------------------------------------ config
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -z "$API_KEY" && $INTERACTIVE -eq 1 ]]; then
    echo "An Anthropic API key is optional now: you can add it later in the web UI"
    echo "(Admin → Claude API) or with 'sudo sysadmin-ia set-api-key'. The monitor works without it."
    read -r -s -p "Anthropic API key (press Enter to skip): " API_KEY; echo
  fi
  c_info "Writing $ENV_FILE"
  umask 027
  cat > "$ENV_FILE" <<EOF
# SysAdmin.IA configuration. Restart after editing:
#   sudo systemctl restart ${APP_NAME}
# The API key, model and effort below are defaults; values saved in the web UI
# (Admin → Claude API) or with 'sysadmin-ia set-api-key' take precedence and need no restart.
ANTHROPIC_API_KEY=${API_KEY}
SAI_MODEL=claude-opus-5-5
SAI_EFFORT=high
SAI_HOST=${BIND}
SAI_PORT=${PORT}
SAI_DATA_DIR=${DATA_DIR}
SAI_SERVICE_NAME=${APP_NAME}
SAI_SECRET_KEY=$(openssl rand -hex 32)
SAI_SESSION_HOURS=12
SAI_COMMAND_TIMEOUT=300
SAI_ALLOW_AUTO_APPROVE=true
# Server-side refusal fallback routing ("default"), or empty to disable.
SAI_FALLBACKS=default
EOF
  if [[ $TLS -eq 1 ]]; then
    echo "SAI_TLS_CERT=${CONF_DIR}/tls/cert.pem" >> "$ENV_FILE"
    echo "SAI_TLS_KEY=${CONF_DIR}/tls/key.pem"   >> "$ENV_FILE"
    echo "# Plain-HTTP port served alongside HTTPS (unencrypted!). 0 disables it." >> "$ENV_FILE"
    echo "SAI_HTTP_PORT=${HTTP_PORT}" >> "$ENV_FILE"
  fi
  umask 022
else
  c_info "Keeping existing $ENV_FILE"
  if [[ -n "$API_KEY" ]]; then
    sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${API_KEY}|" "$ENV_FILE"
  fi
  if grep -q '^SAI_TLS_CERT=.\+' "$ENV_FILE"; then
    if grep -q '^SAI_HTTP_PORT=' "$ENV_FILE"; then
      # Only change an existing value when --http-port / --no-http was given explicitly.
      if [[ $HTTP_PORT_SET -eq 1 ]]; then
        sed -i "s|^SAI_HTTP_PORT=.*|SAI_HTTP_PORT=${HTTP_PORT}|" "$ENV_FILE"
      fi
    else
      printf '# Plain-HTTP port served alongside HTTPS (unencrypted!). 0 disables it.\nSAI_HTTP_PORT=%s\n' "$HTTP_PORT" >> "$ENV_FILE"
    fi
  fi
fi
# Effective ports, for the messages below.
TLS=0; grep -q '^SAI_TLS_CERT=.\+' "$ENV_FILE" && TLS=1
PORT="$(sed -n 's/^SAI_PORT=//p' "$ENV_FILE" | tail -1)"; PORT="${PORT:-8765}"
HTTP_PORT="$(sed -n 's/^SAI_HTTP_PORT=//p' "$ENV_FILE" | tail -1)"; HTTP_PORT="${HTTP_PORT:-0}"
chown root:"$SVC_USER" "$ENV_FILE"; chmod 640 "$ENV_FILE"

if [[ $TLS -eq 1 && ! -f "$CONF_DIR/tls/cert.pem" ]]; then
  c_info "Generating self-signed TLS certificate (replace with a real one for production)"
  mkdir -p "$CONF_DIR/tls"
  HOST_FQDN="$(hostname -f 2>/dev/null || hostname)"
  SAN="DNS:${HOST_FQDN},DNS:$(hostname),DNS:localhost,IP:127.0.0.1"
  for ip in $(hostname -I 2>/dev/null || true); do [[ "$ip" == *:* ]] || SAN="${SAN},IP:${ip}"; done
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 -sha256 \
    -keyout "$CONF_DIR/tls/key.pem" -out "$CONF_DIR/tls/cert.pem" \
    -subj "/CN=${HOST_FQDN}" -addext "subjectAltName=${SAN}" >/dev/null 2>&1
  chown -R root:"$SVC_USER" "$CONF_DIR/tls"; chmod 750 "$CONF_DIR/tls"; chmod 640 "$CONF_DIR/tls/"*.pem
fi

# ------------------------------------------------------------------ privileges
SUDOERS="/etc/sudoers.d/${APP_NAME}"
if [[ -z "$FULL_ACCESS" ]]; then
  if [[ -f "$SUDOERS" ]]; then FULL_ACCESS=1
  else
    echo
    c_warn "Full access lets Claude (after your approval in the web UI) run commands as root,"
    c_warn "e.g. install packages, edit /etc, restart services. Without it, Claude can only act"
    c_warn "as the unprivileged '$SVC_USER' user (it can still modify this app)."
    if ask_yn "Grant the service passwordless sudo (full server control)?" y; then FULL_ACCESS=1; else FULL_ACCESS=0; fi
  fi
fi
if [[ "$FULL_ACCESS" == 1 ]]; then
  c_info "Granting passwordless sudo to '$SVC_USER' ($SUDOERS)"
  mkdir -p /etc/sudoers.d
  echo "$SVC_USER ALL=(ALL) NOPASSWD:ALL" > "$SUDOERS.tmp"
  chmod 440 "$SUDOERS.tmp"
  if visudo -cf "$SUDOERS.tmp" >/dev/null; then mv "$SUDOERS.tmp" "$SUDOERS"; else rm -f "$SUDOERS.tmp"; die "sudoers validation failed"; fi
else
  rm -f "$SUDOERS"
fi

# ------------------------------------------------------------------ CLI wrapper
cat > /usr/local/bin/sysadmin-ia <<EOF
#!/usr/bin/env bash
# Manage SysAdmin.IA users / audit log. Run as root.
[[ \$EUID -eq 0 ]] || exec sudo "\$0" "\$@"
cd "${PREFIX}"
set -a; . "${ENV_FILE}"; set +a
ENVV=(SAI_DATA_DIR="${DATA_DIR}" ANTHROPIC_API_KEY="\${ANTHROPIC_API_KEY:-}" SAI_MODEL="\${SAI_MODEL:-}" SAI_EFFORT="\${SAI_EFFORT:-}")
if command -v runuser >/dev/null; then
  exec runuser -u ${SVC_USER} -- env -i PATH="\$PATH" HOME="${DATA_DIR}" "\${ENVV[@]}" "${PREFIX}/.venv/bin/python" -m app.cli "\$@"
else
  exec sudo -u ${SVC_USER} env -i PATH="\$PATH" HOME="${DATA_DIR}" "\${ENVV[@]}" "${PREFIX}/.venv/bin/python" -m app.cli "\$@"
fi
EOF
chmod 755 /usr/local/bin/sysadmin-ia

# ------------------------------------------------------------------ first admin user
USER_COUNT="$(/usr/local/bin/sysadmin-ia list-users | wc -l)"
if [[ "$USER_COUNT" -eq 0 ]]; then
  if [[ $INTERACTIVE -eq 1 ]]; then
    read -r -p "Admin username [${ADMIN_NAME}]: " n; ADMIN_NAME="${n:-$ADMIN_NAME}"
    c_info "Set a password for '${ADMIN_NAME}'"
    /usr/local/bin/sysadmin-ia create-user "$ADMIN_NAME" --role admin
  else
    PW="$(openssl rand -base64 18)"
    printf '%s\n' "$PW" | /usr/local/bin/sysadmin-ia create-user "$ADMIN_NAME" --role admin --password-stdin
    GENERATED_PW="$PW"
  fi
fi

# ------------------------------------------------------------------ service
if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
  c_info "Installing systemd service"
  cat > "$UNIT_FILE" <<EOF
[Unit]
Description=SysAdmin.IA (web UI + Claude assistant)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${PREFIX}
EnvironmentFile=${ENV_FILE}
ExecStart=${PREFIX}/.venv/bin/python -m app
# Always restart: the app exits on purpose to load self-modified code.
Restart=always
RestartSec=2
# NoNewPrivileges is intentionally not set: Claude's commands may use sudo.

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "${APP_NAME}" >/dev/null 2>&1
  systemctl restart "${APP_NAME}"
  sleep 2
  systemctl is-active --quiet "${APP_NAME}" || c_warn "Service failed to start: journalctl -u ${APP_NAME} -n 50"
else
  c_warn "systemd not detected. Start the app manually, e.g.:"
  echo "  cd $PREFIX && set -a && . $ENV_FILE && set +a && runuser -u $SVC_USER -- .venv/bin/python -m app"
fi

# ------------------------------------------------------------------ firewall hint
PORTS="$PORT"
if [[ $TLS -eq 1 && "$HTTP_PORT" != 0 ]]; then PORTS="$PORT $HTTP_PORT"; fi
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  c_warn "ufw is active. Allow access with (ideally only from trusted IPs):"
  for p in $PORTS; do echo "      sudo ufw allow ${p}/tcp"; done
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  c_warn "firewalld is active. Allow access with:"
  for p in $PORTS; do echo "      sudo firewall-cmd --add-port=${p}/tcp --permanent"; done
  echo "      sudo firewall-cmd --reload"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
if [[ $TLS -eq 1 ]]; then
  c_info "Done. Open https://${IP:-<server-ip>}:${PORT}/"
  if [[ "$HTTP_PORT" != 0 ]]; then
    echo "    or (unencrypted) http://${IP:-<server-ip>}:${HTTP_PORT}/"
  fi
else
  c_info "Done. Open http://${IP:-<server-ip>}:${PORT}/"
fi
[[ -n "${GENERATED_PW:-}" ]] && echo "    Admin login: ${ADMIN_NAME} / ${GENERATED_PW}   (change it under Admin)"
if ! grep -q '^ANTHROPIC_API_KEY=.\+' "$ENV_FILE"; then
  echo "    No API key yet. Add it after signing in: Admin → Claude API (or: sudo sysadmin-ia set-api-key)."
fi
[[ $TLS -eq 1 ]] && echo "    The certificate is self-signed; your browser will warn the first time."
echo "    Manage users: sudo sysadmin-ia --help    Logs: journalctl -u ${APP_NAME} -f"
