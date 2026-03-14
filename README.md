# DjangoMultiDeploy

Interaktives Bash-Installationsskript für **mehrere Django-Projekte auf einem Server** — jedes mit eigenem Gunicorn-Port, nginx-Site, systemd-Service, Datenbank und App-User.

Zoraxy Reverse Proxy ready · LXC/Container ready · Debian & Ubuntu

---

## Was das Skript macht

Ein einziger Aufruf von `Installv2.sh` richtet ein komplettes Django-Projekt ein:

| Schritt | Was passiert |
|---|---|
| **App-User** | eigener Linux-User wird angelegt |
| **SSH-Key** | ed25519-Key für WinSCP/PuTTY/GitHub |
| **Python venv** | projektgebunden unter `/srv/<projekt>/.venv` |
| **Django + Gunicorn** | inkl. aller DB-Treiber |
| **Datenbank** | PostgreSQL / MySQL / SQLite (lokal oder remote) |
| **systemd Service** | Autostart, Restart, Logging |
| **nginx Site** | server_name-basiert, static/media files |
| **Backup-Skript** | DB-Dump + Projekt-Archiv, 14 Tage Rotation |
| **Update-Skript** | git pull + migrate + collectstatic + restart |
| **MOTD** | zeigt beim Login alle laufenden Django-Projekte |

---

## Multi-Server-Betrieb

Mehrere Django-Projekte laufen parallel auf demselben Server — jedes bekommt einen eigenen Gunicorn-Port. Das Skript erkennt automatisch den nächsten freien Port ab 8000.

```
webapp   →  Gunicorn: 127.0.0.1:8000  →  nginx (server_name: webapp.example.com)
shopapp  →  Gunicorn: 127.0.0.1:8001  →  nginx (server_name: shop.example.com)
intranet →  Gunicorn: 127.0.0.1:8002  →  nginx (server_name: intern.example.com)
```

**PostgreSQL**: Ein einziger PostgreSQL-Server (Port 5432) reicht für alle Projekte — nur DB-Name und DB-User müssen pro Projekt unterschiedlich sein.

---

## Zoraxy Reverse Proxy

Zoraxy läuft auf einem **anderen Server** und terminiert SSL. nginx auf diesem Server hört auf Port 80 und leitet per `server_name` weiter.

```
Internet
   ↓
Zoraxy (anderer Server, SSL)
   ↓  http://DIESE-SERVER-IP:80  +  Host-Header weiterleiten
nginx (dieser Server, Port 80)
   ↓  server_name-basiertes Routing
Gunicorn  (127.0.0.1:8000 / :8001 / :8002 …)
   ↓
Django
```

**Zoraxy-Einstellung:** `Pass Host Header` / `Preserve Host` aktivieren.

---

## Login-Anzeige (MOTD)

Beim Login wird automatisch eine Übersicht aller installierten Django-Projekte angezeigt:

```
╔══════════════════════════════════════════════════════════════════════════╗
║  Django Server Übersicht - mein-server.local                             ║
╠══════════════════════════════════════════════════════════════════════════╣
║  PROJEKT                PORT    MODUS  DATENBANK  STATUS     BENUTZER   ║
╠══════════════════════════════════════════════════════════════════════════╣
║  webapp                 8000    prod   postgresql aktiv ✅   webuser    ║
║  shopapp                8001    prod   postgresql aktiv ✅   shopuser   ║
╚══════════════════════════════════════════════════════════════════════════╝

  [1] webapp
  ┌─────────────────────────────────────────────────────────────────
  │  👤 App-User:    webuser
  │  📁 Pfad:        /srv/webapp
  │  🔄 Git Pull:    su - webuser -s /bin/bash -c "cd /srv/webapp && git pull"
  │  🚀 Update:      webapp_update.sh      (pull+migrate+static+restart)
  │  🔁 Neustart:    systemctl restart webapp
  │  📋 Logs live:   journalctl -u webapp -f
  └─────────────────────────────────────────────────────────────────
```

---

## Datenbanken

| Typ | Lokal installieren | Remote-Verbindung |
|---|---|---|
| **PostgreSQL** | ✅ | ✅ |
| **MySQL / MariaDB** | ✅ | ✅ |
| **SQLite** | ✅ (nur DEV) | — |

---

## DEV / PROD Modus

| | DEV | PROD |
|---|---|---|
| `DEBUG` | `True` | `False` |
| SSL-Proxy-Header | — | ✅ aktiv |
| Secure Cookies | — | ✅ aktiv |
| Geeignet für | LAN / Tests | Zoraxy / HTTPS |

---

## Installation

```bash
chmod +x Installv2.sh
sudo ./Installv2.sh
```

Das Skript fragt interaktiv ab:

- Projektname → `/srv/<projekt>`
- GitHub Repository (optional, öffentlich oder privat)
- DEV oder PROD Modus
- Gunicorn-Port (Vorschlag: nächster freier ab 8000)
- ALLOWED_HOSTS (automatisch vorbelegt mit Server-IPs)
- Datenbank-Typ + Zugangsdaten
- Linux App-User
- Django SECRET_KEY (auto-generiert wenn leer)
- SSH-Key Passphrase

---

## Checkpoint / Resume

Wird die Installation unterbrochen, kann sie beim nächsten Aufruf an der Stelle fortgesetzt werden, an der sie abgebrochen hat. Laufende State-Dateien liegen unter `/tmp/django_install_<projekt>.state`.

---

## Architektur

```
/srv/<projekt>/
├── .venv/          ← Python Virtual Environment
├── .env            ← Secrets (chmod 600, nur App-User)
├── manage.py
├── <django-modul>/
│   └── settings.py
└── staticfiles/

/etc/systemd/system/<projekt>.service
/etc/nginx/sites-available/<projekt>
/etc/django-servers.d/<projekt>.conf   ← Registry für MOTD
/etc/profile.d/00_django_motd.sh       ← Geteiltes MOTD-Skript
/usr/local/bin/<projekt>_update.sh
/usr/local/bin/<projekt>_backup.sh
/var/log/<projekt>/
/var/backups/<projekt>/
```

**Trennung:** Linux-User ≠ DB-User (Absicht — minimale Rechte)

---

## Security

- keine Secrets im Code oder systemd-Service
- `.env` nur für root + App-User (`chmod 600`)
- `.env` niemals ins Git-Repo (`.gitignore` wird automatisch erstellt)
- fail2ban optional installierbar
- Django-Admin erreichbar unter `/djadmin/` (nicht `/admin/`)

---

## Was das Skript bewusst nicht macht

- kein HTTPS (macht Zoraxy / dein Reverse Proxy)
- kein Firewall-Setup
- kein Auto-Scaling

→ Ziel: klarer, stabiler Unterbau. Kein Hosting-Framework.

---

## Kompatibilität

- Debian 12+
- Ubuntu 22.04+
- Proxmox LXC (nesting=1, keyctl=1)
- Normale VMs und Bare-Metal-Server
