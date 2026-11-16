"""Common apps for the Containers module: images with ready-made settings.

Each app pre-fills the "New container" form:
  ports   – container ports with a label; `host` = preferred server port (default: the same).
  folders – data folders inside the container; stored on a drive as <drive>/appdata/<name>/<sub>.
            kind "media" = an existing folder of yours (e.g. movies): must be chosen before creating.
            kind "host" = a fixed server path in `source` (e.g. the Docker socket; needs "allow system folders").
  env     – settings; `required` must be filled before creating (empty to start with), `generate` = offer a Generate button,
            `secret` = shown as a password.
  network – "host" for apps that need the server's network directly (device discovery).
  open    – what to open in the browser: "/path" on the first port, ":PORT/path" on that container port,
            "https://:PORT/path" for HTTPS (None = not a web app).
  privileged, sysctls, command – passed to the container as is.
Values come from each image's official documentation (checked against Docker Hub, Nov 2026).
"""
from __future__ import annotations

CATEGORIES = [
    ("db", "Databases"), ("admin", "Database tools"), ("web", "Web servers & proxies"),
    ("media", "Media"), ("files", "Files & sync"), ("home", "Home & network"),
    ("monitor", "Monitoring & management"), ("dev", "Development & automation"), ("security", "Security"),
]

_TZ = {"key": "TZ", "value": "Etc/UTC", "label": "Time zone, e.g. Europe/Madrid"}


def _pw(key: str, label: str = "Password") -> dict:
    # required and empty: the user must type one (or click Generate)
    return {"key": key, "value": "", "label": label, "required": True, "secret": True, "generate": True}


APPS = [
    # ---- databases
    {"id": "postgres", "cat": "db", "title": "PostgreSQL", "image": "postgres:17", "logo": "🐘",
     "about": "Powerful open-source SQL database. Used by many apps (Nextcloud, Gitea, n8n…).",
     "ports": [{"container": 5432, "label": "Database connections"}],
     "folders": [{"target": "/var/lib/postgresql/data", "sub": "data", "label": "Database files"}],
     "env": [{"key": "POSTGRES_USER", "value": "", "label": "Superuser name", "required": True, "placeholder": "e.g. admin"},
             _pw("POSTGRES_PASSWORD", "Superuser password"),
             {"key": "POSTGRES_DB", "value": "", "label": "Database to create (optional; default = user name)"}],
     "open": None, "notes": "Connect with host = this server, the port below, and the superuser name and password you choose here."},
    {"id": "mariadb", "cat": "db", "title": "MariaDB", "image": "mariadb:11", "logo": "🦭",
     "about": "MySQL-compatible SQL database, community developed.",
     "ports": [{"container": 3306, "label": "Database connections"}],
     "folders": [{"target": "/var/lib/mysql", "sub": "data", "label": "Database files"}],
     "env": [_pw("MARIADB_ROOT_PASSWORD", "Root password"),
             {"key": "MARIADB_DATABASE", "value": "", "label": "Database to create (optional)"},
             {"key": "MARIADB_USER", "value": "", "label": "Extra user (optional)"},
             {"key": "MARIADB_PASSWORD", "value": "", "label": "Extra user's password (optional)", "secret": True, "generate": True}],
     "open": None},
    {"id": "mysql", "cat": "db", "title": "MySQL", "image": "mysql:8.4", "logo": "🐬",
     "about": "The classic SQL database (Oracle's long-term-support release).",
     "ports": [{"container": 3306, "label": "Database connections"}],
     "folders": [{"target": "/var/lib/mysql", "sub": "data", "label": "Database files"}],
     "env": [_pw("MYSQL_ROOT_PASSWORD", "Root password"), {"key": "MYSQL_DATABASE", "value": "", "label": "Database to create (optional)"}],
     "open": None},
    {"id": "mongo", "cat": "db", "title": "MongoDB", "image": "mongo:8", "logo": "🍃",
     "about": "Document (NoSQL) database storing JSON-like records.",
     "ports": [{"container": 27017, "label": "Database connections"}],
     "folders": [{"target": "/data/db", "sub": "db", "label": "Database files"}],
     "env": [{"key": "MONGO_INITDB_ROOT_USERNAME", "value": "", "label": "Admin user name", "required": True, "placeholder": "e.g. admin"}, _pw("MONGO_INITDB_ROOT_PASSWORD", "Admin password")],
     "open": None, "notes": "MongoDB 5+ needs a CPU with AVX (most x86 CPUs from 2011 on)."},
    {"id": "redis", "cat": "db", "title": "Redis", "image": "redis:7", "logo": "🟥",
     "about": "In-memory key-value store, used as a cache or message queue.",
     "ports": [{"container": 6379, "label": "Connections", "local": True}],
     "folders": [{"target": "/data", "sub": "data", "label": "Saved data"}],
     "env": [], "command": "redis-server --save 60 1 --appendonly yes", "open": None,
     "notes": "Redis has no password by default, so its port is reachable from this server only. Change “Reachable from” if you add a password."},
    {"id": "valkey", "cat": "db", "title": "Valkey", "image": "valkey/valkey:8", "logo": "🔑",
     "about": "Open-source fork of Redis (Linux Foundation), drop-in compatible.",
     "ports": [{"container": 6379, "label": "Connections", "local": True}],
     "folders": [{"target": "/data", "sub": "data", "label": "Saved data"}], "env": [],
     "command": "valkey-server --save 60 1 --appendonly yes", "open": None},
    {"id": "influxdb", "cat": "db", "title": "InfluxDB 2", "image": "influxdb:2", "logo": "📈",
     "about": "Time-series database for metrics and sensor data, with a web UI.",
     "ports": [{"container": 8086, "label": "Web UI and API"}],
     "folders": [{"target": "/var/lib/influxdb2", "sub": "data", "label": "Data"}, {"target": "/etc/influxdb2", "sub": "config", "label": "Configuration"}],
     "env": [], "open": "/", "notes": "Open the web UI to create the first user and bucket."},
    # ---- database tools
    {"id": "adminer", "cat": "admin", "title": "Adminer", "image": "adminer:latest", "logo": "🗄️",
     "about": "Tiny web page to browse and edit PostgreSQL, MySQL/MariaDB, SQLite… databases.",
     "ports": [{"container": 8080, "label": "Web page"}], "folders": [], "env": [], "open": "/",
     "notes": "To reach a database container, use this server's address as the server (e.g. 192.168.1.10:5432)."},
    {"id": "pgadmin", "cat": "admin", "title": "pgAdmin 4", "image": "dpage/pgadmin4:latest", "logo": "🐘",
     "about": "Full web interface for managing PostgreSQL.",
     "ports": [{"container": 80, "host": 5050, "label": "Web page"}],
     "folders": [{"target": "/var/lib/pgadmin", "sub": "data", "label": "Settings and saved servers"}],
     "env": [{"key": "PGADMIN_DEFAULT_EMAIL", "value": "", "label": "Login email", "required": True, "placeholder": "you@example.com"},
             _pw("PGADMIN_DEFAULT_PASSWORD", "Login password")],
     "open": "/", "notes": "The data folder must be writable by user 5050 inside the container: if pgAdmin fails to start, run "
                           "“sudo chown -R 5050:5050 FOLDER” on the server."},
    # ---- web
    {"id": "nginx", "cat": "web", "title": "nginx", "image": "nginx:latest", "logo": "🌐",
     "about": "Fast web server: serve a static website from a folder on your drives.",
     "ports": [{"container": 80, "host": 8080, "label": "Website"}],
     "folders": [{"target": "/usr/share/nginx/html", "sub": "html", "label": "Website files", "readonly": True}],
     "env": [], "open": "/", "notes": "Put an index.html in the website folder."},
    {"id": "httpd", "cat": "web", "title": "Apache httpd", "image": "httpd:2.4", "logo": "🪶",
     "about": "The Apache web server, serving a website from a folder.",
     "ports": [{"container": 80, "host": 8080, "label": "Website"}],
     "folders": [{"target": "/usr/local/apache2/htdocs", "sub": "htdocs", "label": "Website files", "readonly": True}],
     "env": [], "open": "/"},
    {"id": "npm", "cat": "web", "title": "Nginx Proxy Manager", "image": "jc21/nginx-proxy-manager:latest", "logo": "🔀",
     "about": "Put your apps behind nice addresses (app.example.com) with free HTTPS certificates, from a web UI.",
     "ports": [{"container": 80, "label": "HTTP (websites)"}, {"container": 443, "label": "HTTPS (websites)"},
               {"container": 81, "label": "Admin web page"}],
     "folders": [{"target": "/data", "sub": "data", "label": "Settings"}, {"target": "/etc/letsencrypt", "sub": "letsencrypt", "label": "Certificates"}],
     "env": [], "open": ":81/", "notes": "Needs ports 80 and 443 free on the server. First login: admin@example.com / changeme."},
    {"id": "caddy", "cat": "web", "title": "Caddy", "image": "caddy:2", "logo": "🔒",
     "about": "Web server and reverse proxy with automatic HTTPS.",
     "ports": [{"container": 80, "label": "HTTP"}, {"container": 443, "label": "HTTPS"}],
     "folders": [{"target": "/srv", "sub": "site", "label": "Website files"}, {"target": "/data", "sub": "data", "label": "Certificates"},
                 {"target": "/config", "sub": "config", "label": "Configuration"}],
     "env": [], "open": "/", "notes": "To change its configuration, add a folder with a Caddyfile mounted at /etc/caddy."},
    # ---- media
    {"id": "jellyfin", "cat": "media", "title": "Jellyfin", "image": "jellyfin/jellyfin:latest", "logo": "🎬",
     "about": "Your own Netflix: stream your movies, shows and music to any device. Free, no account needed.",
     "ports": [{"container": 8096, "label": "Web app"}],
     "folders": [{"target": "/config", "sub": "config", "label": "Settings and library database"},
                 {"target": "/cache", "sub": "cache", "label": "Cache"},
                 {"target": "/media", "kind": "media", "label": "Your movies, shows and music", "readonly": True}],
     "env": [_TZ], "open": "/"},
    {"id": "plex", "cat": "media", "title": "Plex Media Server", "image": "plexinc/pms-docker:latest", "logo": "🎞️",
     "about": "Popular media server with apps for TVs and phones (Plex account needed).",
     "network": "host", "ports": [{"container": 32400, "label": "Web app"}],
     "folders": [{"target": "/config", "sub": "config", "label": "Settings and library database"},
                 {"target": "/transcode", "sub": "transcode", "label": "Temporary transcoding files"},
                 {"target": "/data", "kind": "media", "label": "Your media", "readonly": True}],
     "env": [_TZ, {"key": "PLEX_CLAIM", "value": "", "label": "Claim code from plex.tv/claim (valid 4 minutes; optional)"}],
     "open": ":32400/web", "notes": "Uses the server's network directly so TVs and phones can find it."},
    {"id": "navidrome", "cat": "media", "title": "Navidrome", "image": "deluan/navidrome:latest", "logo": "🎵",
     "about": "Your own Spotify: stream your music collection, works with many Subsonic apps.",
     "ports": [{"container": 4533, "label": "Web app"}],
     "folders": [{"target": "/data", "sub": "data", "label": "Database"}, {"target": "/music", "kind": "media", "label": "Your music", "readonly": True}],
     "env": [], "open": "/"},
    {"id": "audiobookshelf", "cat": "media", "title": "Audiobookshelf", "image": "ghcr.io/advplyr/audiobookshelf:latest", "logo": "🎧",
     "about": "Audiobook and podcast server with progress sync and mobile apps.",
     "ports": [{"container": 80, "host": 13378, "label": "Web app"}],
     "folders": [{"target": "/config", "sub": "config", "label": "Settings"}, {"target": "/metadata", "sub": "metadata", "label": "Covers and cache"},
                 {"target": "/audiobooks", "kind": "media", "label": "Your audiobooks"}],
     "env": [], "open": "/"},
    # ---- files
    {"id": "nextcloud", "cat": "files", "title": "Nextcloud", "image": "nextcloud:latest", "logo": "☁️",
     "about": "Your own Google Drive / Dropbox: files, calendar, contacts and sharing, with desktop and phone apps.",
     "ports": [{"container": 80, "host": 8080, "label": "Web app"}],
     "folders": [{"target": "/var/www/html", "sub": "html", "label": "App, settings and files"}],
     "env": [], "open": "/", "notes": "Uses a built-in SQLite database (fine for a few users). For more, create a PostgreSQL or MariaDB "
                                      "container first and pick it in the setup page."},
    {"id": "filebrowser", "cat": "files", "title": "File Browser", "image": "filebrowser/filebrowser:latest", "logo": "📁",
     "about": "Simple web file manager for a folder: upload, download, share links.",
     "ports": [{"container": 80, "host": 8085, "label": "Web app"}],
     "folders": [{"target": "/srv", "kind": "media", "label": "Folder to manage"},
                 {"target": "/database", "sub": "database", "label": "Its database"}, {"target": "/config", "sub": "config", "label": "Settings"}],
     "env": [], "open": "/", "notes": "The first-login password is printed in the container's Logs."},
    {"id": "syncthing", "cat": "files", "title": "Syncthing", "image": "syncthing/syncthing:latest", "logo": "🔄",
     "about": "Keep folders in sync between this server, your PCs and phones, without a cloud.",
     "ports": [{"container": 8384, "label": "Web UI"}, {"container": 22000, "label": "Sync (TCP)"},
               {"container": 22000, "proto": "udp", "label": "Sync (QUIC)"}, {"container": 21027, "proto": "udp", "label": "Local discovery"}],
     "folders": [{"target": "/var/syncthing", "sub": "data", "label": "Settings and synced folders"}],
     "env": [], "open": "/"},
    # ---- home & network
    {"id": "homeassistant", "cat": "home", "title": "Home Assistant", "image": "ghcr.io/home-assistant/home-assistant:stable", "logo": "🏠",
     "about": "Home automation: control lights, sensors and devices from thousands of brands in one place.",
     "network": "host", "ports": [{"container": 8123, "label": "Web app"}],
     "folders": [{"target": "/config", "sub": "config", "label": "Configuration"}],
     "env": [_TZ], "open": ":8123/", "notes": "Uses the server's network directly so it can discover devices on your LAN."},
    {"id": "pihole", "cat": "home", "title": "Pi-hole", "image": "pihole/pihole:latest", "logo": "🛡️",
     "about": "Blocks ads and trackers for every device on your network (a DNS server).",
     "ports": [{"container": 53, "label": "DNS (TCP)"}, {"container": 53, "proto": "udp", "label": "DNS (UDP)"},
               {"container": 80, "host": 8053, "label": "Admin web page"}],
     "folders": [{"target": "/etc/pihole", "sub": "etc-pihole", "label": "Settings and lists"}],
     "env": [_TZ, _pw("FTLCONF_webserver_api_password", "Admin page password")], "open": ":8053/admin/",
     "notes": "Port 53 is often used by the server's own DNS (systemd-resolved): if it's taken, free it first. "
              "Then point your router's DNS to this server."},
    {"id": "adguard", "cat": "home", "title": "AdGuard Home", "image": "adguard/adguardhome:latest", "logo": "🛡️",
     "about": "Network-wide ad and tracker blocking (DNS), with a modern web UI.",
     "ports": [{"container": 53, "label": "DNS (TCP)"}, {"container": 53, "proto": "udp", "label": "DNS (UDP)"},
               {"container": 3000, "label": "Setup web page"}],
     "folders": [{"target": "/opt/adguardhome/work", "sub": "work", "label": "Data"}, {"target": "/opt/adguardhome/conf", "sub": "conf", "label": "Configuration"}],
     "env": [], "open": ":3000/", "notes": "Port 53 is often used by systemd-resolved: free it first. Pick a web port other than 80 in its setup."},
    {"id": "mosquitto", "cat": "home", "title": "Mosquitto (MQTT)", "image": "eclipse-mosquitto:2", "logo": "📡",
     "about": "MQTT message broker for smart-home devices and sensors.",
     "ports": [{"container": 1883, "label": "MQTT"}],
     "folders": [{"target": "/mosquitto/data", "sub": "data", "label": "Data"}, {"target": "/mosquitto/log", "sub": "log", "label": "Logs"}],
     "env": [], "open": None, "notes": "Version 2 only accepts local connections until you add a mosquitto.conf (mounted at /mosquitto/config)."},
    # ---- monitoring
    {"id": "uptimekuma", "cat": "monitor", "title": "Uptime Kuma", "image": "louislam/uptime-kuma:1", "logo": "📶",
     "about": "Watches your websites and services and alerts you when they go down.",
     "ports": [{"container": 3001, "label": "Web app"}],
     "folders": [{"target": "/app/data", "sub": "data", "label": "Data"}], "env": [], "open": "/"},
    {"id": "grafana", "cat": "monitor", "title": "Grafana", "image": "grafana/grafana:latest", "logo": "📊",
     "about": "Dashboards and charts for metrics from Prometheus, InfluxDB, databases…",
     "ports": [{"container": 3000, "label": "Web app"}],
     "folders": [{"target": "/var/lib/grafana", "sub": "data", "label": "Dashboards and settings"}],
     "env": [_pw("GF_SECURITY_ADMIN_PASSWORD", "Password of the admin user")], "open": "/",
     "notes": "Log in as admin with the password above. The data folder must be writable by user 472: if Grafana fails to start, "
              "run “sudo chown -R 472:0 FOLDER”."},
    {"id": "prometheus", "cat": "monitor", "title": "Prometheus", "image": "prom/prometheus:latest", "logo": "🔥",
     "about": "Collects and stores metrics; pairs with Grafana.",
     "ports": [{"container": 9090, "label": "Web UI"}],
     "folders": [{"target": "/prometheus", "sub": "data", "label": "Metrics database"}], "env": [], "open": "/",
     "notes": "To scrape your own targets, mount a prometheus.yml at /etc/prometheus/prometheus.yml."},
    {"id": "portainer", "cat": "monitor", "title": "Portainer CE", "image": "portainer/portainer-ce:latest", "logo": "🐳",
     "about": "Another web interface for Docker, with more advanced options.",
     "ports": [{"container": 9443, "label": "Web app (HTTPS)"}],
     "folders": [{"target": "/data", "sub": "data", "label": "Data"}, {"target": "/var/run/docker.sock", "kind": "host", "source": "/var/run/docker.sock", "label": "Docker control"}],
     "env": [], "open": "https://:9443/", "notes": "It controls Docker (root-equivalent). Create the admin user within 5 minutes of starting it."},
    {"id": "dozzle", "cat": "monitor", "title": "Dozzle", "image": "amir20/dozzle:latest", "logo": "📜",
     "about": "Live logs of all your containers in the browser.",
     "ports": [{"container": 8080, "host": 8888, "label": "Web app"}],
     "folders": [{"target": "/var/run/docker.sock", "kind": "host", "source": "/var/run/docker.sock", "label": "Docker (read-only)", "readonly": True}],
     "env": [], "open": "/", "notes": "Anyone who can open its page sees all container logs: keep it on your LAN."},
    {"id": "watchtower", "cat": "monitor", "title": "Watchtower", "image": "containrrr/watchtower:latest", "logo": "🗼",
     "about": "Updates your containers automatically when new image versions are published.",
     "ports": [], "folders": [{"target": "/var/run/docker.sock", "kind": "host", "source": "/var/run/docker.sock", "label": "Docker control"}],
     "env": [{"key": "WATCHTOWER_CLEANUP", "value": "true", "label": "Delete old images after updating"},
             {"key": "WATCHTOWER_SCHEDULE", "value": "0 0 4 * * *", "label": "When to check (cron with seconds; default 4 am daily)"}],
     "open": None},
    # ---- development & automation
    {"id": "gitea", "cat": "dev", "title": "Gitea", "image": "gitea/gitea:latest", "logo": "🍵",
     "about": "Your own GitHub: Git hosting with issues, pull requests and CI.",
     "ports": [{"container": 3000, "label": "Web app"}, {"container": 22, "host": 2222, "label": "Git over SSH"}],
     "folders": [{"target": "/data", "sub": "data", "label": "Repositories and settings"}],
     "env": [{"key": "USER_UID", "value": "1000", "label": "User id that owns the files"}, {"key": "USER_GID", "value": "1000", "label": "Group id"}],
     "open": "/"},
    {"id": "n8n", "cat": "dev", "title": "n8n", "image": "n8nio/n8n:latest", "logo": "🔗",
     "about": "Workflow automation (like Zapier): connect apps and APIs with a visual editor.",
     "ports": [{"container": 5678, "label": "Web app"}],
     "folders": [{"target": "/home/node/.n8n", "sub": "data", "label": "Workflows and credentials"}],
     "env": [_TZ, {"key": "GENERIC_TIMEZONE", "value": "Etc/UTC", "label": "Time zone for schedules"},
             {"key": "N8N_SECURE_COOKIE", "value": "false", "label": "false = allow login over plain http on your LAN"}],
     "open": "/", "notes": "The data folder must be writable by user 1000: if n8n fails to start, run “sudo chown -R 1000:1000 FOLDER”."},
    {"id": "codeserver", "cat": "dev", "title": "code-server", "image": "lscr.io/linuxserver/code-server:latest", "logo": "💻",
     "about": "VS Code in your browser, running on this server.",
     "ports": [{"container": 8443, "label": "Web app"}],
     "folders": [{"target": "/config", "sub": "config", "label": "Settings and workspace"}],
     "env": [_TZ, {"key": "PUID", "value": "1000", "label": "User id that owns the files"}, {"key": "PGID", "value": "1000", "label": "Group id"},
             _pw("PASSWORD", "Login password")], "open": "/"},
    {"id": "rabbitmq", "cat": "dev", "title": "RabbitMQ", "image": "rabbitmq:4-management", "logo": "🐇",
     "about": "Message broker for applications, with a management web page.",
     "ports": [{"container": 5672, "label": "AMQP"}, {"container": 15672, "label": "Management web page"}],
     "folders": [{"target": "/var/lib/rabbitmq", "sub": "data", "label": "Data"}],
     "env": [{"key": "RABBITMQ_DEFAULT_USER", "value": "", "label": "Admin user name", "required": True, "placeholder": "e.g. admin"}, _pw("RABBITMQ_DEFAULT_PASS", "Admin password")],
     "open": ":15672/"},
    {"id": "registry", "cat": "dev", "title": "Docker Registry", "image": "registry:2", "logo": "📦",
     "about": "Store your own Docker images on this server.",
     "ports": [{"container": 5000, "label": "Registry API"}],
     "folders": [{"target": "/var/lib/registry", "sub": "data", "label": "Images"}], "env": [], "open": None,
     "notes": "Without HTTPS, other machines must list this server as an insecure registry."},
    # ---- security
    {"id": "vaultwarden", "cat": "security", "title": "Vaultwarden", "image": "vaultwarden/server:latest", "logo": "🔐",
     "about": "Password manager server compatible with the Bitwarden apps and browser extensions.",
     "ports": [{"container": 80, "host": 8086, "label": "Web vault"}],
     "folders": [{"target": "/data", "sub": "data", "label": "Encrypted vaults"}],
     "env": [{"key": "DOMAIN", "value": "", "label": "Public https address, e.g. https://vault.example.com (optional)"},
             {"key": "SIGNUPS_ALLOWED", "value": "true", "label": "Allow new accounts (set to false after creating yours)"}],
     "open": "/", "notes": "Browsers only allow the web vault over HTTPS: put it behind Nginx Proxy Manager or Caddy."},
    {"id": "wireguard", "cat": "security", "title": "WireGuard (wg-easy)", "image": "ghcr.io/wg-easy/wg-easy:15", "logo": "🧭",
     "about": "VPN to reach your home network from anywhere, with a web page to add devices (QR codes).",
     "ports": [{"container": 51820, "proto": "udp", "label": "VPN"}, {"container": 51821, "label": "Web page"}],
     "folders": [{"target": "/etc/wireguard", "sub": "config", "label": "VPN configuration"},
                 {"target": "/lib/modules", "kind": "host", "source": "/lib/modules", "label": "Kernel modules", "readonly": True}],
     "env": [{"key": "INSECURE", "value": "true", "label": "true = allow the web page over plain http on your LAN"}],
     "privileged": True, "sysctls": {"net.ipv4.ip_forward": "1", "net.ipv4.conf.all.src_valid_mark": "1"}, "open": "/", "notes": "Needs privileged mode (network settings). Forward UDP 51820 on your router to this server."},
]

_BY_ID = {a["id"]: a for a in APPS}


def get(app_id: str) -> dict | None:
    return _BY_ID.get(app_id)


def repo(image: str) -> str:
    """'postgres:17' → 'postgres', 'ghcr.io/x/y:tag' → 'ghcr.io/x/y'."""
    image = image.split("@", 1)[0]
    last = image.rsplit("/", 1)
    if ":" in last[-1]:
        image = image.rsplit(":", 1)[0]
    if image.startswith("docker.io/"):
        image = image[10:]
    if image.startswith("library/"):
        image = image[8:]
    return image
