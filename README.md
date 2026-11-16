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

1. installs `python3` (3.10 or newer), `venv`, `git`, `openssl`, `sudo` and `avahi-daemon` (mDNS) with the distro's package manager
2. creates the system user `sysadmin-ia` and copies the app to `/opt/sysadmin-ia` (a git repo, owned by `sysadmin-ia`, so Claude can change it)
3. writes the config to `/etc/sysadmin-ia/sysadmin-ia.env` and asks for your Anthropic API key. **You can skip this.** The app installs and runs without a key (the monitor and admin pages work), and you can add the key later (see below)
4. asks for the **server name** (default: the current hostname) and announces it on the LAN as `NAME.local`
5. generates a self-signed TLS certificate valid for `NAME.local` and the server's IPs, then serves **HTTPS on port 8765** and **plain HTTP on port 8080**
6. asks whether to grant **full access** (passwordless sudo for `sysadmin-ia`). Without it, Claude can only act as the unprivileged `sysadmin-ia` user
7. creates the first admin user and installs and starts `sysadmin-ia.service`

Then open `https://NAME.local:8765/` or `https://<server-ip>:8765/` (your browser warns about the self-signed certificate the first time), or `http://NAME.local:8080/`.

Non-interactive example (`--api-key` is optional):

```bash
sudo ./install.sh -y --full-access --server-name office-server --admin alice --port 8765 --http-port 8080
```

### Server name and LAN access

The server name is the machine's hostname. The installer runs Avahi (mDNS), so every computer on the same network can reach the app as `https://NAME.local:8765/` without any DNS setup. This works out of the box on Windows 10/11, macOS, iOS, and Linux desktops with `nss-mdns`. Android supports `.local` names in Chrome on recent versions; if one doesn't, use the IP address.

Change the name later in **Admin → Server name**, or with `sudo sysadmin-ia set-server-name NAME` (`sudo sysadmin-ia server-name` shows it and the LAN addresses). Renaming:

- sets the hostname and the `127.0.1.1` entry in `/etc/hosts`
- re-announces the new name via mDNS, and advertises the web UI as a network service
- replaces the self-signed certificate so it is valid for the new name, then restarts the app (browsers show the certificate warning once more). A certificate you installed yourself is never replaced

No reboot is needed: the new name works right away. If a device on the network still uses the old name (for example, a router that registered it), use **Reboot server** in the same card, or `sudo sysadmin-ia reboot`.

These steps need root, so the installer adds a small root-owned helper, `/usr/local/sbin/sysadmin-ia-hostname`, which the `sysadmin-ia` user may run through sudo. This works even without full access; the helper can only do the steps above, reboot, and install updates from the configured GitHub repository. If the app was updated from GitHub and the helper is missing or outdated, the app installs or refreshes it by itself when it has full access. Without full access, the card shows a one-time command to run as root instead:

```bash
sudo bash /opt/sysadmin-ia/scripts/sysadmin-ia-hostname install
```

If a firewall is active, allow mDNS (UDP port 5353): `sudo ufw allow 5353/udp`, or `sudo firewall-cmd --add-service=mdns --permanent && sudo firewall-cmd --reload`. Names must be 1–63 letters, digits and hyphens. `.local` names only work inside the local network (the same subnet); for access from elsewhere, use a DNS name or the IP address.

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

### AI connections (Claude, ChatGPT, Grok, Copilot, Gemini, …)

The **AI connections** module (left sidebar → Settings) manages which AI services the chat can use. You can add as many connections as you like, and edit, test, disable or delete them. One of them is the **default**; each chat can switch to another with the picker next to the message box (the chat remembers its choice).

| Provider | How it connects |
|---|---|
| **Anthropic Claude** | Anthropic API key · **Google account (Sign in with Google)** via Google Cloud Vertex AI · Google Cloud service-account key or the server's Google login · **Amazon Bedrock** with AWS keys or the server's AWS credentials |
| **OpenAI (ChatGPT)** | OpenAI API key (a ChatGPT Plus/Pro subscription is separate from API access) |
| **Microsoft Azure OpenAI** | Azure endpoint + key + deployment (the platform Microsoft Copilot is built on) |
| **GitHub Models (Copilot)** | GitHub token with "Models: read" (OpenAI, xAI, Meta and other models through GitHub) |
| **xAI Grok** · **Groq** · **Google Gemini** · **Mistral** · **DeepSeek** · **OpenRouter** | API key |
| **Ollama** | Local models, no sign-in |
| **Other (OpenAI-compatible)** | Base URL + optional key (vLLM, LM Studio, Together, Fireworks, LiteLLM, …) |

**Usage.** Each connection card shows the tokens used today, this month and in total (input, output and cached), and the rate limits the service reported with its last reply (for example "1.64M of 2.00M input tokens left · resets 5:00 PM"). Optionally set a **monthly token limit** per connection: the card then shows the percentage used and how much is left, and **Stop using this connection when the limit is reached** blocks new requests once it's used up. After each reply, the chat shows how many tokens it used. Some services (for example Ollama) don't report rate limits. Account balances and subscription quotas aren't available through these APIs, so they aren't shown.

**Load models** asks the service which models your key can use, and **Test connection** checks the settings before you save. The assistant needs a model that supports tool (function) calling, which all the suggested models do. Claude gets the most capable tools: Anthropic's built-in shell and editor tools, adaptive thinking, and the effort setting. Other models get the same three tools as plain functions. A chat can change provider partway through.

**Signing in to Claude with Google.** Choose **Anthropic Claude → Google account (Sign in with Google)**. Claude then runs on **Google Cloud Vertex AI** under your Google account, and usage is billed by Google Cloud. (Claude.ai and Claude Code *subscription* logins can't be used by other applications under Anthropic's terms, so the Google sign-in works through Vertex AI instead.)

You need, once, in a Google Cloud project with billing:

1. **Vertex AI → Model Garden:** enable the Claude models. Give your Google account the **Vertex AI User** role in the project.
2. **APIs & Services → OAuth consent screen:** set it up and set the publishing status to **In production**. While it's in "Testing", Google ends the sign-in after 7 days. Workspace organisations can choose "Internal" instead.
3. **Credentials → Create credentials → OAuth client ID → Desktop app:** copy its client ID and secret into the connection.

Then click **Sign in with Google** in the connection editor, choose your account and allow access. Google next sends that tab to `http://127.0.0.1:53682/…`, a page that doesn't load; that's expected. Copy the address from that tab and paste it into the editor, then click **Finish sign-in**, **Test connection** and **Save**. This copy-paste step is needed because Google only sends sign-ins back to public addresses or the local computer, not a LAN name like `office-server.local`.

The app keeps a long-lived Google sign-in (refresh token) on the server and never sends it to the browser. To revoke it, delete the connection, or remove the app at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

Other Google options: paste a service-account key, or leave it empty and sign the server itself in once from the **Console** with `gcloud auth application-default login` (needs the Google Cloud CLI).

Keys and credentials are stored in the app database (`/var/lib/sysadmin-ia/sysadmin-ia.sqlite3`, readable only by the `sysadmin-ia` user), are never sent back to the browser, and every change is recorded in the audit log. A provider's key can also come from the config file instead (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `AZURE_OPENAI_API_KEY`, `GITHUB_TOKEN`): leave the key empty in the connection.

From the command line: `sudo sysadmin-ia set-api-key` saves the key of the Claude connection (creating it if needed), and `sudo sysadmin-ia ai-status` lists the connections.

Upgrading from an older version turns the key, model and effort from the old **Admin → Claude API** card into a connection called "Claude", automatically.

To upgrade, run `install.sh` again from a newer copy. Your config, data and users are kept, and the current app state (including Claude's edits) is committed to git first.

### Updating from GitHub

The app can update itself from [github.com/ChonaCio/SysAdmin.IA](https://github.com/ChonaCio/SysAdmin.IA):

- **Web UI:** **Admin → Updates** (the first card) has the **Check for updates** button and turns it into **Update to X.Y.Z** when a newer version exists, listing every version the update includes with its release notes. While an update runs, a bar with a spinner shows its progress on every page; **See details** opens the installer's output in the **Console**. The page reloads when the new version is running.
- **Command line:** `sudo sysadmin-ia check-update`, then `sudo sysadmin-ia update` (it prints the installer's output until it's done).

Updates are cumulative: the latest version is the **highest version number** among the repository's GitHub releases and version tags (`v1.3.3`), and an update installs it directly, whatever the installed version. Only if the repository has neither, the `VERSION` file on the `main` branch is used. To publish a new version, push a tag (or create a release) such as `v1.3.3`.

The installed version is the `VERSION` file in `/opt/sysadmin-ia`. `install.sh` sets it from the tag it's installed from (a `git clone` checked out at a tag, or a release's "Source code" archive such as `SysAdmin.IA-1.3.3`), so `VERSION` in the repository doesn't have to be bumped by hand. A copy of a branch without a tag (for example "Download ZIP" of `main`) falls back to the `VERSION` file.

**An update does everything a fresh install of the new version would do.** The root helper (`/usr/local/sbin/sysadmin-ia-hostname`) downloads the new version from the repository configured in `/etc/sysadmin-ia/sysadmin-ia.env` and runs its `install.sh -y` in the background. That installs any new system packages (for example avahi for `NAME.local`), and refreshes the systemd unit, the helper, the sudoers rules, the `sysadmin-ia` command, the virtualenv and the app code. Your config, data, users, server name, certificate and full-access choice are kept. Before it starts, the current app state (including Claude's edits) is committed to the app's local git repo. If the new version doesn't come up, the code is reset to that commit and the service is restarted. The output is in `/var/log/sysadmin-ia/upgrade.log`.

The app can only ask the helper to install a version from the configured repository, never code it supplies itself, so this works without granting Claude full access.

**One-time step for older installations.** Servers installed before this feature have no helper, or an older one that can't run updates. With full access, the app installs or refreshes the helper by itself. Otherwise, **Admin → Updates** shows this command to run once as root:

```bash
sudo bash /opt/sysadmin-ia/scripts/sysadmin-ia-hostname install
```

Until then, updates fall back to replacing only the app code and Python packages: the current code is backed up to `/var/lib/sysadmin-ia/backups/app-<version>-<time>.tar.gz`, the new files are copied over `/opt/sysadmin-ia`, and the backup is restored if the new code fails to load.

If the repository is private, save a GitHub token (a fine-grained token with read-only **Contents** access is enough) in **Admin → Updates** or with `sudo sysadmin-ia set-github-token`.

### Drives

The **Drives** tab (admins only) shows every disk connected to the server, internal and external (USB, SD cards), with its model, connection type, SSD/HDD, live read/write speed and each volume's used and free space.

- **Mount…** on an unmounted volume opens a dialog with a ready-made name (the drive's label, or one based on its model) that you can change. The files then appear at `/mnt/<name>`. Optionally tick **Mount automatically when the server starts** (adds an `/etc/fstab` entry by UUID, with `nofail`, so a missing drive never blocks booting) or **Read-only**.
- **Unmount** makes a drive safe to remove, and can also remove its start-up entry. Only drives under `/mnt`, `/media` or `/run/media` can be unmounted here; the operating system's own volumes are marked **System**.
- **Open** / **Browse files** is a file browser with a breadcrumb path, shortcuts to every mounted drive, downloads, and **Analyze sizes**, which adds up how much space each folder uses.

Supported filesystems: ext2/3/4, XFS, Btrfs, FAT32, exFAT, NTFS, F2FS, ISO 9660/UDF, HFS+. Drives are always mounted with `nosuid,nodev`. FAT, exFAT and NTFS drives are owned by the `sysadmin-ia` user so the app can read and write them. Mounting uses the same root helper as renaming and updates (no full access needed). Browsing runs as the `sysadmin-ia` user, so it shows what that user can read. Mounts, unmounts and downloads are recorded in the audit log.

### Users and your profile

- **Users** tab (admins only): everyone who can sign in, with their picture. Change roles (admin or viewer), set a new password for someone, delete users, and add new ones. There's always at least one admin.
- **Your profile card** (top right) shows your picture and username. Click it for **Edit profile** (picture, username, display name, email), **Change password**, and **Sign out**. Pictures are cropped to a square in the browser and stored in the app database.

### Containers (Docker)

The **Containers** module (Server) runs apps in [Docker](https://www.docker.com) containers.

- **Install Docker** runs Docker's official install script (`https://get.docker.com`) as root through the app's helper, in the background with a live log. It installs Docker Engine and Docker Compose, then adds the app's user to the `docker` group. The app restarts once to pick that up (**Restart the app to finish**). If Docker was already installed, **Give access** does the same. **Start / Stop / Restart** control the Docker service, and **Remove Docker** uninstalls it, optionally deleting all containers, images and volumes.
  > Being able to use Docker is equivalent to root access on the server (a container can mount any folder). The app asks before granting it. The chat assistant can also use the `docker` command once access is granted, with your approval as usual.
- **Containers:** a list with state, CPU and memory. Each container shows its ports as links that open `http://this-server:PORT`, and its shared folders. Buttons:
  - **Start / Stop / Restart**.
  - **Logs** (refreshes live).
  - **Terminal** opens a shell inside the container in the Console module.
  - **Edit** and **Remove**.
- **New container:**
  1. Type an image (or **Search Docker Hub**) and click **Get image**. It downloads with progress.
  2. The form fills itself in from the image:
     - **Ports:** the image's ports, each mapped to the same port on the server, or the next free one if that's taken. Conflicts with other containers or programs on the server are shown before you create anything. Each port can be reachable from the whole network or from this server only. You can also use the server's network directly (host mode).
     - **Folders:** the image's data folders become folders on your biggest internal data drive from the Drives module, e.g. `/mnt/media/appdata/jellyfin/config`. Click a drive under **Store on** to move them, or **Browse** to pick any folder. Missing folders are created, and each can be read-only. Docker volumes work too.
     - **Settings:** environment variables (e.g. `TZ`, passwords).
     - **When it stops:** the restart policy.
     - **Advanced:** command, privileged mode, and sharing system folders. System folders such as `/etc` or the Docker socket are refused unless you tick the box.
- **App catalog:** about 35 popular apps, searchable and grouped by category, ready to install with recommended settings:
  - **Databases:** PostgreSQL, MariaDB, MySQL, MongoDB, Redis, Valkey, InfluxDB.
  - **Database tools:** Adminer, pgAdmin.
  - **Web servers & proxies:** nginx, Apache, Caddy, Nginx Proxy Manager.
  - **Media:** Jellyfin, Plex, Navidrome, Audiobookshelf.
  - **Files & sync:** Nextcloud, File Browser, Syncthing.
  - **Home & network:** Home Assistant, Pi-hole, AdGuard Home, Mosquitto.
  - **Monitoring & management:** Uptime Kuma, Grafana, Prometheus, Portainer, Dozzle, Watchtower.
  - **Development & automation:** Gitea, n8n, code-server, RabbitMQ, Docker Registry.
  - **Security:** Vaultwarden, WireGuard (wg-easy).

  **Install** downloads the image and fills in the form:
  - the app's ports, with free server ports picked if they're taken;
  - its data folders on your data drive (e.g. `/mnt/media/appdata/postgres/data`);
  - its settings, with descriptions, required ones marked, and passwords generated for you (copy them somewhere safe);
  - anything special it needs: the server's network for Home Assistant and Plex, kernel network settings for WireGuard, the Docker socket for Portainer.

  Notes from each app's documentation are shown at the top of the form. Apps you already run show **Running as …** and an **Open** button. **Install again** adds another copy with its own name and ports (e.g. `postgres-2` on port 5433).
- **Edit** changes ports, folders, settings or the image version (**Download the newest version** updates the app). Docker can't change a container's ports or folders in place, so the container is recreated. The old one is kept until the new one starts, and put back if the new one fails. Data in shared folders and volumes is kept.
- **Images:** download, update or delete images, search Docker Hub, and create a container from an image.
- **Stacks:** several containers in one Docker Compose file (e.g. an app and its database). Paste the `docker-compose.yml` from an app's documentation, with optional `.env` values. It's checked before saving. A button inserts a folder path from your drives. You can **Start**, **Update** (pull newer images and recreate), **Stop**, view **Logs**, **Edit** or **Delete** a stack. Stacks are stored in `/var/lib/sysadmin-ia/stacks/NAME/`. Stacks started outside the app are listed but read-only.
- **Volumes** and **Networks:** create and delete them. You can connect a container to a network, and containers on the same network reach each other by name. A network can be internal (no internet access).
- **Clean up:** shows how much space images, containers, volumes and the build cache use. It can delete stopped containers, unused images, networks and build cache, and optionally unused volumes.

Downloads and container changes run in the background with progress shown at the top of the page. Existing installations need the updated root helper for install and service control (refreshed automatically with full access; otherwise the page shows the one-time command).

### Local AI (Ollama on this server)

The **Local AI** module (Settings) runs open AI models on the server itself with [Ollama](https://ollama.com). Prompts never leave the server, and there's no per-use cost.

- **Install Ollama** runs Ollama's official installer (`https://ollama.com/install.sh`) as root through the app's helper, in the background with a live log. It adds the `ollama` service and user, with GPU support if a GPU is found. Ollama listens on `127.0.0.1:11434`, so only this server can reach it. **Start / Stop / Restart** control the service.
- **Hardware** shows memory, GPUs (NVIDIA via `nvidia-smi`, or AMD) and free disk space where models are stored, so you can tell which models fit.
- **Context length** sets how much text models keep in mind (Ollama's `OLLAMA_CONTEXT_LENGTH`, via a systemd drop-in). The installer sets 8k; the chat assistant needs at least that, and 16–32k for long tasks (more memory).
- **Install progress** shows a single progress bar with the current step (e.g. "Downloading Linux amd64 bundle… 64%"); the installer's full log is under **Show details**.
- **Download a model:** search three places:
  - **Recommended:** about 40 hand-picked models, from tiny (about 300 MB) to huge (671B), including coding, vision, embeddings, image generation (experimental in Ollama, officially macOS-only for now) and a few from Hugging Face.
  - **Ollama library:** every model on [ollama.com/library](https://ollama.com/library) (about 250), read from the site and kept for 6 hours. Cloud-only models (they run on ollama.com, not on your server) are hidden.
  - **Hugging Face:** a live search of GGUF models on [huggingface.co](https://huggingface.co/models?library=gguf), which Ollama downloads directly (`hf.co/user/repo`), top 100 matches. Kinds of models Ollama can't run (speech, music…) are hidden unless you choose **Audio & speech**.

  Filter by text, **type of use** (chat, runs commands/tools, reasoning, coding, reads images, embeddings, audio, image generation), **size** in parameters (under 1B up to over 100B), and **Fits this server** (enough memory); sort by popularity, last update, size or name.

  Each model shows a short **Best for** summary of what it's good at, its description, downloads/pulls, and its sizes. Models with several sizes (e.g. 8B / 70B / 405B) have a size picker. Sizes and memory marked ≈ are estimates for the usual 4-bit download. You can also type any name from the Ollama library or `hf.co/user/repo:quantization` from Hugging Face. Downloads show live progress and can be cancelled. Ollama doesn't do video generation, so there are no video models.
- **Installed models** shows size, parameters, quantization and capabilities (Tools, Vision, Thinking). It shows whether a model is loaded and how much memory it uses (and how much of that is on the GPU), with **Unload** to free that memory, **Delete**, and **Use in chat**.

**Use in chat** creates an AI connection under the provider **Ollama (Server Managed)**. You can also add one yourself in AI connections; its model box lists the models installed on the server. The chat assistant needs a model with **Tools** to run commands. Models without tools (e.g. DeepSeek-R1, Gemma 3) still work for questions and for the "Ask the AI" job action. To use Ollama running on *another* computer, use the provider **Ollama (another server)**. **Remove Ollama** uninstalls it, optionally with all downloaded models.

Existing installations need the updated root helper for install and service control (it's refreshed automatically with full access; otherwise the page shows the one-time command).

### Jobs, variables and mail accounts (Automation)

**Jobs** are automations: *when* something happens, *then* do something. A job has one or more triggers (it runs when any of them fires) and a list of actions that run in order. **Run now** tests a job straight away.

**History** shows the last 100 runs. Each run shows how long it took and how many actions succeeded, then what triggered it (with the values the trigger saw), then each action's result and duration. Expand **Details** to see:
- for an email: the full message
- for a webhook: the request and the response
- for a command: its full output
- for Ask the AI: the question and answer
- for Set a variable: the old and new value

**Check for updates** says whether an update was found (from which version to which, the versions it includes and their release notes) and whether it was installed. When it installs one, the app restarts, and the outcome ("now runs 1.6.0" or "failed and was rolled back") is added to the same run afterwards.

| Triggers (When) | |
|---|---|
| Schedule | Every N minutes, daily, weekly, monthly, or a cron expression |
| Disk space | A drive (or any drive) is over N % full |
| CPU usage / Memory usage | Over N %, optionally for at least N minutes |
| System load | 5-minute load per CPU core over N |
| Temperature | A sensor (or any sensor) over N °C |
| Service failed | A systemd service (or any service) has failed |
| Update available | A new SysAdmin.IA version is published (checked hourly) |
| App started | SysAdmin.IA starts (e.g. after a reboot) |
| Mail received | New unread mail in a mail account, optionally from someone or with a subject |

Condition triggers (disk, CPU, memory, load, temperature, service) fire when the problem starts. They fire again every N minutes while it lasts (0 = only once), and optionally once more when it's back to normal: in actions, `{{trigger.state}}` is `alert` or `recovered`. The engine checks every 20 seconds.

| Actions (Then) | |
|---|---|
| Send an email | Through a mail account, with subject and message filled from placeholders |
| Call a webhook | POST/GET to Slack, Teams, Discord, ntfy or any URL |
| Run a command | A shell command as the `sysadmin-ia` user; its output is available to later actions |
| Ask the AI | Ask an AI connection (no tools); its answer is `{{ai.answer}}` |
| Check for updates | Optionally install the update |
| Set a variable | Store a value, e.g. the time of the last alert |

**Placeholders** in any action text are replaced when the job runs. The editor lists the ones available (click one to insert it):
- `{{trigger.summary}}`, `{{trigger.value}}`, `{{trigger.mount}}`, `{{mail.subject}}`, … come from the trigger.
- `{{last.output}}` and `{{steps.2.output}}` come from earlier actions.
- `{{var.NAME}}` inserts one of your variables.
- `{{sys.hostname}}`, `{{sys.ip}}`, `{{sys.datetime}}`, `{{sys.url}}`, … are built in.

**Monitor values** are live measurements, read at the moment a job uses them. They're listed with their current values in **Variables → Monitor values**:

| Group | Placeholders |
|---|---|
| CPU & load | `{{mon.cpu_percent}}`, `{{mon.cpu_cores}}`, `{{mon.load_1}}` / `_5` / `_15`, `{{mon.load_per_core}}`, `{{mon.processes}}` |
| Memory | `{{mon.mem_percent}}`, `{{mon.mem_used}}`, `{{mon.mem_available}}`, `{{mon.mem_total}}`, `{{mon.swap_percent}}` |
| Disks | `{{mon.disk_max_percent}}` and `{{mon.disk_max_mount}}` (the fullest drive), and per drive `{{mon.disk.root.percent}}`, `.free`, `.used`, `.total` (`/mnt/backup` → `disk.mnt_backup`) |
| Temperatures | `{{mon.temp_max}}`, `{{mon.temp_max_sensor}}`, `{{mon.temp_avg}}`, `{{mon.temp_min}}`, and per sensor `{{mon.temp.package_id_0}}`, … |
| Activity & system | `{{mon.net_rx}}`, `{{mon.net_tx}}`, `{{mon.disk_read}}`, `{{mon.disk_write}}`, `{{mon.uptime}}`, `{{mon.uptime_days}}` |

Percentages and temperatures are plain numbers (`CPU {{mon.cpu_percent}}%`), and sizes and speeds come formatted (`21.0 GB`, `1.5 MB/s`).

**Variables** are named values you define once and reuse, e.g. `alert_email` and then `{{var.alert_email}}` in every job. Mark a variable **secret** for passwords and tokens: its value is never shown again and is masked in job logs.

**Mail accounts** let jobs send mail (SMTP) and read mail (IMAP). Presets fill in the servers for Gmail, Outlook / Microsoft 365, Yahoo, iCloud and a local mail relay. Use an *app password* where the provider requires one, and use **Test** to check the settings (optionally sending a test message).

Jobs run inside the app, so they only run while it's running. Commands run with the app user's permissions (sudo only with full access). Job changes and runs are recorded in the audit log.

### Console

The **Console** tab (admins only) is a terminal on the server in the browser: a login shell as the `sysadmin-ia` user, with full colors, resizing and copy/paste. With full access it can use `sudo`, just like Claude's commands. Unlike Claude's actions, commands typed there run immediately, without an approval step. Opening and closing a console session is recorded in the audit log, but what is typed is not. The **Update progress** session follows the installer's output while an update runs. To turn the console off, set `SAI_ALLOW_CONSOLE=false` and restart.

## Day-to-day

| Task | Command |
|---|---|
| Logs | `journalctl -u sysadmin-ia -f` |
| Restart | `sudo systemctl restart sysadmin-ia` |
| Add user | `sudo sysadmin-ia create-user bob --role admin` (or `viewer`) |
| Reset password | `sudo sysadmin-ia reset-password alice` |
| Audit log | `sudo sysadmin-ia audit -n 100` |
| List AI connections | `sudo sysadmin-ia ai-status` (manage them in the AI connections module) |
| Set / remove the Claude API key | `sudo sysadmin-ia set-api-key` / `sudo sysadmin-ia clear-api-key` |
| Change the default connection's model | `sudo sysadmin-ia set-model claude-sonnet-5-5` |
| Show / change the server name | `sudo sysadmin-ia server-name` / `sudo sysadmin-ia set-server-name NAME` (or Admin → Server name) |
| Reboot the server | `sudo sysadmin-ia reboot` (or Admin → Server name → Reboot server) |
| Check for / install updates | `sudo sysadmin-ia check-update` / `sudo sysadmin-ia update` (or Admin → Updates) |
| Roll back an app change Claude made | `sudo -u sysadmin-ia git -C /opt/sysadmin-ia log --oneline`, then `sudo -u sysadmin-ia git -C /opt/sysadmin-ia revert <sha>` and restart |
| Restore a file Claude edited | backups are in `/var/lib/sysadmin-ia/backups/<timestamp>/<original path>` |
| Uninstall | `sudo ./uninstall.sh` (add `--purge` to remove config, data and the user) |

## Configuration (`/etc/sysadmin-ia/sysadmin-ia.env`)

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` (and `OPENAI_API_KEY`, `XAI_API_KEY`, …) | – | Optional API keys used by AI connections whose key field is left empty |
| `SAI_MODEL` / `SAI_EFFORT` | `claude-opus-5-5` / `high` | Only used once, when an older install's Claude key is turned into the first AI connection |
| `SAI_MAX_TOKENS` | `64000` | Max output tokens per response |
| `SAI_FALLBACKS` | `default` | Server-side refusal fallback (another model answers if one declines). Empty disables it |
| `SAI_HOST` | `0.0.0.0` | Listen address |
| `SAI_PORT` | `8765` | HTTPS port (or the only, plain-HTTP port if no TLS certificate is configured) |
| `SAI_HTTP_PORT` | `8080` | Extra plain-HTTP port served alongside HTTPS; `0` disables it |
| `SAI_TLS_CERT` / `SAI_TLS_KEY` | self-signed | Replace with real certificates (for example Let's Encrypt) if you have a domain |
| `SAI_SESSION_HOURS` | `12` | Login session lifetime |
| `SAI_COMMAND_TIMEOUT` | `300` | Seconds before a command is killed |
| `SAI_ALLOW_AUTO_APPROVE` | `true` | Whether users can turn on "Auto-approve actions" in the chat |
| `SAI_ALLOW_CONSOLE` | `true` | Whether admins get the web **Console** (a shell on the server). `false` removes it |
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
browser ─HTTP(S)/WebSocket─► FastAPI (app/main.py) ──► AI connection (app/providers.py: Claude API / Vertex / Bedrock, or an OpenAI-compatible API) — streaming, tool use
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
cp .env.example .env   # API keys optional here; add connections in the AI connections module
python -m app.cli create-user admin
python -m app          # http://127.0.0.1:8765
```
