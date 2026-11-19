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

Change the name later in **Network → Names & websites → Server name**, or with `sudo sysadmin-ia set-server-name NAME` (`sudo sysadmin-ia server-name` shows it and the LAN addresses). Renaming:

- sets the hostname and the `127.0.1.1` entry in `/etc/hosts`
- re-announces the new name via mDNS, and advertises the web UI as a network service
- replaces the self-signed certificate so it is valid for the new name, then restarts the app (browsers show the certificate warning once more). A certificate you installed yourself is never replaced

No reboot is needed: the new name works right away. If a device on the network still uses the old name (for example, a router that registered it), use **Reboot server** in the same card, or `sudo sysadmin-ia reboot`.

### Time zone

**Admin → Time zone** shows the server's time zone, its current time and whether the clock is synchronized with internet time (NTP). Type a city to pick another zone (e.g. `Europe/Madrid`), or click **Use my time zone** to copy your browser's. It's changed through the app's helper (`timedatectl set-timezone`). The server clock, logs and job schedules switch to the new local time right away, and containers from the App catalog get it as their `TZ` setting.

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

- **Choosing a version:** when several newer versions are out, a **Version to install** list next to the button lets you install one of them instead of the latest (the latest is preselected and includes everything before it).
- **All releases:** under the update notes, **All releases** lists every published version, newest first, marked Newer / Installed / Older. Each name links to its release page on GitHub. **Install** installs a newer one; **Downgrade** goes back to an older one after a confirmation. A downgrade runs that version's own installer exactly like an update: your settings, users and data are kept (the database only ever gains columns, which older versions ignore), but features added later disappear until you update again.

Updates are cumulative: the latest version is the **highest version number** on the chosen **update channel** (Admin → Updates), and an update installs it directly, whatever the installed version.

| Channel | Offers |
|---|---|
| **Public** (default) | Published GitHub releases only (no drafts, no pre-releases). Recommended. |
| **Developer** | Every version tag pushed to the repository: releases, pre-releases and tags without a release. It also shows the latest push to the `main` branch and whether it's tagged. Only tagged versions can be installed; with no version tags at all, the `VERSION` file on `main` is used. |

Version tags are `vX.X.X` or `vX.X.X.X` (numbers only, e.g. `v1.3.3`, `v1.3.3.1`). They compare number by number, leftmost first: `v1.2.10` > `v1.2.9`, `v1.3.3.1` > `v1.3.3` (= `v1.3.3.0`), `v2.0.0` > `v1.99.0`. Other tags (`v1.3`, `1.3.3`, `v1.3.3-beta`…) are ignored. To publish a version, push a tag such as `v1.3.3`, and create a GitHub release from it for the Public channel. Switching channels re-checks right away. Switching from Developer back to Public may leave you on a newer version than the latest release; you can then downgrade from the list.

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
- **Your profile card** (top right) shows your picture and username. Click it for **Edit profile** (picture, username, display name, email), **Change password**, **Appearance & language**, and **Sign out**. Pictures are cropped to a square in the browser and stored in the app database.

### Appearance and language

Profile card → **Appearance & language**. Each user has their own settings, saved to their account so they follow them to other devices (and remembered in the browser, so the page is drawn correctly from the first moment):

- **Language**: English (default), Español, Português (Brasil), Français, Deutsch, Italiano, 日本語 (Japanese) or 简体中文 (Simplified Chinese). The whole interface switches immediately, including the job, app and model catalogs and most server messages, and the chat assistant replies in that language (unless you write to it in another one). The sign-in page has its own language picker.
- **Theme**: Automatic (follows your device's light/dark setting), Light, Dark, or Black (true black, for OLED screens). The sun/moon button in the top bar switches between light and dark in one click.
- **Accent colour**: orange, blue, green, purple, pink, teal or graphite (buttons, links, highlights, charts).
- **Text size**: Small, Normal, Large, Extra large (scales the whole interface).
- **High contrast** (stronger text and borders) and **Reduce motion** (no animations or sliding panels).

Names you choose (files, chats, containers, jobs…), command output, logs and code are never translated. The translations were written for this app (not by a translation service), so a native speaker may want to polish some wording; each language is two plain files, `app/static/i18n/<code>.js` (interface) and `<code>-server.js` (catalogs and server messages), with the English text as the key. Adding a language: copy the Spanish files `es.js` and `es-server.js`, translate the values, and add the language code to `LANGS` in `app/static/prefs.js` and `app/user_prefs.py`.

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
  - its settings, with descriptions, and the time zone set to the server's;
  - anything special it needs: the server's network for Home Assistant and Plex, kernel network settings for WireGuard, the Docker socket for Portainer.

  **Every setting the app needs is loaded with a default value** and marked `*` when it's required: for example the PostgreSQL superuser `POSTGRES_USER` (default `postgres`, as in the image's documentation) and `POSTGRES_PASSWORD`, MariaDB/MySQL root passwords, and admin users and passwords for MongoDB, RabbitMQ, Grafana, code-server and Pi-hole. Passwords start with a **freshly generated random password**, shown so you can copy it (a note at the top of the form lists them); type your own or click **Generate** for another. Required settings can't be left empty. A few things have no sensible default and **must be chosen by you**, listed at the top of the form: the pgAdmin login email, and the folder with your movies or music for Jellyfin, Plex, Navidrome, Audiobookshelf and File Browser.

  The same settings load when you use **New container** and type a catalog app's image yourself, with any tag: for example `postgres:16` gets the PostgreSQL settings above (for version 16).

  Notes from each app's documentation are shown at the top of the form. Apps you already run show **Running as …** and an **Open** button. **Install again** adds another copy with its own name and ports (e.g. `postgres-2` on port 5433).
- **Edit** changes ports, folders, settings or the image version (**Download the newest version** updates the app). Docker can't change a container's ports or folders in place, so the container is recreated. The old one is kept until the new one starts, and put back if the new one fails. Data in shared folders and volumes is kept.
- **Images:** download, update or delete images, search Docker Hub, and create a container from an image.
- **Stacks:** several containers in one Docker Compose file (e.g. an app and its database). Paste the `docker-compose.yml` from an app's documentation, with optional `.env` values. It's checked before saving. A button inserts a folder path from your drives. You can **Start**, **Update** (pull newer images and recreate), **Stop**, view **Logs**, **Edit** or **Delete** a stack. Stacks are stored in `/var/lib/sysadmin-ia/stacks/NAME/`. Stacks started outside the app are listed but read-only.
- **Volumes** and **Networks:** create and delete them. You can connect a container to a network, and containers on the same network reach each other by name. A network can be internal (no internet access).
- **Clean up:** shows how much space images, containers, volumes and the build cache use. It can delete stopped containers, unused images, networks and build cache, and optionally unused volumes.

Downloads and container changes run in the background with progress shown at the top of the page. Existing installations need the updated root helper for install and service control (refreshed automatically with full access; otherwise the page shows the one-time command).

### Network

The **Network** module (Server group) shows what's on your LAN, talks to your router, and publishes self-hosted services on the internet.

**Devices.** **Scan now** finds every device that's switched on and identifies it, with no admin rights needed:
- **How devices are found:** a quick contact to each address of the server's network, then the ARP table.
- **What's identified:**
  - the maker, from the network card's address (the IEEE list is downloaded once);
  - names devices announce on the LAN (mDNS, UPnP, Windows names), and the router's DNS;
  - about 100 common ports (web pages on any port, SSH, mail, databases, file sharing, printers, cameras, media servers…).
- **Services:** whatever answers on an open port is identified by its reply (web pages and their titles, SSH, MySQL/MariaDB, PostgreSQL, Redis, MongoDB, FTP, mail, VNC…) and shown as a tag on the device:
  - **web pages** open in a new browser tab;
  - **SSH** opens a new tab in the **Console** with an SSH session to the device (you choose the user name; the password or key passphrase is typed in the Console). The device's key is remembered the first time; if it changes later, ssh refuses to connect;
  - **other services** (databases…) copy their address when clicked.
- **Scan all ports** checks all 65,535 ports of one device (about a minute) to find services on unusual ports.
- **Kick and block** (OpenWrt and MikroTik routers): **Kick** disconnects a Wi-Fi device now (it can reconnect after about a minute). **Block** cuts its internet and router access (no address, no DNS) and keeps it off the router's Wi-Fi; **Unblock** lets it back. Blocked devices are listed in **Router & DHCP**. This server, the router and the device you're using can't be blocked. Devices on the same switch or Wi-Fi can still talk to a blocked device directly.
- **Kind guessed:** router, computer, phone, TV, media player, speaker, printer, NAS, camera, smart device, home-automation hub, console, server…
- **Your changes:** rename a device, change its kind, add notes, or mark it as known. New devices are flagged until you do.
- **Private addresses:** phones and laptops often use a private (randomized) Wi-Fi address, so they show without a maker.
- **Watching:** the network is re-checked every 15 minutes (**Settings**), which powers the job triggers **New device on the network** and **Device offline**.

**Names & websites.** Names instead of addresses, for this server and everything on the LAN.
- **Server name:** the server's own name (`NAME.local`), rename and reboot (moved here from Admin).
- **Names on the network:** give a device (or this server) a name.
  - Names ending in **`.local`** (e.g. `nas.local`) are announced by this server over mDNS (`avahi-publish`, package avahi-utils). They work on any network, with no router support; phones, Macs, Windows 10+ and Linux understand them. If another device already uses the name, the row says so.
  - **Other names** (e.g. `nas.home.lan`) are stored as DNS entries in the router (OpenWrt, MikroTik). Devices must use the router as their DNS server (the usual setup).
  - Pick the device from a dropdown of this server and all known devices (from the last scan), or choose **Other address…** and type one.
  - Reserve the device's address (**Router & DHCP**) so the name keeps pointing to it.
  - **Ports (optional):** map a port of the name to a port of the device, e.g. `website1.local:80 → 192.168.1.10:8080` and `website2.local:80 → 192.168.1.10:8081`.
    - A name alone can't change ports, so the mapping goes through this server's proxy (Caddy): the name then points to this server, and the proxy passes each visit on to the device's port.
    - One name can have several mappings, e.g. `website1.local:8082 → 192.168.1.10:8081`. They show under the name and in **Websites on this server**.
    - Websites (HTTP) only; needs Docker.
    - Removing the last mapping points the name back where it was. Removing the name removes its mappings.
- **Websites on this server:** many sites and apps on the same port. Each site gets a name (e.g. `photos.local` → the Immich container, `nas.local` → the NAS's web page, `admin.local` → SysAdmin.IA itself). A small proxy (Caddy, container `sai-caddy`) listens on port 80 (and 443) and sends each visit to the right app by name.
  - **Goes to** is a dropdown, grouped as SysAdmin.IA itself, apps on this server (container ports), web pages found on the network, and any known device. For a device you then choose its port (and whether its page uses HTTPS). **Other address and port…** lets you type any address.
  - The name is created too, pointing to this server (optional).
  - Each website answers on port 80 (443 with HTTPS) unless you pick another **Port**. Several websites share a port as long as their names differ. Ports already used by another program, or SysAdmin.IA's own port, are refused. If the firewall keeps the port closed, you're told to open it in **Ports**.
  - **HTTPS** uses certificates from this server's own certificate authority. Browsers warn until you trust it; the tab has a link to download it.
  - **SysAdmin.IA itself** can be a website (pick “app”, or type its address and port, e.g. `192.168.1.5:8765`), e.g. `a.local` on port 80. HTTPS in front is recommended (otherwise passwords cross the network unencrypted), but plain HTTP works too: the app trusts the proxy's forwarded headers (only from this machine), so signing in and the audit log's addresses are right either way.
  - **Edit** any name or website with its pencil button. Renaming a name keeps its port mappings. Giving a name with mappings a new address moves the mappings to that device. A website's name, target, port and HTTPS can all be changed.
  - The proxy is shared with **Publish on the internet** (Caddy method): public and local sites live in the same configuration. It's removed when nothing needs it. Needs Docker, and ports 80/443 not used by another program.

**Ports.** This server's own firewall: which of its ports other devices can connect to.
- **Uses what's installed:** ufw (Debian/Ubuntu), firewalld (Fedora/RHEL/openSUSE), or, if neither is installed, a small nftables table managed by SysAdmin.IA that is loaded at boot.
- **Closed by default (recommended):** turn the firewall on or close ports by default with one button. SysAdmin.IA's own port, SSH and mDNS (for `name.local`) are opened first, so you aren't locked out. You can switch back to open by default.
- **Open ports table:** each rule with its port, protocol, whether something is using it, and what (SSH server, a container, nginx, PostgreSQL…). **Open port** adds a port or a range (TCP, UDP or both, with a note). Delete a row to close the port. The app's own port can't be closed from here; closing the SSH port asks first. Rules made elsewhere (e.g. firewalld rich rules) are shown but not edited.
- **Ports in use:** every program waiting for connections, whether other devices can reach it (open, closed, only some addresses), and an **Open port** button for closed ones. Ports Docker publishes for containers are marked: Docker routes them around the host firewall, so they stay reachable even when closed here.
- Needs the refreshed root helper; if it's missing, the tab shows the one-time setup command.

**Router & DHCP.** Connect SysAdmin.IA to your router (**Detect my router** suggests the type):

| Router type | What SysAdmin.IA can do |
|---|---|
| Automatic (UPnP) — most home routers | Port forwarding and reading the public address. It can't change DHCP. UPnP must be on in the router. |
| OpenWrt (LuCI) | Everything: DHCP leases, reserved addresses, address range, lease time, DNS servers handed out, port forwarding, kicking and blocking devices, local DNS names |
| MikroTik RouterOS 7 (REST API: IP → Services → www/www-ssl) | Everything, as for OpenWrt |
| Other | A link to the router's admin page |

Reserve an address straight from a device (**Reserve address**). The router password is stored on the server and never shown again.

**Publish on the internet.** The tab shows your public address and the router's internet address. If they differ, your provider shares addresses between customers (carrier-grade NAT) and port forwarding can't work. Two ways to publish:

- **Cloudflare Tunnel (recommended).** Works with any internet connection, opens nothing on the router, and keeps your address hidden.
  - **You need:** a free Cloudflare account with your domain, and an API token with *Account · Cloudflare Tunnel · Edit* and *Zone · DNS · Edit*.
  - **SysAdmin.IA does the rest:** it creates the tunnel and runs its connector as the container `sai-cloudflared`. For each service you publish, it adds the route and a proxied DNS name such as `photos.example.com`.
- **Port forwarding + HTTPS.** Ports 80 and 443 are forwarded to this server, where Caddy (container `sai-caddy`) gets free Let's Encrypt certificates.
  - **Router:** the forwards are created automatically when the router connection can do it.
  - **DNS:** each name must point to your public address. **Dynamic DNS** (Cloudflare or DuckDNS) keeps that up to date when the address changes.

**Publish a service:**
1. Pick a container port or a web page found on the LAN, or type an address such as `127.0.0.1:8080`.
2. Give it a public name.

Anyone on the internet can then open it, so publish only apps with their own login and keep them updated. Publishing SysAdmin.IA itself needs an explicit confirmation. Both ways need Docker (Containers module).

### Processes

SysAdmin.IA runs as separate processes, so one part can stop or crash without taking the others down:

| Process | Does | While it's stopped |
|---|---|---|
| Web interface | The pages and API (chat, console, monitor, every module's page) | Nothing to see: start it with `sudo sysadmin-ia process start web` |
| Jobs scheduler | Checks DAG triggers and runs DAG tasks | No job runs; runs started by hand wait in the queue |
| Network watcher | Periodic network scans, dynamic DNS updates, announcing `.local` names | The Network pages work, but nothing happens in the background |

A supervisor (the service's main process) starts them.
- **Crashes:** a process that crashes is restarted after a short pause. After 5 crashes in 2 minutes it's left "failed" until someone starts it.
- **Hangs:** each process sends a sign of life every 10 seconds; one that stops answering shows as "not responding".
- **The Processes page** (Server group, admins) shows each process with its state, process id, uptime, CPU, memory, restarts, last exit and recent output. It has **Start**, **Stop** and **Restart** for each, and **Restart everything**. A process stopped there stays stopped, also after a reboot, until it's started again. The web interface can only be restarted from the page; it can be stopped from the command line.
- **When a process is down:** the modules that depend on it (Jobs, Network) show a notice saying what isn't working. Admins get a link to Processes; other users are asked to contact an admin. A red dot on **Processes** means something isn't running.
- **Command line:** `sudo sysadmin-ia processes` shows them; `sudo sysadmin-ia process start|stop|restart web|jobs|network` controls them.

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

**Jobs** are DAGs (directed acyclic graphs), the way Apache Airflow does it:
- **Triggers** say *when* a DAG runs: a schedule or an event (full disk, new device, new mail… see the table below). It runs when any of them fires. A DAG without triggers only runs when you start it (**Run now**).
- **Tasks** say *what* it does. Each task is an *operator* (send an email, run a command, ask the AI…) with its settings. Connections between tasks set the order: a task runs once the tasks before it have finished. Tasks that don't depend on each other run at the same time (up to 4 per run).
- **Per task:** retries (with a wait between tries), a time limit (`execution_timeout`), and a **trigger rule**:
  - `all_success` (default): all the tasks before it succeeded;
  - `all_done`: they all finished, whatever the result;
  - `none_failed`: none of them failed (skipped ones are fine);
  - `one_success` / `one_failed` / `all_failed`: e.g. `one_failed` runs a "send a warning" task only when something before it failed.
- **Flow operators:** **Condition** continues only if a value meets a test (not empty, equals, contains, greater than…); when it's false, everything after it is skipped (Airflow's ShortCircuitOperator). **Wait** pauses. **Join** does nothing and is a meeting point for branches. **Start** and **End** do nothing either (Airflow's EmptyOperator): they mark where the DAG begins and ends. Give End the trigger rule `all_done` to have it run whatever happened before it.
- **Code operators:** **Run Python** runs Python code with the base Python (the server's `python3`) or one of your **Python environments** (below). **Run console** runs a multi-line shell script (bash or sh), by default with `set -e` (and `pipefail` in bash) so it stops at the first failing command, plus optional `NAME=value` environment variables. In both, what the code prints is the task's output (`{{tasks.<id>.output}}`), a non-zero exit fails the task, and the environment variable `SAI_CONTEXT` holds the run as JSON (trigger values, dag, run, and every earlier task's output). You can also set arguments (Python's `sys.argv[1:]`), a working folder (default: the app's data folder) and a time limit.

**The editor** (New DAG or Edit):
- **Graph:** drag operators from the list on the left onto the canvas (or click one: it's added after the selected task and connected to it). Drag a task to move it. Drag from a task's right dot to another task's left dot to make the second run after the first; loops are refused. Click a connection, then press Delete (or its ×) to remove it. The panel on the right edits the selected task: its id, settings, retries, time limit, trigger rule, and the placeholders it can use. **Arrange** lays the tasks out automatically.
- **Code:** the same DAG as Airflow-style Python. Edit it and switch back to Graph (or save) to apply. The code is only read, never run: just plain values, `tasks.<operator>(…)`, `triggers.<trigger>(…)` and dependencies (`a >> b`, `a >> [b, c]`, `[a, b] >> c`, `a << b`, `set_downstream`). Mistakes are reported with their line number.

```python
from sysadmin_ia import DAG, triggers, tasks

with DAG(dag_id="disk_alert", description="Warn me when a disk is almost full",
         triggers=[triggers.disk_usage(threshold=90, repeat_minutes=60)]) as dag:
    biggest = tasks.run_command(task_id="biggest", command="du -xh / --max-depth=2 | sort -rh | head -20")
    found = tasks.short_circuit(task_id="found", value="{{tasks.biggest.output}}", op="not_empty")
    explain = tasks.ask_ai(task_id="explain", prompt="What can I delete?\n{{tasks.biggest.output}}", retries=2, retry_delay=30)
    mail = tasks.send_mail(task_id="mail", account=1, to="{{var.alert_email}}", subject="{{trigger.summary}}",
                           body="{{tasks.explain.output}}")
    biggest >> found >> explain >> mail
```

**The DAG page:**
- **Graph:** the DAG with each task coloured by its state in the chosen run (success, failed, running, skipped, upstream failed, up for retry). Click a task to see its message, output and details.
- **Runs:** a grid like Airflow's, with one column per run (newest first) and one row per task. Below it is the list of runs: why each one started, when, and how long it took. Running or queued runs can be stopped.
- **Code:** read-only.
- **Controls:** each DAG can be paused (the switch on its card) or started by hand. At most N runs of a DAG happen at once (default 1); later ones wait in the queue. The last 100 runs are kept.

**Check for updates** says whether an update was found and whether it was installed. When it installs one, the processes restart, and the outcome ("now runs 1.6.0" or "failed and was rolled back") is added to the run's note afterwards.

Existing jobs from before were turned into DAGs automatically, once: their triggers stay, their actions became a chain of tasks, and `{{steps.N.output}}` became `{{tasks.<id>.output}}`.

| Triggers (When) | |
|---|---|
| Schedule | Every N minutes, daily, weekly, monthly, or a cron expression |
| Disk space | A drive (or any drive) is over N % full |
| CPU usage / Memory usage | Over N %, optionally for at least N minutes |
| System load | 5-minute load per CPU core over N |
| Temperature | A sensor (or any sensor) over N °C |
| Service failed | A systemd service (or any service) has failed |
| Container stopped | A container (or any container seen running) stops, crashes, keeps restarting or is unhealthy, optionally for N minutes |
| Docker stopped | The Docker service isn't running |
| Local AI stopped | Ollama is installed but not answering |
| AI usage | An AI connection used over N % of its monthly limit, or over N tokens this month |
| New device on the network | A device never seen before joins the LAN (optionally ignoring devices marked as known) |
| Device offline | A chosen device (NAS, camera, printer…) stops answering, optionally for N minutes |
| Public address changed | The network's internet (public) IP address changes (checked every 5 minutes) |
| Update available | A new SysAdmin.IA version is published (checked hourly) |
| App started | SysAdmin.IA starts (e.g. after a reboot) |
| Mail received | New unread mail in a mail account, optionally from someone or with a subject |

Condition triggers (disk, CPU, memory, load, temperature, service, containers, Docker, Local AI, AI usage) fire when the problem starts. They fire again every N minutes while it lasts (0 = only once), and optionally once more when it's back to normal: in actions, `{{trigger.state}}` is `alert` or `recovered`. The engine checks every 20 seconds.

| Operators (tasks) | |
|---|---|
| Condition / Wait / Join | Flow control: continue only if…, pause, join branches |
| Start / End | Do nothing; mark where the DAG begins and ends |
| Run Python | Python code, with the base Python or a Python environment |
| Run console | A shell script (bash or sh), several lines, with optional environment variables |
| Send an email | Through a mail account, with subject and message filled from placeholders |
| Call a webhook | POST/GET to Slack, Teams, Discord, ntfy or any URL |
| Run a command | A shell command as the `sysadmin-ia` user; its output is available to later actions |
| Start, stop or restart a container | A chosen container, or the one a "Container stopped" trigger found |
| Update a container | Pull its image's newest version; if there is one, recreate the container with the same ports, folders and settings |
| Start, stop or update a stack | A Docker Compose stack created in Containers |
| Clean up Docker | Delete stopped containers, unused images, networks, build cache (and optionally unused volumes) |
| Ask the AI | Ask an AI connection (no tools); its answer is `{{ai.answer}}` |
| Download or update a local AI model | Pull a model into Local AI (Ollama) and wait until it's done |
| Start, stop or restart Local AI | Control the Ollama service |
| Scan the network | Look for devices on the LAN now (new ones are identified) |
| Check for updates | Optionally install the update |
| Set a variable | Store a value, e.g. the time of the last alert |

In the editor, the triggers and the operators are grouped by the module they belong to (Notify, Server, Containers, AI, Network, Flow…).

**Placeholders** in any task's text are replaced when it runs. The editor lists the ones the selected task can use (click one to insert it):
- `{{trigger.summary}}`, `{{trigger.value}}`, `{{trigger.mount}}`, `{{mail.subject}}`, … come from the trigger.
- `{{tasks.<task_id>.output}}` (like Airflow's XCom) is the output of any task before it, and `{{tasks.<task_id>.exit_code}}` is a command's exit code. `{{last.output}}` is the output of the task right before it (the one that finished last).
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

DAGs run in the **jobs process** (see **Processes**), so they only run while it's running; if it's stopped, the Jobs page says so and runs you start wait in the queue. Commands run with the app user's permissions (sudo only with full access). DAG changes and runs are recorded in the audit log.

**Python environments** (Automation → Python environments, admins only) are virtual environments for the Run Python task, each with its own packages and Python version:
- **New environment:** a name (tasks refer to it by name, so it can't be renamed), the Python to use, optional packages (like `pip install`: `requests pandas>=2.2`) and a description. Creating it and installing packages run in the background, with their output shown live on the page.
- **Python versions:** the base Python or any `python3.X` installed on the server work out of the box (`python -m venv`). For other versions, click **Install uv**: [uv](https://github.com/astral-sh/uv) is installed into the app's own folder (not system-wide), and then you can type any version (e.g. `3.9` or `3.13`); it's downloaded the first time into the data folder.
- **Packages:** list, filter, install (optionally upgrading) and remove. Package options such as `-r` or `--index-url` aren't accepted.
- **Edit** changes the description. **Delete** removes the environment and its packages, and warns about DAGs that use it (their Run Python tasks fail until you pick another environment).

Environments live in `<data folder>/pyenvs/<name>`; uv lives in `<data folder>/tools/uv` and the Pythons it downloads in `<data folder>/pythons`.

### Console

**Several consoles at once:** the **+** button next to the tabs opens another shell (Shell 2, Shell 3… up to 9 shells). Each tab is its own session; switching tabs keeps them running. Double-click a tab to rename it, and click its **×** to close it (that ends its session). SSH sessions opened from Network → Devices are tabs too.

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
| Show / change the server name | `sudo sysadmin-ia server-name` / `sudo sysadmin-ia set-server-name NAME` (or Network → Names & websites → Server name) |
| Reboot the server | `sudo sysadmin-ia reboot` (or Network → Names & websites → Reboot server) |
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
| `SAI_UPDATE_BRANCH` | `main` | Branch shown as "latest push" on the Developer channel, and whose `VERSION` file is used when the repository has no version tags |
| `SAI_UPDATE_CHANNEL` | `public` | Default update channel (`public` or `developer`); a choice saved in Admin → Updates wins |
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

- `app/supervisor.py`: the service's main process (`python -m app`). It starts the worker processes (`python -m app.worker web|jobs|network`), restarts them when they crash, and takes start/stop/restart requests from the web interface and the CLI on 127.0.0.1, with a token kept in the data folder. `app/procs.py` is the client side. The processes share the SQLite database (WAL mode). The web interface hands work to the jobs process through it (queued runs, stop requests).
- `app/dags.py`: jobs as DAGs: the model, the Airflow-style code view (parsed with Python's `ast`, never executed), and the scheduler/executor that runs in the jobs process.
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
python -m app          # http://127.0.0.1:8765 (supervisor + web, jobs and network processes)
python -m app --single # everything in one process (simpler to debug; Processes can't control the parts)
```
