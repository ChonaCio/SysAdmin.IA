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
#   --server-name NAME   server (host) name; the UI is then reachable on the LAN at NAME.local
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
SERVER_NAME=""
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
    --server-name) SERVER_NAME="$2"; shift 2 ;;
    --full-access) FULL_ACCESS=1; shift ;;
    --no-full-access) FULL_ACCESS=0; shift ;;
    --no-tls) TLS=0; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
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
    apt-get install -y -qq avahi-daemon libnss-mdns >/dev/null || c_warn "Could not install avahi (NAME.local won't work)."
    # avahi-browse: the Network module reads the names devices announce on the LAN (optional)
    apt-get install -y -qq avahi-utils >/dev/null || true
  elif command -v dnf >/dev/null; then
    dnf install -y -q python3 python3-pip git openssl sudo
    dnf install -y -q avahi nss-mdns || dnf install -y -q avahi || c_warn "Could not install avahi (NAME.local won't work)."
    dnf install -y -q avahi-tools || true
  elif command -v yum >/dev/null; then
    yum install -y -q python3 python3-pip git openssl sudo
    yum install -y -q avahi || c_warn "Could not install avahi (NAME.local won't work)."
    yum install -y -q avahi-tools || true
  elif command -v zypper >/dev/null; then
    zypper --non-interactive install -y python3 python3-pip git openssl sudo
    zypper --non-interactive install -y avahi nss-mdns || c_warn "Could not install avahi (NAME.local won't work)."
    zypper --non-interactive install -y avahi-utils || true
  elif command -v pacman >/dev/null; then
    pacman -Sy --noconfirm --needed python python-pip git openssl sudo
    pacman -S --noconfirm --needed avahi nss-mdns || c_warn "Could not install avahi (NAME.local won't work)."
  elif command -v apk >/dev/null; then
    apk add --no-cache python3 py3-pip git openssl sudo bash shadow util-linux
    apk add --no-cache avahi || c_warn "Could not install avahi (NAME.local won't work)."
    apk add --no-cache avahi-tools || true
  else
    c_warn "Unknown package manager; make sure python3 (>=3.10, with venv), git, openssl and sudo are installed,"
    c_warn "plus avahi-daemon if you want to reach the server as NAME.local."
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
tar -C "$SRC_DIR" --exclude='.venv' --exclude='data' --exclude='.git' --exclude='__pycache__' --exclude='.env' --exclude='HANDOFF.md' -cf - . \
  | tar -C "$PREFIX" -xf -

# The version being installed: the highest of the VERSION file, the git tag this copy was
# checked out from (git clone + checkout), and a release archive's folder name
# (SysAdmin.IA-1.3.3). That way VERSION needn't be bumped by hand for each release.
detect_version() {
  local v="" tag base
  v="$(tr -d '[:space:]' < "${SRC_DIR}/VERSION" 2>/dev/null || true)"
  if command -v git >/dev/null && [[ -e "${SRC_DIR}/.git" ]]; then
    tag="$(git -c safe.directory="$SRC_DIR" -C "$SRC_DIR" describe --tags --abbrev=0 --match 'v[0-9]*' 2>/dev/null \
        || git -c safe.directory="$SRC_DIR" -C "$SRC_DIR" describe --tags --abbrev=0 --match '[0-9]*' 2>/dev/null || true)"
    tag="${tag#[vV]}"
  fi
  base="$(basename "$SRC_DIR")"
  [[ -z "${tag:-}" && "$base" =~ -[vV]?([0-9]+(\.[0-9]+){1,3})$ ]] && tag="${BASH_REMATCH[1]}"
  printf '%s\n' "$v" "${tag:-}" | grep -E '^[0-9]+(\.[0-9]+){0,3}$' \
    | sort -t. -k1,1n -k2,2n -k3,3n -k4,4n | tail -1
}
APP_VERSION="$(detect_version || true)"
if [[ -n "$APP_VERSION" ]]; then echo "$APP_VERSION" > "$PREFIX/VERSION"; fi
c_info "Version ${APP_VERSION:-unknown}"
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
    echo "An Anthropic API key is optional now: you can add Claude, ChatGPT, Grok, Gemini and other"
    echo "AI services later in the web UI (AI connections). The monitor works without one."
    read -r -s -p "Anthropic API key (press Enter to skip): " API_KEY; echo
  fi
  c_info "Writing $ENV_FILE"
  umask 027
  cat > "$ENV_FILE" <<EOF
# SysAdmin.IA configuration. Restart after editing:
#   sudo systemctl restart ${APP_NAME}
# AI services are managed in the web UI (AI connections). An API key here is used by a
# connection whose key field is left empty (also OPENAI_API_KEY, XAI_API_KEY, GEMINI_API_KEY, ...).
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
# Self-update source (Admin → Updates / 'sysadmin-ia update'). A token is only needed
# for a private repository; one saved in the web UI takes precedence.
#SAI_UPDATE_REPO=ChonaCio/SysAdmin.IA
#SAI_UPDATE_BRANCH=main
#SAI_GITHUB_TOKEN=
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

# ------------------------------------------------------------------ server name & LAN discovery
# Root-owned helper that sets the hostname, announces NAME.local via mDNS (Avahi) and keeps
# the self-signed certificate valid for the name. The service user may run it (and only it)
# through sudo, so the name can also be changed later from Network → Names & websites → Server name.
HELPER="/usr/local/sbin/${APP_NAME}-hostname"
bash "${SRC_DIR}/scripts/${APP_NAME}-hostname" install | sed 's/^/    /' \
  || c_warn "Could not install the server-name helper; the server can't be renamed from the web UI."

valid_server_name() { [[ "$1" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ && ! "$1" =~ ^[0-9]+$ ]]; }
CURRENT_NAME="$(hostname -s 2>/dev/null || hostname)"
if [[ -z "$SERVER_NAME" && $INTERACTIVE -eq 1 ]]; then
  echo "The server name is how other computers on your network reach it: https://NAME.local:${PORT:-8765}/"
  while true; do
    read -r -p "Server name [${CURRENT_NAME}]: " SERVER_NAME
    SERVER_NAME="${SERVER_NAME%.local}"
    [[ -z "$SERVER_NAME" ]] && break
    valid_server_name "${SERVER_NAME,,}" && break
    echo "Use 1-63 letters, digits and hyphens (no spaces or dots, not starting/ending with a hyphen)."
  done
fi
SERVER_NAME="${SERVER_NAME%.local}"
if [[ -n "$SERVER_NAME" ]] && ! valid_server_name "${SERVER_NAME,,}"; then
  c_warn "Invalid --server-name '${SERVER_NAME}'; keeping '${CURRENT_NAME}'."
  SERVER_NAME=""
fi
SERVER_NAME="${SERVER_NAME:-$CURRENT_NAME}"
if [[ "${SERVER_NAME,,}" != "${CURRENT_NAME,,}" ]]; then
  SERVER_NAME="${SERVER_NAME,,}"
  c_info "Setting server name to '${SERVER_NAME}'"
  "$HELPER" set "$SERVER_NAME" | sed 's/^/    /' || c_warn "Could not set the server name (see the message above)."
else
  "$HELPER" mdns | sed 's/^/    /' || true
fi
if [[ $TLS -eq 1 ]]; then
  c_info "Checking the TLS certificate"
  "$HELPER" cert | grep -v '^CERT_CHANGED$' | sed 's/^/    /' || c_warn "Could not create the TLS certificate."
fi
SERVER_NAME="$(hostname -s 2>/dev/null || hostname)"

# ------------------------------------------------------------------ privileges
SUDOERS="/etc/sudoers.d/${APP_NAME}"
if [[ -z "$FULL_ACCESS" ]]; then
  if [[ -f "$SUDOERS" ]]; then FULL_ACCESS=1
  elif [[ $UPGRADE -eq 1 && $INTERACTIVE -eq 0 ]]; then
    FULL_ACCESS=0   # unattended upgrade (e.g. from Admin → Updates): keep the earlier choice
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
ENVV=(SAI_DATA_DIR="${DATA_DIR}" ANTHROPIC_API_KEY="\${ANTHROPIC_API_KEY:-}" SAI_MODEL="\${SAI_MODEL:-}" SAI_EFFORT="\${SAI_EFFORT:-}" SAI_SERVICE_NAME="${APP_NAME}"
      SAI_GITHUB_TOKEN="\${SAI_GITHUB_TOKEN:-}" SAI_UPDATE_REPO="\${SAI_UPDATE_REPO:-}" SAI_UPDATE_BRANCH="\${SAI_UPDATE_BRANCH:-}")
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
  echo "      sudo ufw allow 5353/udp    # mDNS, so ${SERVER_NAME}.local resolves on the LAN"
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  c_warn "firewalld is active. Allow access with:"
  for p in $PORTS; do echo "      sudo firewall-cmd --add-port=${p}/tcp --permanent"; done
  echo "      sudo firewall-cmd --add-service=mdns --permanent    # so ${SERVER_NAME}.local resolves"
  echo "      sudo firewall-cmd --reload"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
MDNS=0; command -v avahi-daemon >/dev/null && MDNS=1
echo
if [[ $TLS -eq 1 ]]; then
  if [[ $MDNS -eq 1 ]]; then
    c_info "Done. Open https://${SERVER_NAME}.local:${PORT}/   (or https://${IP:-<server-ip>}:${PORT}/)"
  else
    c_info "Done. Open https://${IP:-<server-ip>}:${PORT}/"
  fi
  if [[ "$HTTP_PORT" != 0 ]]; then
    echo "    or (unencrypted) http://$( [[ $MDNS -eq 1 ]] && echo "${SERVER_NAME}.local" || echo "${IP:-<server-ip>}" ):${HTTP_PORT}/"
  fi
else
  if [[ $MDNS -eq 1 ]]; then
    c_info "Done. Open http://${SERVER_NAME}.local:${PORT}/   (or http://${IP:-<server-ip>}:${PORT}/)"
  else
    c_info "Done. Open http://${IP:-<server-ip>}:${PORT}/"
  fi
fi
[[ -n "${GENERATED_PW:-}" ]] && echo "    Admin login: ${ADMIN_NAME} / ${GENERATED_PW}   (change it under Admin)"
if ! grep -q '^ANTHROPIC_API_KEY=.\+' "$ENV_FILE"; then
  echo "    No AI connected yet. Add one after signing in: AI connections (or: sudo sysadmin-ia set-api-key)."
fi
[[ $TLS -eq 1 ]] && echo "    The certificate is self-signed; your browser will warn the first time."
echo "    Manage users: sudo sysadmin-ia --help    Logs: journalctl -u ${APP_NAME} -f"
