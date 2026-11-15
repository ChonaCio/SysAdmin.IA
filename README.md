# SysAdmin.IA

A self-hosted, password-protected web app for a Linux server that gives you:

- **Chat with Claude** (Anthropic API, default model `claude-opus-5-5`), with streaming replies and conversation history.
- **Server changes from the chat.** Claude can run shell commands and view or edit files on the server. Every state-changing action shows up as a card that you **approve, edit, or deny** before it runs.
- **Changes to the app itself.** The app's code lives in a git repository. Claude can edit it, check it, commit it, and restart the service (`restart_app` tool). The restart is refused if the new code doesn't import.
- **Resource monitor.** CPU (total and per core), memory, swap, load, network and disk I/O with 5-minute charts, filesystems, temperatures, top processes, and failed systemd units.
- **Admin tools.** Users with `admin` or `viewer` roles (viewers only see the monitor), password changes, and an audit log of every login, chat, approval and command.

Tested target is Ubuntu Server. The installer also handles Debian, RHEL/Fedora/Rocky/Alma, openSUSE and Arch (all systemd). Alpine works without the service unit.

## Install

```bash
git clone <this repo> sysadmin-ia   # or copy the folder to the server
cd sysadmin-ia
sudo ./install.sh
```

The installer:

1. installs `python3` (3.10 or newer), `venv`, `git`, `openssl` and `sudo` with the distro's package manager
2. creates the system user `sysadmin-ia` and copies the app to `/opt/sysadmin-ia` (a git repo, owned by `sysadmin-ia`, so Claude can change it)
3. writes the config to `/etc/sysadmin-ia/sysadmin-ia.env` and asks for your Anthropic API key. **You can skip this.** The app installs and runs without a key (the monitor and admin pages work), and you can add the key later (see below)
4. generates a self-signed TLS certificate, then serves **HTTPS on port 8765** and **plain HTTP on port 8080**
5. asks whether to grant **full access** (passwordless sudo for `sysadmin-ia`). Without it, Claude can only act as the unprivileged `sysadmin-ia` user
6. creates the first admin user and installs and starts `sysadmin-ia.service`

Then open `https://<server-ip>:8765/` (your browser warns about the self-signed certificate the first time) or `http://<server-ip>:8080/`.

Non-interactive example (`--api-key` is optional):

```bash
sudo ./install.sh -y --full-access --admin alice --port 8765 --http-port 8080
```

### HTTP and HTTPS

Both run in the same process and share logins, conversations and settings. Each one keeps its own login, though: signing in over HTTPS doesn't sign you in over HTTP, and the other way around.

- **HTTP is unencrypted.** Anyone on the network path can read your password, session cookie, and everything Claude does. Use it only on a trusted LAN, or behind a reverse proxy that adds TLS. The login page shows a warning and a link to HTTPS when you use HTTP.
- Change the HTTP port with `SAI_HTTP_PORT=` in the config file, or turn HTTP off with `SAI_HTTP_PORT=0`, then restart. At install time, use `--http-port PORT` or `--no-http`.
- While HTTP is on, the app doesn't send HSTS. HSTS applies to every port on the host, so it would make browsers refuse the HTTP port.

### Upgrading from "Linux Server Control"

Copy the new version to the server and run `sudo ./install.sh` again. The installer detects the old `linux-server-control` install and migrates it in place:

- stops the old service and removes it
- moves `/opt`, `/etc` and `/var/lib` to `sysadmin-ia`
- renames the `lsc` user and the database
- converts `LSC_*` settings to `SAI_*`
- keeps your users, conversations, API key and sudo choice
- adds HTTP on port 8080

The command-line tool `lscctl` is now called `sysadmin-ia`.

### Adding the API key later

You don't need to restart for any of these:

- **Web UI:** sign in as an admin, open **Admin → Claude API**, paste the key, click **Test**, then **Save**. You can also change the model and effort there.
- **Command line:** `sudo sysadmin-ia set-api-key` (it prompts for the key), then `sudo sysadmin-ia claude-status` to check.
- **Config file:** set `ANTHROPIC_API_KEY=` in `/etc/sysadmin-ia/sysadmin-ia.env` and run `sudo systemctl restart sysadmin-ia`.

A key saved in the web UI or with the `sysadmin-ia` command takes precedence over the config file. It's stored in the app database (`/var/lib/sysadmin-ia/sysadmin-ia.sqlite3`, readable only by the `sysadmin-ia` user), and the UI only ever shows a masked hint of it. Until a key is set, the chat shows a banner linking to the settings.

To upgrade, run `install.sh` again from a newer copy. Your config, data and users are kept, and the current app state (including Claude's edits) is committed to git first.

### Updating from GitHub

The app can update itself from [github.com/ChonaCio/SysAdmin.IA](https://github.com/ChonaCio/SysAdmin.IA):

- **Web UI:** **Admin → Updates** shows the installed version and checks GitHub (automatically, at most once an hour, or with **Check for updates**). When a newer version exists, **Update now** installs it and restarts the service; the page reloads when it's back.
- **Command line:** `sudo sysadmin-ia check-update`, then `sudo sysadmin-ia update` and `sudo systemctl restart sysadmin-ia`.

The latest version is the newest GitHub **release** (tag like `v1.2.0`) or, if the repository has no releases, the `VERSION` file on the `main` branch. The installed version is the `VERSION` file in `/opt/sysadmin-ia`. To publish a new version, bump `VERSION` (and optionally create a release with the matching tag).

An update downloads the code, backs up the current code to `/var/lib/sysadmin-ia/backups/app-<version>-<time>.tar.gz`, copies the new files over `/opt/sysadmin-ia` (config, data and the virtualenv are kept), reinstalls Python dependencies if `requirements.txt` changed, and checks that the new code loads. If any step fails the backup is restored. Both the old and the new state are committed to the app's local git repo, so Claude's own edits to the app can be recovered with `git log` / `git diff` if an update overwrites them.

If the repository is private, save a GitHub token (a fine-grained token with read-only **Contents** access is enough) in **Admin → Updates** or with `sudo sysadmin-ia set-github-token`. The in-app update only refreshes the app code and Python packages; if a release changes `install.sh` (systemd unit, sudoers, CLI wrapper), re-run `install.sh` from that release instead.

## Day-to-day

| Task | Command |
|---|---|
| Logs | `journalctl -u sysadmin-ia -f` |
| Restart | `sudo systemctl restart sysadmin-ia` |
| Add user | `sudo sysadmin-ia create-user bob --role admin` (or `viewer`) |
| Reset password | `sudo sysadmin-ia reset-password alice` |
| Audit log | `sudo sysadmin-ia audit -n 100` |
| Set / remove API key | `sudo sysadmin-ia set-api-key` / `sudo sysadmin-ia clear-api-key` |
| Change model | `sudo sysadmin-ia set-model claude-sonnet-5-5` (or use Admin → Claude API) |
| Check for / install updates | `sudo sysadmin-ia check-update` / `sudo sysadmin-ia update` (or Admin → Updates) |
| Roll back an app change Claude made | `sudo -u sysadmin-ia git -C /opt/sysadmin-ia log --oneline`, then `sudo -u sysadmin-ia git -C /opt/sysadmin-ia revert <sha>` and restart |
| Restore a file Claude edited | backups are in `/var/lib/sysadmin-ia/backups/<timestamp>/<original path>` |
| Uninstall | `sudo ./uninstall.sh` (add `--purge` to remove config, data and the user) |

## Configuration (`/etc/sysadmin-ia/sysadmin-ia.env`)

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Your API key. Optional; a key saved in the web UI overrides it |
| `SAI_MODEL` | `claude-opus-5-5` | Default Claude model; the web UI can override it |
| `SAI_EFFORT` | `high` | Default effort: `low` / `medium` / `high` / `xhigh` / `max`. Higher is more thorough, slower and more expensive. The web UI can override it |
| `SAI_MAX_TOKENS` | `64000` | Max output tokens per response |
| `SAI_FALLBACKS` | `default` | Server-side refusal fallback (another model answers if one declines). Empty disables it |
| `SAI_HOST` | `0.0.0.0` | Listen address |
| `SAI_PORT` | `8765` | HTTPS port (or the only, plain-HTTP port if no TLS certificate is configured) |
| `SAI_HTTP_PORT` | `8080` | Extra plain-HTTP port served alongside HTTPS; `0` disables it |
| `SAI_TLS_CERT` / `SAI_TLS_KEY` | self-signed | Replace with real certificates (for example Let's Encrypt) if you have a domain |
| `SAI_SESSION_HOURS` | `12` | Login session lifetime |
| `SAI_COMMAND_TIMEOUT` | `300` | Seconds before a command is killed |
| `SAI_ALLOW_AUTO_APPROVE` | `true` | Whether users can turn on "Auto-approve actions" in the chat |
| `SAI_UPDATE_REPO` | `ChonaCio/SysAdmin.IA` | GitHub repository checked for updates |
| `SAI_UPDATE_BRANCH` | `main` | Branch whose `VERSION` file is used when the repository has no releases |
| `SAI_GITHUB_TOKEN` | – | GitHub token for a private repository; one saved in the web UI overrides it |

Restart the service after editing.

## Security notes — read these

This app gives a web page root-level control of the server if you grant full access. Treat it like SSH:

- **Don't expose it to the internet.** Limit the ports to trusted IPs (`ufw allow from 203.0.113.4 to any port 8765`), or reach it over a VPN or SSH tunnel (`ssh -L 8765:localhost:8765 server`, with `SAI_HOST=127.0.0.1`).
- **Don't use the HTTP port on untrusted networks**: passwords and sessions travel unencrypted. Set `SAI_HTTP_PORT=0` if you don't need it.
- Use strong passwords. Logins are throttled (5 failures per 15 minutes per IP and user), sessions are server-side, cookies are `HttpOnly` and `SameSite=Strict`, and there's a CSP and a CSRF header check.
- Leave auto-approve off unless you are watching. Claude explains risky steps before taking them, but you are the safeguard.
- The API key is removed from the environment of Claude's shell, but Claude's commands run as `sysadmin-ia`, which can read the app database and (with sudo) the config file. Use a dedicated API key with a spend limit.
- Everything is recorded in the audit log (Admin tab, or `sysadmin-ia audit`).

## How it works

```
browser ─HTTP(S)/WebSocket─► FastAPI (app/main.py) ──► Claude API (streaming, tool use)
                                 │                        │
                                 │      tool_use ◄────────┘
                                 ▼
                    approval card in the browser ──approve──► bash session / file editor / restart
                                 │
                       SQLite: users, sessions, conversations, audit log
```

- `app/agent.py`: the Claude loop. It streams replies (with summarized reasoning), runs tools after approval, and stores full message history, including thinking blocks, so conversations can be resumed.
- `app/tools.py`: a persistent bash session per conversation (stdin is `/dev/null`, per-command timeout, output truncated to 30 KB), and a text editor with automatic backups.
- `app/monitor.py`: psutil and systemd metrics.
- `app/static/`: plain HTML, CSS and JS with no build step and no CDN. Frontend edits take effect on reload.

## Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # API key optional here; you can add it in Admin → Claude API
python -m app.cli create-user admin
python -m app          # http://127.0.0.1:8765
```
