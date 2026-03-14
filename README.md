# DjangoMultiDeploy

Interaktives Bash-Installationsskript für **mehrere Django-Projekte auf einem Server** — jedes mit eigenem Gunicorn-Port, nginx-Site, systemd-Service, Datenbank, App-User und SSH-Key.

Optional: **Web-Interface (Manager)** auf Port 8888 zur Verwaltung aller Projekte im Browser.

Zoraxy Reverse Proxy ready · Checkpoint/Resume · LXC/Container ready · Debian & Ubuntu

---

## Übersicht

| Was | Details |
|---|---|
| Linux App-User | eigener User, Home-Verzeichnis, SSH-Key (ed25519) |
| Python venv | projektgebunden unter `/srv/<projekt>/.venv` |
| Django + Gunicorn | 2×CPU+1 Worker, 120s Timeout, eigener Port ab 8000 |
| Datenbank | PostgreSQL / MySQL / SQLite — lokal oder remote |
| systemd Service | Autostart, Restart=always, RestartSec=10 |
| nginx | server_name-basiert, Security-Header, gzip, Static/Media-Caching |
| fail2ban | SSH-Schutz (3 Versuche, 1h Ban) — optional |
| Backup-Skript | DB-Dump + Projekt-Archiv, 14-Tage-Rotation, täglicher Cron |
| Update-Skript | Backup → git pull → migrate → collectstatic → restart |
| Health-Check | `/health/` Endpoint mit DB-Test |
| MOTD | zeigt beim Login alle Django-Projekte mit Status und Befehlen |
| Checkpoint/Resume | unterbrochene Installationen fortsetzbar |
| **Web-Interface** | **Browser-Verwaltung aller Projekte auf Port 8888** |

---

## Installation

```bash
git clone https://github.com/Manjo80/DjangoMultiDeploy.git
cd DjangoMultiDeploy
chmod +x Installv2.sh
sudo ./Installv2.sh
```

> Muss als **root** auf Debian 12+ oder Ubuntu 22.04+ ausgeführt werden.

---

## Installations-Menü

Nach den System-Voraussetzungs-Checks wählt man, was installiert werden soll:

```
╔═══════════════════════════════════════════════════════════════╗
║          DjangoMultiDeploy — Was installieren?               ║
╠═══════════════════════════════════════════════════════════════╣
║  1) Django-Projekt           (neue Django-Webanwendung)      ║
║  2) DjangoMultiDeploy Manager (Web-Interface Port 8888)      ║
║  3) Beides                                                   ║
╚═══════════════════════════════════════════════════════════════╝
```

| Option | Was passiert |
|---|---|
| **1** | Django-Projekt einrichten (Gunicorn, nginx, DB, systemd, …) |
| **2** | Nur Manager installieren (venv, systemd, Port 8888) |
| **3** | Beides — Manager + neues Django-Projekt |

---

## Eingaben beim Setup (Django-Projekt)

Das Skript fragt alle Parameter interaktiv ab. Alle Eingaben werden in einer State-Datei gespeichert, damit eine unterbrochene Installation fortgesetzt werden kann.

| Eingabe | Standard | Hinweis |
|---|---|---|
| **Projektname** | — | 3–50 Zeichen, a-z A-Z 0-9 _ - |
| **GitHub URL** | leer (neues Projekt) | öffentlich oder privat (SSH) |
| **Modus** | 1 = DEV | 2 = PROD |
| **Gunicorn-Port** | nächster freier Port ≥ 8000 | automatisch erkannt |
| **ALLOWED_HOSTS** | alle lokalen IPs + localhost + FQDN | kommasepariert |
| **Datenbank-Typ** | — | 1 = PostgreSQL, 2 = MySQL, 3 = SQLite |
| **DB-Modus** | — | 1 = lokal installieren, 2 = remote |
| **DB-Name** | `<projektname>` | - wird zu _ |
| **DB-User** | `<projektname>_user` | |
| **DB-Host** | localhost | |
| **DB-Port** | 5432 (PG) / 3306 (MySQL) | |
| **DB-Passwort** | — | Pflicht |
| **Linux App-User** | — | Pflicht, beginnt mit Buchstabe |
| **Django SECRET_KEY** | auto (32 Hex-Zeichen) | leer lassen = wird generiert |
| **SSH-Key Passphrase** | leer (kein Passwort) | für ed25519-Key |
| **Gunicorn Worker** | 2×CPU+1 | automatisch berechnet |
| **Sprachcode** | `de-de` | z.B. `en-us`, `fr-fr` |
| **Zeitzone** | `Europe/Berlin` | z.B. `Europe/London` |
| **SMTP Host** | leer (deaktiviert) | optional |
| **Backup-Uhrzeit** | `02:00` | täglicher Cron |
| **System-Pakete updaten** | J | empfohlen |
| **fail2ban installieren** | J | SSH-Brute-Force-Schutz |

---

## NONINTERACTIVE-Modus

Das Skript unterstützt einen vollständig nicht-interaktiven Modus für die Einbindung in CI/CD oder das Web-Interface. Alle Eingaben werden als Umgebungsvariablen übergeben:

```bash
export NONINTERACTIVE=true
export PROJECTNAME=myapp
export APPUSER=myuser
export MODESEL=2
export DBTYPE_SEL=1
export DBMODE=2
export DBHOST=localhost
export DBPORT=5432
export DBNAME=myapp
export DBUSER=myapp_user
export DBPASS=geheim
export GUNICORN_PORT=8001
export LANGUAGE_CODE=de-de
export TIME_ZONE=Europe/Berlin
export _INSTALL_SEL=1
sudo ./Installv2.sh
```

| Umgebungsvariable | Bedeutung | Standard |
|---|---|---|
| `NONINTERACTIVE` | `true` = alle Prompts deaktivieren | `false` |
| `_INSTALL_SEL` | 1=Projekt, 2=Manager, 3=Beides | `3` |
| `PROJECTNAME` | Projektname | — |
| `APPUSER` | Linux App-User | — |
| `MODESEL` | 1=DEV, 2=PROD | `1` |
| `GITHUB_REPO_URL` | GitHub URL oder leer | leer |
| `GUNICORN_PORT` | Port (leer = auto) | ab 8000 |
| `GUNICORN_WORKERS` | Anzahl Worker | 2×CPU+1 |
| `ALLOWED_HOSTS` | Kommasepariert | auto |
| `DBTYPE_SEL` | 1=PG, 2=MySQL, 3=SQLite | — |
| `DBMODE` | 1=lokal, 2=remote | — |
| `DBNAME` / `DBUSER` / `DBPASS` | DB-Zugangsdaten | — |
| `DBHOST` / `DBPORT` | DB-Verbindung | localhost/5432 |
| `LANGUAGE_CODE` / `TIME_ZONE` | Lokalisierung | de-de / Europe/Berlin |
| `EMAIL_HOST` u.a. | SMTP-Konfiguration | leer |
| `_BACKUP_TIME` | Cron-Zeit (HH:MM) | `02:00` |
| `UPGRADE` | j=System-Pakete updaten | `n` |
| `INSTALL_FAIL2BAN` | j=fail2ban installieren | `n` |

---

## DjangoMultiDeploy Manager (Web-Interface)

Der Manager ist eine Django-App, die alle installierten Projekte über einen Browser verwaltet. Er wird auf **Port 8888** betrieben.

### Features

| Seite | Funktion |
|---|---|
| **Dashboard** | Übersicht aller Projekte mit Service-Status |
| **Install-Wizard** | Formular → NONINTERACTIVE-Aufruf → Live-Terminal (SSE) |
| **Projektdetail** | Start / Stop / Restart / Update / Backup |
| **Log-Viewer** | systemd Journal, nginx Access + Error-Logs |
| **SSH-Key** | Key im Browser anzeigen und herunterladen |
| **Remove-Wizard** | Granulares Entfernen: Dateien / DB / User / Backup / Logs |

### Installationsweg

```bash
sudo ./Installv2.sh
# → Option 2 oder 3 wählen
```

Manager läuft danach unter: `http://<server-ip>:8888/`

### Manager-Verzeichnis

```
/srv/djmanager/
├── .env                    ← SECRET_KEY, Port, Pfade (chmod 600)
├── venv/                   ← Python venv
├── manage.py
├── djmanager/              ← Django-Einstellungen
│   ├── settings.py
│   └── urls.py
├── control/                ← Views, Utils, Templates
│   ├── views.py            ← alle Views + SSE-Stream
│   ├── utils.py            ← Registry lesen, systemctl, Logs, Backup
│   ├── urls.py
│   └── templates/control/
│       ├── base.html       ← Bootstrap 5 Dark-Theme
│       ├── dashboard.html
│       ├── install_form.html
│       ├── install_progress.html   ← Live-Terminal via SSE
│       ├── project_detail.html
│       ├── log_viewer.html
│       ├── ssh_key.html
│       ├── remove_confirm.html
│       └── remove_done.html
├── logs/                   ← Install-Logs (pro Aufruf)
└── staticfiles/
```

### systemd Service

```ini
[Service]
User=root
ExecStart=/srv/djmanager/venv/bin/python /srv/djmanager/manage.py \
          runserver 0.0.0.0:8888
Restart=always
```

```bash
systemctl status djmanager
journalctl -u djmanager -f
```

> **Hinweis:** Der Manager läuft als root, da er systemctl-Befehle ausführen und das Installationsskript aufrufen muss. Nur im internen Netz verwenden — für externen Zugriff Zoraxy/nginx mit Authentifizierung vorschalten.

---

## Multi-Server-Betrieb

Mehrere Django-Projekte laufen parallel auf demselben Server. Das Skript erkennt den nächsten freien Port automatisch:

```
webapp    →  Gunicorn: 127.0.0.1:8000  →  nginx (server_name: webapp.example.com)
shopapp   →  Gunicorn: 127.0.0.1:8001  →  nginx (server_name: shop.example.com)
intranet  →  Gunicorn: 127.0.0.1:8002  →  nginx (server_name: intern.example.com)
djmanager →  Port 8888 direkt            (Manager Web-Interface)
```

**PostgreSQL:** Ein einziger PostgreSQL-Server (Port 5432) reicht für alle Projekte. Nur DB-Name und DB-User müssen pro Projekt unterschiedlich sein. Das Skript zeigt beim Setup vorhandene Datenbanken an.

```
webapp   →  DB: webapp   / User: webapp_user   (Port 5432 ✅)
shopapp  →  DB: shopapp  / User: shopapp_user  (Port 5432 ✅)
```

---

## Zoraxy Reverse Proxy

Zoraxy läuft auf einem **anderen Server** und terminiert SSL. nginx auf diesem Server hört auf Port 80.

```
Internet
   ↓
Zoraxy  (anderer Server, SSL-Terminierung)
   ↓   Ziel: http://<DIESER-SERVER>:80   +   Host-Header weiterleiten
nginx   (dieser Server, Port 80, server_name-Routing)
   ↓
Gunicorn  (127.0.0.1:8000 / :8001 / :8002 …)
   ↓
Django
```

**Zoraxy-Einstellung:** `Pass Host Header` / `Preserve Host` aktivieren — sonst schlägt Django CSRF fehl.

Das Skript zeigt nach der Installation die fertige Zoraxy-Konfiguration an:

```
┌─────────────────────────────────────────────────────────────┐
│  Incoming:  webapp.example.com                              │
│  Target:    http://192.168.1.10:80                          │
│  Option:    'Pass Host Header' / 'Preserve Host' ✅         │
└─────────────────────────────────────────────────────────────┘
```

Im PROD-Modus setzt Django automatisch:
```python
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True
```

---

## System-Voraussetzungs-Checks

Vor der Installation werden automatisch geprüft:

| Check | Was wird geprüft | Fehler-Aktion |
|---|---|---|
| `/tmp` beschreibbar | `touch /tmp/.test` | remount / neues tmpfs |
| Root-FS beschreibbar | `touch /root/.test` | Abbruch mit Hinweis |
| Freier Speicher `/` | mind. 2 GB | Abbruch |
| Freier Speicher `/tmp` | mind. 512 MB | Abbruch |
| DNS-Auflösung | `getent hosts pypi.org` | Abbruch |
| HTTPS-Verbindung | `curl pypi.org` | Abbruch |
| Systemzeit | > 2023 | Warnung |

> **Proxmox LXC:** Falls `/tmp` read-only ist (häufiges Problem), versucht das Skript automatisch `mount -o remount,rw /tmp` und als Fallback ein neues tmpfs. Schlägt beides fehl, erscheint eine klare Diagnose-Meldung.

---

## DEV / PROD Modus

| Einstellung | DEV | PROD |
|---|---|---|
| `DEBUG` | `True` | `False` |
| SSL-Proxy-Header | — | ✅ |
| Secure Cookies | — | ✅ |
| X-Frame-Options | — | `DENY` |
| Content-Type-Nosniff | — | ✅ |
| Geeignet für | LAN / Tests | Zoraxy / HTTPS |

---

## Datenbanken

| Typ | Lokal | Remote | DEV | PROD |
|---|---|---|---|---|
| **PostgreSQL** | ✅ | ✅ | ✅ | ✅ |
| **MySQL / MariaDB** | ✅ | ✅ | ✅ | ✅ |
| **SQLite** | ✅ | — | ✅ | ⚠️ nicht empfohlen |

Alle Zugangsdaten landen ausschließlich in `/srv/<projekt>/.env` (chmod 600).

**DB-Verbindungstest:** Vor den Migrationen wird die DB-Verbindung mit einem `SELECT 1` geprüft — bei Fehler Abbruch mit klarer Meldung.

---

## Gunicorn Worker

Die Worker-Anzahl wird automatisch aus den CPU-Kernen berechnet:

```
Worker = 2 × CPU-Kerne + 1
```

| CPU-Kerne | Worker |
|---|---|
| 1 | 3 |
| 2 | 5 |
| 4 | 9 |
| 8 | 17 |

Der Wert kann beim Setup manuell überschrieben werden (1–32).

---

## ALLOWED_HOSTS & CSRF

Das Skript belegt `ALLOWED_HOSTS` automatisch mit allen lokalen IPs, `localhost` und dem FQDN vor. Daraus wird `CSRF_TRUSTED_ORIGINS` automatisch gebaut:

- IPs und `localhost` → `http://` und `https://`
- DNS-Namen → nur `https://`

Beispiel:
```
ALLOWED_HOSTS = 192.168.1.10, localhost, webapp.example.com
CSRF_TRUSTED_ORIGINS = http://192.168.1.10, https://192.168.1.10,
                       http://localhost, https://localhost,
                       https://webapp.example.com
```

---

## GitHub Integration

Das Skript unterstützt das Klonen von öffentlichen und privaten GitHub-Repositories.

**Ablauf bei privatem Repo:**
1. ed25519-Key wird generiert und angezeigt
2. User kopiert den Public Key zu GitHub (Settings → SSH Keys)
3. Skript wartet auf Bestätigung
4. SSH-Verbindung wird getestet:
   - Port 22 → `git@github.com`
   - Fallback Port 443 → `git@ssh.github.com` (automatisch, falls Port 22 blockiert)
5. Bei Port-443-Fallback wird `~/.ssh/config` automatisch erstellt

```
Host github.com
    Hostname ssh.github.com
    Port 443
    User git
    IdentityFile /home/<appuser>/.ssh/id_ed25519
    IdentitiesOnly yes
```

**Im Web-Interface (Manager):** Der Public Key wird direkt im Browser angezeigt und ist per Klick herunterladbar. Die Installation wartet, bis der User auf "GitHub-Key bestätigt" klickt.

Der **Django-Modul-Name** (Verzeichnis mit `wsgi.py`) wird automatisch erkannt.

---

## SSH-Key (App-User)

| Datei | Rechte | Zweck |
|---|---|---|
| `~/.ssh/id_ed25519` | 600 | Private Key |
| `~/.ssh/id_ed25519.pub` | 644 | Public Key |
| `~/.ssh/authorized_keys` | 600 | SSH-Login mit Key |
| `~/.ssh/` | 700 | Verzeichnis |

Key nach der Installation herunterladen (für WinSCP / PuTTY):
```bash
scp root@<server-ip>:/home/<appuser>/.ssh/id_ed25519 .
```

Oder im **Manager** → Projektdetail → **SSH-Key** → Herunterladen.

---

## Django Admin

Der Admin-Bereich ist unter `/djadmin/` erreichbar (nicht `/admin/`). Das erhöht die Sicherheit gegen automatisierte Angriffe auf den Standard-Pfad.

```
http://<server-ip>/djadmin/
```

---

## Health-Check Endpoint

Wird bei **neuen Projekten** automatisch erstellt (nicht bei GitHub-Klonen).

```
GET /health/
```

Antwort:
```json
{ "status": "ok", "database": "ok", "mode": "prod" }
```

Bei DB-Fehler:
```json
{ "status": "degraded", "database": "error", "mode": "prod" }
```

Geeignet für Container-Health-Checks, Monitoring, Load-Balancer-Probes.

---

## systemd Service

```ini
[Service]
User=<appuser>
WorkingDirectory=/srv/<projekt>
EnvironmentFile=/srv/<projekt>/.env
ExecStart=.venv/bin/gunicorn <modul>.wsgi:application \
  --bind 127.0.0.1:<port> \
  --workers <2×CPU+1> \
  --timeout 120 \
  --access-logfile /var/log/<projekt>/access.log \
  --error-logfile /var/log/<projekt>/error.log
Restart=always
RestartSec=10
```

App-User darf ohne Passwort:
```bash
sudo systemctl restart <projekt>
sudo systemctl status  <projekt>
sudo systemctl reload  <projekt>
sudo journalctl -u <projekt> -f
```

---

## nginx Konfiguration

- Hört auf **Port 80**, Routing per `server_name`
- `client_max_body_size 50M` (Datei-Upload)
- gzip aktiviert (Kompressionsstufe 6, min. 256 Byte)

**Security-Header** (automatisch gesetzt):
```
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
X-XSS-Protection: 1; mode=block
```

**Caching:**
| Pfad | Expiry | Cache-Control |
|---|---|---|
| `/static/` | 1 Jahr | `public, immutable` |
| `/media/` | 30 Tage | — |

---

## fail2ban

Optional installierbar (Standard: Ja).

```ini
[sshd]
enabled  = true
port     = ssh
filter   = sshd
logpath  = /var/log/auth.log
maxretry = 3
bantime  = 3600    # 1 Stunde
```

→ IP wird nach **3 fehlgeschlagenen SSH-Logins** für **1 Stunde** gesperrt.

---

## E-Mail / SMTP (optional)

Beim Setup kann ein SMTP-Server konfiguriert werden:

| Variable | Beispiel |
|---|---|
| `EMAIL_HOST` | `smtp.gmail.com` |
| `EMAIL_PORT` | `587` |
| `EMAIL_HOST_USER` | `user@gmail.com` |
| `EMAIL_HOST_PASSWORD` | `geheim` |
| `EMAIL_USE_TLS` | `True` |
| `DEFAULT_FROM_EMAIL` | `noreply@beispiel.de` |

Ohne SMTP-Konfiguration verwendet Django automatisch das `console`-Backend (gibt E-Mails in die Logs aus).

---

## Update-Skript

Erstellt unter `/usr/local/bin/<projekt>_update.sh`

```
1. Backup erstellen  (<projekt>_backup.sh — sichert vor dem Update!)
2. git pull  (als App-User, mit SSH-Key + ConnectTimeout=30s)
3. pip install -r requirements.txt  (falls vorhanden)
4. python manage.py makemigrations  (nur wenn neue Migrationen erkannt)
5. python manage.py migrate
6. python manage.py collectstatic --noinput
7. sudo systemctl restart <projekt>
```

Über den **Manager** per Klick ausführbar (Projektdetail → "Git Pull + Update").

---

## Backup-Skript

Erstellt unter `/usr/local/bin/<projekt>_backup.sh`

| Datei | Inhalt |
|---|---|
| `db_YYYYMMDD_HHMMSS.dump` | PostgreSQL pg_dump (-Fc Format) |
| `db_YYYYMMDD_HHMMSS.sql` | MySQL mysqldump |
| `db_YYYYMMDD_HHMMSS.sqlite3` | SQLite Kopie |
| `env_YYYYMMDD_HHMMSS.backup` | `.env` Datei (chmod 600) |
| `project_YYYYMMDD_HHMMSS.tar.gz` | Projekt-Archiv ohne `.venv`, `__pycache__`, `*.pyc`, `*.log` |

Backups liegen in `/var/backups/<projekt>/` (Rechte 700).
Dateien älter als **14 Tage** werden automatisch gelöscht.
Täglicher Cron zur eingestellten Uhrzeit (Standard: 02:00).

---

## Deinstallations-Skript

Erstellt unter `/usr/local/bin/<projekt>_remove.sh`

Entfernt (mit Bestätigung pro Schritt):
1. systemd Service stoppen + deaktivieren
2. nginx-Site entfernen
3. systemd + sudoers + logrotate + Registry-Eintrag
4. optional: Projektverzeichnis `/srv/<projekt>/`
5. optional: Datenbank + DB-User
6. optional: Linux App-User
7. optional: Backups `/var/backups/<projekt>/`
8. optional: Logs `/var/log/<projekt>/`
9. Skript löscht sich selbst am Ende

Im **Manager** über den Remove-Wizard mit Checkboxen steuerbar.

---

## Status-Skript

```bash
django_status.sh
```

Globales Skript für alle Projekte. Zeigt eine Tabelle mit:
- systemctl-Status
- HTTP `/health/` Check
- Port, Modus, DB, User

---

## MOTD — Login-Anzeige

Beim Login wird automatisch eine Übersicht aller installierten Django-Projekte angezeigt (einmalig pro Session, nur in interaktiven Shells).

```
╔══════════════════════════════════════════════════════════════════════════╗
║  Django Server Übersicht - mein-server.local                             ║
╠══════════════════════════════════════════════════════════════════════════╣
║  PROJEKT                PORT    MODUS  DATENBANK  STATUS     BENUTZER   ║
╠══════════════════════════════════════════════════════════════════════════╣
║  webapp                 8000    prod   postgresql aktiv ✅   webuser    ║
║  shopapp                8001    prod   postgresql aktiv ✅   shopuser   ║
╠══════════════════════════════════════════════════════════════════════════╣
║  IP: 192.168.1.10  |  14.03.2025 08:45  |  Uptime: up 3 days           ║
╚══════════════════════════════════════════════════════════════════════════╝

  [1] webapp  (installiert: 2025-01-15 14:22)
  ┌─────────────────────────────────────────────────────────────────
  │  👤 App-User:    webuser
  │  📁 Pfad:        /srv/webapp
  │  🌐 Modus:       prod  |  🔌 Gunicorn: 127.0.0.1:8000
  │  🗄️  DB:         postgresql  |  Name: webapp  |  Host: localhost:5432
  │  📦 GitHub:      git@github.com:user/webapp.git
  │
  │  ── Befehle (als root ausführen) ─────────────────────────────
  │  🔄 Git Pull:    su - webuser -s /bin/bash -c "cd /srv/webapp && git pull"
  │  🚀 Update:      webapp_update.sh          (pull+migrate+static+restart)
  │  🔁 Neustart:    systemctl restart webapp
  │  📊 Status:      systemctl status webapp
  │  📋 Logs live:   journalctl -u webapp -f
  │  💾 Backup:      webapp_backup.sh
  │
  │  ── Zugriff ───────────────────────────────────────────────────
  │  🌍 Django-Admin: http://192.168.1.10/djadmin/
  │  🔐 SSH als User: ssh webuser@192.168.1.10
  │  📥 Key herunterladen: scp root@192.168.1.10:/home/webuser/.ssh/id_ed25519 .
  └─────────────────────────────────────────────────────────────────

  🔀 Zoraxy Reverse Proxy Konfiguration:
     webapp.example.com   →  http://192.168.1.10:80
     shop.example.com     →  http://192.168.1.10:80
     ⚠️  'Pass Host Header' in Zoraxy aktivieren!
```

Skript: `/etc/profile.d/00_django_motd.sh` (shared, liest Registry zur Laufzeit)

---

## Checkpoint / Resume

Wird die Installation unterbrochen, kann sie beim nächsten Aufruf fortgesetzt werden.

**State-Datei:** `/tmp/django_install_<projekt>.state` (chmod 600, automatisch gelöscht nach Erfolg)

Checkpoints (Schritte die übersprungen werden falls bereits erledigt):

| Checkpoint | Was wurde abgeschlossen |
|---|---|
| `input_saved` | Alle Eingaben gespeichert |
| `pkgs_installed` | Systempakete installiert |
| `sshd_configured` | SSH-Server angepasst |
| `appuser_created` | Linux-User + SSH-Key erstellt |
| `db_setup` | Datenbank eingerichtet |
| `project_setup` | Projekt geklont oder neu erstellt |
| `config_done` | .env, settings.py, .gitignore erstellt |
| `logdir_done` | Log-Verzeichnis erstellt |
| `migrations_done` | Migrationen ausgeführt |
| `static_done` | Static files gesammelt |
| `superuser_done` | Django Superuser erstellt |
| `systemd_done` | systemd Service gestartet |
| `logrotate_done` | Log-Rotation konfiguriert |
| `nginx_done` | nginx konfiguriert |
| `registry_done` | Projekt-Registry eingetragen |
| `scripts_done` | Update- und Backup-Skript erstellt |
| `healthcheck_done` | /health/ Endpoint erstellt |
| `motd_done` | MOTD-Skript erstellt |

---

## Erstellte Dateien und Verzeichnisse

```
/srv/<projekt>/
├── .venv/                          ← Python Virtual Environment
├── .env                            ← Secrets (chmod 600, nur App-User)
├── .gitignore                      ← Standard Python/Django Ausschlüsse
├── manage.py
├── <django-modul>/
│   ├── settings.py
│   ├── urls.py
│   ├── wsgi.py
│   └── views.py                    ← Health-Check (nur neues Projekt)
├── app/                            ← Default-App (nur neues Projekt)
├── staticfiles/
└── media/

/srv/djmanager/                     ← Manager Web-Interface (optional)
├── .env                            ← Manager-Secrets (chmod 600)
├── venv/
├── manage.py
├── djmanager/
├── control/
├── logs/                           ← Install-Logs
└── staticfiles/

/home/<appuser>/
└── .ssh/
    ├── id_ed25519                  ← Private Key (600)
    ├── id_ed25519.pub              ← Public Key (644)
    ├── authorized_keys             ← SSH-Login (600)
    ├── known_hosts                 ← GitHub Host-Keys (644)
    └── config                      ← GitHub Port-443-Fallback (600, optional)

/etc/
├── systemd/system/<projekt>.service
├── systemd/system/djmanager.service  ← Manager-Service (optional)
├── nginx/sites-available/<projekt>
├── nginx/sites-enabled/<projekt>   ← Symlink
├── django-servers.d/<projekt>.conf ← Registry für MOTD und Manager
├── profile.d/00_django_motd.sh     ← Geteiltes MOTD-Skript
├── sudoers.d/<projekt>-service     ← (chmod 440)
├── logrotate.d/<projekt>
└── fail2ban/jail.local             ← SSH-Schutz (optional)

/usr/local/bin/
├── <projekt>_update.sh             ← (chmod 755)
├── <projekt>_backup.sh             ← (chmod 755)
├── <projekt>_remove.sh             ← (chmod 755)
└── django_status.sh                ← Globaler Status (chmod 755)

/var/
├── log/<projekt>/                  ← (chmod 750, <appuser>:adm)
│   ├── access.log
│   ├── error.log
│   └── django.log
└── backups/<projekt>/              ← (chmod 700)
    ├── db_YYYYMMDD_HHMMSS.dump
    ├── env_YYYYMMDD_HHMMSS.backup
    └── project_YYYYMMDD_HHMMSS.tar.gz

/tmp/django_install_<projekt>.state ← Checkpoint (wird nach Erfolg gelöscht)
/tmp/djmanager_installs/            ← Manager Install-Locks (SSH-Key-Pause)
```

---

## Log-Management

**Verzeichnis:** `/var/log/<projekt>/` (Rechte 750, Gruppe `adm`)

| Datei | Inhalt |
|---|---|
| `access.log` | Gunicorn Access-Log |
| `error.log` | Gunicorn Error-Log |
| `django.log` | Django ERROR-Logging (nur PROD) |

**logrotate** (`/etc/logrotate.d/<projekt>`):

| Parameter | Wert |
|---|---|
| Rotation | täglich |
| Aufbewahrung | 14 Tage |
| Komprimierung | gzip (verzögert um 1 Tag) |
| Neue Datei | 640, `<appuser>:adm` |
| Nach Rotation | `systemctl reload <projekt>` |

---

## Architektur

```
Linux-User (<appuser>)   →  startet Django, besitzt .venv und .env
PostgreSQL-User (<dbuser>) →  nur DB-Zugriff, kein Shell-Login
/srv/<projekt>             →  Projekt + venv + Secrets
.env                       →  einziger Ort für alle Secrets
/etc/django-servers.d/     →  Registry: Quelle für MOTD, Manager, Status-Skript
```

**Trennung:** Linux-User ≠ DB-User — minimale Rechte (Absicht)

---

## Lokalisierung

Sprache und Zeitzone werden beim Setup abgefragt und in `.env` gespeichert:

```python
LANGUAGE_CODE = os.getenv("LANGUAGE_CODE", "de-de")
TIME_ZONE     = os.getenv("TIME_ZONE", "Europe/Berlin")
```

Beispiele: `de-de / Europe/Berlin` · `en-us / Europe/London` · `fr-fr / Europe/Paris`

---

## Sicherheit

| Maßnahme | Details |
|---|---|
| Keine Secrets im Code | alles in `.env` |
| Keine Secrets in systemd | EnvironmentFile statt Environment= |
| `.env` nur für root + App-User | chmod 600 |
| `.env` im `.gitignore` | wird automatisch eingetragen |
| Admin unter `/djadmin/` | nicht `/admin/` |
| nginx Security-Header | X-Frame-Options, X-Content-Type, X-XSS |
| nginx gzip | Kompressionsstufe 6 |
| fail2ban | 3 Versuche → 1h Sperre |
| SSH-Key ed25519 | moderner Algorithmus statt RSA |
| DB-Verbindungstest | vor Migrationen, Abbruch bei Fehler |
| Manager als root | nur intern — externe Absicherung via Reverse Proxy |

---

## Kompatibilität

- Debian 12+
- Ubuntu 22.04+
- Proxmox LXC (`nesting=1`, `keyctl=1`)
- Normale VMs und Bare-Metal-Server

---

## Was das Skript bewusst nicht macht

- **kein HTTPS** — macht Zoraxy / dein Reverse Proxy
- **kein Firewall-Setup** — ufw / iptables bleibt dem Admin überlassen
- **kein Auto-Scaling** — ein Gunicorn-Prozess pro Projekt
- **keine Manager-Authentifizierung** — für externen Zugriff Reverse Proxy mit Auth vorschalten
