# DjangoMultiDeploy

Interaktives Bash-Installationsskript für **mehrere Django-Projekte auf einem Server** — jedes mit eigenem Gunicorn-Port, nginx-Site, systemd-Service, Datenbank, App-User und SSH-Key.

**Web-Interface (Manager):** Browser-basierte Verwaltung aller Projekte — Install-Wizard, Start/Stop/Restart, Git-Update, ZIP-Deployment, Backups, Logs, Zugriffsstatistiken.

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
| ufw Firewall | automatisch: Port 22/80/443 offen, Gunicorn-Ports gesperrt |
| fail2ban | SSH-Schutz (3 Versuche, 1h Ban) — optional |
| Backup-Skript | DB-Dump + Projekt-Archiv, max. 5 Backups, täglicher Cron |
| Update-Skript | Backup → git pull → migrate → collectstatic → restart |
| ZIP-Deployment | Webapp als ZIP installieren oder per ZIP aktualisieren |
| Health-Check | `/health/` Endpoint mit DB-Test |
| MOTD | zeigt beim Login alle Django-Projekte mit Status und Befehlen |
| Checkpoint/Resume | unterbrochene Installationen fortsetzbar |
| **Web-Interface** | **Browser-Verwaltung aller Projekte (Manager)** |

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

```
╔═══════════════════════════════════════════════════════════════╗
║          DjangoMultiDeploy — Was installieren?               ║
╠═══════════════════════════════════════════════════════════════╣
║  1) Django-Projekt           (neue Django-Webanwendung)      ║
║  2) DjangoMultiDeploy Manager (Web-Interface)                ║
║  3) Beides                                                   ║
╚═══════════════════════════════════════════════════════════════╝
```

| Option | Was passiert |
|---|---|
| **1** | Django-Projekt einrichten (Gunicorn, nginx, DB, systemd, …) |
| **2** | Nur Manager installieren (venv, nginx, systemd) |
| **3** | Beides — Manager + neues Django-Projekt |

---

## Quellcode-Optionen beim Setup

Beim Setup eines neuen Django-Projekts gibt es drei Quellen:

| Option | Beschreibung |
|---|---|
| **Leeres Projekt** | `django-admin startproject` — Grundstruktur wird erzeugt |
| **GitHub Repository** | öffentlich (HTTPS) oder privat (SSH mit Deploy-Key) — `git clone` |
| **ZIP hochladen** | Webapp als ZIP-Datei → wird entpackt, Requirements installiert, DB migriert |

Die ZIP-Option funktioniert direkt mit dem **„Code → Download ZIP"** Button auf GitHub — kein Umbenennen oder Umstrukturieren nötig. Das Tool erkennt automatisch ob ein einzelnes Top-Level-Verzeichnis vorhanden ist (GitHub-Style) und bereinigt die Struktur.

---

## Django-Projekt Voraussetzungen (für ZIP und GitHub)

Damit eine bestehende Django-Webapp mit diesem Tool sauber funktioniert, muss das Projekt folgende Voraussetzungen erfüllen:

### Pflicht

| Voraussetzung | Details |
|---|---|
| **`manage.py` im Root** | Das Tool sucht `manage.py` im obersten Verzeichnis |
| **`requirements.txt`** | Muss alle Abhängigkeiten enthalten — gunicorn und python-dotenv werden zusätzlich immer installiert |
| **`wsgi.py` vorhanden** | Das Django-Modul (Verzeichnis mit `wsgi.py`) wird automatisch erkannt |
| **Settings per Umgebungsvariablen** | `settings.py` muss Werte aus Umgebungsvariablen / `.env` lesen (siehe unten) |

### settings.py — Pflichtfelder via `.env`

Das Tool erzeugt automatisch eine `.env`-Datei. Die `settings.py` muss diese Werte einlesen. Minimale kompatible Konfiguration:

```python
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

SECRET_KEY = os.getenv('SECRET_KEY')
DEBUG = os.getenv('DEBUG', 'False') == 'True'

ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', '').split(',') if h.strip()]

# Datenbank — das Tool setzt DB_ENGINE, DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT
DATABASES = {
    'default': {
        'ENGINE':   os.getenv('DB_ENGINE', 'django.db.backends.sqlite3'),
        'NAME':     os.getenv('DB_NAME', BASE_DIR / 'db.sqlite3'),
        'USER':     os.getenv('DB_USER', ''),
        'PASSWORD': os.getenv('DB_PASSWORD', ''),
        'HOST':     os.getenv('DB_HOST', ''),
        'PORT':     os.getenv('DB_PORT', ''),
    }
}

# Static/Media — Pflicht für collectstatic
STATIC_URL  = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL   = '/media/'
MEDIA_ROOT  = BASE_DIR / 'media'
```

> **`python-dotenv`** muss in `requirements.txt` stehen oder wird automatisch zusätzlich installiert.

### Empfohlen (für PROD-Modus)

```python
# Für Reverse-Proxy (Zoraxy / nginx)
USE_X_FORWARDED_HOST = os.getenv('USE_X_FORWARDED_HOST', 'False') == 'True'
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') if os.getenv('MODE') == 'prod' else None

# CSRF für Reverse-Proxy
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()]
```

Das Tool setzt `CSRF_TRUSTED_ORIGINS` automatisch aus den ALLOWED_HOSTS in der `.env`.

### Was das Tool automatisch erledigt

- Setzt `ALLOWED_HOSTS`, `SECRET_KEY`, `DEBUG`, alle DB-Variablen in `.env`
- Benennt `/admin/` zu `/djadmin/` um (in `urls.py`, beide Anführungszeichen-Stile)
- Erstellt Superuser (`createsuperuser --noinput`)
- Führt `migrate` und `collectstatic --noinput` aus
- Richtet `STATIC_ROOT = /srv/<projekt>/staticfiles/` und `MEDIA_ROOT = /srv/<projekt>/media/` ein (falls nicht in settings.py vorhanden)

### Was NICHT in der ZIP enthalten sein muss (und soll)

| Ausschließen | Warum |
|---|---|
| `.env` | Wird vom Tool erzeugt — enthält Secrets |
| `.venv/` | Wird vom Tool neu angelegt — kann sich auf jedem Server unterscheiden |
| `staticfiles/` | Wird per `collectstatic` erzeugt |
| `media/` | User-Uploads — wird gesondert behandelt |
| `__pycache__/` / `*.pyc` | Unnötig, server-spezifisch |
| `*.log` | Logs werden in `/var/log/<projekt>/` verwaltet |

### ZIP-Struktur — beide Formate werden unterstützt

**Flaches ZIP (direkte Struktur):**
```
manage.py
requirements.txt
myapp/
  settings.py
  urls.py
  wsgi.py
```

**GitHub-Style ZIP (ein Top-Level-Verzeichnis):**
```
myapp-main/
  manage.py
  requirements.txt
  myapp/
    settings.py
    urls.py
    wsgi.py
```

Beide Formate werden automatisch erkannt und korrekt entpackt.

---

## Eingaben beim Setup (Django-Projekt)

| Eingabe | Standard | Hinweis |
|---|---|---|
| **Projektname** | — | 3–50 Zeichen, a-z A-Z 0-9 _ - |
| **Quellcode-Quelle** | Leer | GitHub / ZIP / Leer |
| **GitHub URL** | — | nur bei GitHub-Option |
| **Modus** | 1 = DEV | 2 = PROD |
| **Gunicorn-Port** | nächster freier Port ≥ 8000 | automatisch erkannt |
| **ALLOWED_HOSTS** | alle lokalen IPs + localhost + FQDN | kommasepariert |
| **Datenbank-Typ** | — | 1 = PostgreSQL, 2 = MySQL, 3 = SQLite |
| **DB-Modus** | — | 1 = lokal installieren, 2 = remote |
| **DB-Name** | `<projektname>` | |
| **DB-User** | `<projektname>_user` | |
| **DB-Host** | localhost | |
| **DB-Port** | 5432 (PG) / 3306 (MySQL) | |
| **DB-Passwort** | — | Pflicht |
| **Linux App-User** | — | Pflicht, beginnt mit Buchstabe |
| **Django SECRET_KEY** | auto (32 Hex-Zeichen) | leer lassen = wird generiert |
| **Gunicorn Worker** | 2×CPU+1 | automatisch berechnet |
| **Sprachcode** | `de-de` | z.B. `en-us`, `fr-fr` |
| **Zeitzone** | `Europe/Berlin` | z.B. `Europe/London` |
| **SMTP Host** | leer (deaktiviert) | optional |
| **Backup-Uhrzeit** | `02:00` | täglicher Cron |

---

## NONINTERACTIVE-Modus

Das Skript unterstützt einen vollständig nicht-interaktiven Modus für CI/CD oder das Web-Interface:

```bash
export NONINTERACTIVE=true
export PROJECTNAME=myapp
export APPUSER=myuser
export MODESEL=2
export SOURCE_TYPE=github          # github | zip | new
export GITHUB_REPO_URL=git@github.com:user/repo.git
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

**Für ZIP-Modus:**
```bash
export SOURCE_TYPE=zip
export UPLOAD_ZIP_PATH=/tmp/myapp.zip
```

| Umgebungsvariable | Bedeutung | Standard |
|---|---|---|
| `NONINTERACTIVE` | `true` = alle Prompts deaktivieren | `false` |
| `_INSTALL_SEL` | 1=Projekt, 2=Manager, 3=Beides | `3` |
| `PROJECTNAME` | Projektname | — |
| `APPUSER` | Linux App-User | — |
| `MODESEL` | 1=DEV, 2=PROD | `1` |
| `SOURCE_TYPE` | `github` / `zip` / `new` | `new` |
| `GITHUB_REPO_URL` | GitHub URL (nur bei `SOURCE_TYPE=github`) | leer |
| `UPLOAD_ZIP_PATH` | Pfad zur ZIP-Datei (nur bei `SOURCE_TYPE=zip`) | — |
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

Der Manager ist eine Django-App, die alle installierten Projekte über den Browser verwaltet. Er läuft hinter nginx und ist nur über einen konfigurierten Hostnamen erreichbar (Port 8888 ist nach außen gesperrt).

### Features

| Bereich | Funktion |
|---|---|
| **Dashboard** | Übersicht aller Projekte — Server-RAM/Disk/Load, Status, letztes Backup, Quick-Actions |
| **Install-Wizard** | Formular → Quellcode wählen (Leer / GitHub / ZIP) → Live-Terminal |
| **Projektdetail** | Start / Stop / Restart / Git-Update / Backup / ZIP-Update |
| **ALLOWED_HOSTS** | Hosts direkt im Browser hinzufügen/entfernen — nginx wird automatisch synchronisiert |
| **Firewall-Status** | ufw-Status und Port-Übersicht pro Projekt |
| **Backups** | Liste, manuell löschen, max. 5 Backups pro Projekt |
| **Zugriffsstatistiken** | nginx-Log-Auswertung: Requests/Tag, Status-Codes, Top-URLs, Top-IPs, Antwortzeit |
| **Service-Ereignisse** | systemd-Journal: Starts, Fehler, Stops der letzten 14 Tage |
| **Log-Viewer** | systemd Journal, nginx Access + Error-Logs |
| **SSH-Key** | Key im Browser anzeigen und herunterladen |
| **Remove-Wizard** | Granulares Entfernen: Dateien / DB / User / Backup / Logs |

### Dashboard

Das Dashboard zeigt oben eine **Server-Ressourcen-Leiste**:
- RAM-Auslastung (MB + %, farblich: grün/gelb/rot)
- Disk-Auslastung auf `/` (GB + %)
- Load-Average (1m / 5m / 15m)

Jede Projektkarte zeigt: Modus, Datenbank, Host, letztes Backup (Warnung falls keins vorhanden) und Schnell-Buttons für Start/Stop/Restart/Update direkt auf dem Dashboard.

### ZIP-Update für bestehende Projekte

In der Projektdetailseite kann unter **Update & Backup** eine neue ZIP-Datei hochgeladen werden:

1. ZIP-Datei auswählen (GitHub "Download ZIP" funktioniert direkt)
2. „Hochladen & Aktualisieren" klicken
3. Das Tool extrahiert die ZIP, führt `pip install`, `migrate`, `collectstatic` aus und startet den Service neu
4. Ausgabe wird live im Browser angezeigt

**Geschützt (werden nie überschrieben):** `.env`, `.venv/`, `media/`, `staticfiles/`

### Installationsweg

```bash
sudo ./Installv2.sh
# → Option 2 oder 3 wählen
# → Manager-Hostname eingeben (z.B. manager.intern.example.com)
```

DNS oder `/etc/hosts` auf dem eigenen PC auf die Server-IP zeigen lassen:
```
192.168.1.10  manager.intern.example.com
```

Manager dann erreichbar unter: `http://manager.intern.example.com/`

### Manager-Verzeichnis

```
/srv/djmanager/
├── .env                    ← SECRET_KEY, ALLOWED_HOSTS (chmod 600)
├── .venv/                  ← Python venv
├── manage.py
├── djmanager/              ← Django-Einstellungen
│   ├── settings.py
│   └── urls.py
├── control/                ← Views, Utils, Templates
│   ├── views.py
│   ├── utils.py
│   ├── urls.py
│   └── templates/control/
│       ├── base.html       ← Bootstrap 5 Dark-Theme
│       ├── dashboard.html
│       ├── install_form.html
│       ├── install_progress.html
│       ├── project_detail.html
│       ├── log_viewer.html
│       ├── ssh_key.html
│       ├── remove_confirm.html
│       └── remove_done.html
├── logs/                   ← Install-Logs (pro Aufruf)
└── staticfiles/
```

---

## Firewall (ufw)

Die Firewall wird **automatisch** konfiguriert:

| Regel | Status |
|---|---|
| Port 22 (SSH) | ✅ erlaubt |
| Port 80 (HTTP nginx) | ✅ erlaubt |
| Port 443 (HTTPS) | ✅ erlaubt |
| Ports 8000–8999 (Gunicorn intern) | 🔒 extern gesperrt |
| Port 8888 (Manager intern) | 🔒 extern gesperrt |
| Ausgehender Traffic | ✅ erlaubt |
| Eingehender Traffic (Rest) | 🔒 standardmäßig gesperrt |

Gunicorn-Ports und der Manager-Port sind nur intern erreichbar (`127.0.0.1`). Der öffentliche Zugriff erfolgt ausschließlich über nginx auf Port 80/443.

> In LXC-Containern wo ufw nicht verfügbar ist, wird dieser Schritt automatisch übersprungen ohne die Installation zu unterbrechen.

---

## Multi-Server-Betrieb

```
webapp    →  Gunicorn: 127.0.0.1:8000  →  nginx (server_name: webapp.example.com)
shopapp   →  Gunicorn: 127.0.0.1:8001  →  nginx (server_name: shop.example.com)
intranet  →  Gunicorn: 127.0.0.1:8002  →  nginx (server_name: intern.example.com)
djmanager →  127.0.0.1:8888  →  nginx (server_name: manager.intern.example.com)
```

**PostgreSQL:** Ein einziger PostgreSQL-Server reicht für alle Projekte. Das Skript zeigt beim Setup vorhandene Datenbanken an.

---

## Zoraxy Reverse Proxy

```
Internet
   ↓
Zoraxy  (anderer Server, SSL-Terminierung)
   ↓   Ziel: http://<DIESER-SERVER>:80  +  Host-Header weiterleiten
nginx   (dieser Server, Port 80, server_name-Routing)
   ↓
Gunicorn  (127.0.0.1:8000 / :8001 / :8002 …)
   ↓
Django
```

**Zoraxy-Einstellung:** `Pass Host Header` / `Preserve Host` aktivieren — sonst schlägt Django CSRF fehl.

Im PROD-Modus setzt Django automatisch:
```python
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True
```

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

---

## GitHub Integration

**Ablauf bei privatem Repo:**
1. ed25519-Key wird generiert und angezeigt
2. Public Key zu GitHub (Settings → SSH Keys) hinzufügen
3. Skript wartet auf Bestätigung
4. SSH-Verbindung wird getestet:
   - Port 22 → `git@github.com`
   - Fallback Port 443 → automatisch falls Port 22 blockiert

**Im Web-Interface:** Der Public Key wird direkt im Browser angezeigt und ist per Klick herunterladbar.

Der **Django-Modul-Name** (Verzeichnis mit `wsgi.py`) wird automatisch erkannt.

---

## Update-Skript

Erstellt unter `/usr/local/bin/<projekt>_update.sh`

```
1. Backup erstellen  (sichert vor dem Update)
2. git config safe.directory setzen  (vermeidet dubious-ownership Fehler)
3. git pull  (als App-User, mit SSH-Key)
4. pip install -r requirements.txt  (falls vorhanden)
5. python manage.py migrate
6. python manage.py collectstatic --noinput
7. sudo systemctl restart <projekt>
8. nginx -t && systemctl reload nginx
```

Über den **Manager** per Klick ausführbar (Projektdetail → „Git Pull + Update").

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
**Maximal 5 Backups** werden pro Projekt aufbewahrt — ältere werden automatisch gelöscht.
Täglicher Cron zur eingestellten Uhrzeit (Standard: 02:00).

Im **Manager** können einzelne Backups manuell gelöscht werden (Projektdetail → Backups → Trash-Icon).

---

## Zugriffsstatistiken (Manager)

In der Projektdetailseite können per Klick Zugriffsstatistiken geladen werden (lazy, kein Page-Load-Overhead):

| Statistik | Beschreibung |
|---|---|
| **Requests / 7 Tage** | Balkendiagramm der letzten 7 Tage |
| **Status-Codes** | Aufteilung 2xx / 3xx / 4xx / 5xx |
| **Ø Antwortzeit** | in ms — nur wenn nginx-Log `$request_time` enthält |
| **Top 10 URLs** | meistbesuchte Pfade (ohne Static/Media) |
| **Top 10 IPs** | häufigste Client-IPs |
| **Service-Ereignisse** | Start / Fehler / Stop aus systemd-Journal (14 Tage) |

Das nginx-Log-Format `reqtime` mit `$request_time` wird automatisch bei neuen Projekten eingerichtet. Bei bereits installierten Projekten kann das Log-Format manuell ergänzt werden:

```bash
# /etc/nginx/conf.d/reqtime_log.conf
log_format reqtime '$remote_addr - $remote_user [$time_local] "$request" '
                   '$status $body_bytes_sent "$http_referer" '
                   '"$http_user_agent" $request_time';

# In der nginx site config:
access_log /var/log/nginx/PROJEKTNAME.access.log reqtime;
```

---

## System-Voraussetzungs-Checks

| Check | Was wird geprüft |
|---|---|
| `/tmp` beschreibbar | `touch /tmp/.test` |
| Root-FS beschreibbar | `touch /root/.test` |
| Freier Speicher `/` | mind. 3 GB |
| Freier Speicher `/tmp` | mind. 512 MB |
| RAM | mind. 512 MB (Warnung < 1 GB) |
| DNS-Auflösung | `getent hosts pypi.org` |
| HTTPS-Verbindung | `curl pypi.org` |
| Systemzeit | > 2023 |

> **Proxmox LXC:** Empfohlen: `nesting=1`, `keyctl=1`. Falls `/tmp` read-only ist, versucht das Skript automatisch `mount -o remount,rw /tmp`.

---

## Checkpoint / Resume

| Checkpoint | Was wurde abgeschlossen |
|---|---|
| `input_saved` | Alle Eingaben gespeichert |
| `pkgs_installed` | Systempakete installiert |
| `ufw_base_done` | Firewall-Grundkonfiguration |
| `sshd_configured` | SSH-Server angepasst |
| `appuser_created` | Linux-User + SSH-Key erstellt |
| `db_setup` | Datenbank eingerichtet |
| `project_setup` | Projekt geklont / entpackt / erstellt |
| `config_done` | .env, settings.py, .gitignore erstellt |
| `logdir_done` | Log-Verzeichnis erstellt |
| `migrations_done` | Migrationen ausgeführt |
| `static_done` | Static files gesammelt |
| `superuser_done` | Django Superuser erstellt |
| `systemd_done` | systemd Service gestartet |
| `firewall_done` | Gunicorn-Port in ufw gesperrt |
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
├── .env                            ← Secrets (chmod 600)
├── .gitignore
├── manage.py
├── <django-modul>/
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── staticfiles/
└── media/

/srv/djmanager/                     ← Manager Web-Interface
├── .env
├── .venv/
├── manage.py
├── djmanager/
├── control/
├── logs/
└── staticfiles/

/home/<appuser>/.ssh/
├── id_ed25519                      ← Private Key (600)
├── id_ed25519.pub                  ← Public Key (644)
└── authorized_keys                 ← SSH-Login (600)

/etc/
├── nginx/conf.d/reqtime_log.conf   ← nginx Log-Format mit $request_time
├── nginx/sites-available/<projekt>
├── nginx/sites-enabled/<projekt>
├── nginx/sites-available/djmanager
├── django-servers.d/<projekt>.conf ← Registry
├── systemd/system/<projekt>.service
├── systemd/system/djmanager.service
├── sudoers.d/<projekt>-service
├── logrotate.d/<projekt>
└── fail2ban/jail.local             ← optional

/usr/local/bin/
├── <projekt>_update.sh
├── <projekt>_backup.sh
├── <projekt>_remove.sh
└── django_status.sh

/var/log/<projekt>/
├── access.log
├── error.log
└── django.log

/var/log/nginx/<projekt>.access.log ← nginx Access-Log (reqtime Format)

/var/backups/<projekt>/             ← max. 5 Backups
├── db_YYYYMMDD_HHMMSS.dump
├── env_YYYYMMDD_HHMMSS.backup
└── project_YYYYMMDD_HHMMSS.tar.gz
```

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
| ufw Firewall | automatisch konfiguriert — Gunicorn/Manager intern |
| fail2ban | 3 Versuche → 1h SSH-Sperre (optional) |
| SSH-Key ed25519 | moderner Algorithmus statt RSA |
| DB-Verbindungstest | vor Migrationen, Abbruch bei Fehler |
| ZIP-Extraktion | Path-Traversal-Schutz, nur `.zip`-Dateien |
| Manager via nginx | Port 8888 nach außen gesperrt — Zugriff nur über Hostname |

---

## Kompatibilität

- Debian 12+
- Ubuntu 22.04+
- Proxmox LXC (`nesting=1`, `keyctl=1`)
- Normale VMs und Bare-Metal-Server

---

## Was das Skript bewusst nicht macht

- **kein HTTPS** — macht Zoraxy / dein Reverse Proxy
- **kein Auto-Scaling** — ein Gunicorn-Prozess pro Projekt
- **keine externe Manager-Authentifizierung** — für externen Zugriff Reverse Proxy mit Auth vorschalten
