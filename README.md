<div align="center">

# 🚀 DjangoMultiDeploy

**Mehrere Django-Projekte auf einem Server — installiert, verwaltet und überwacht über ein Web-Interface.**

[![Bash](https://img.shields.io/badge/Installer-Bash-4EAA25?logo=gnubash&logoColor=white)](Installv2.sh)
[![Django](https://img.shields.io/badge/Manager-Django%204.2-092E20?logo=django&logoColor=white)](manager/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](#kompatibilität)
[![OS](https://img.shields.io/badge/OS-Debian%2012%2B%20%7C%20Ubuntu%2022.04%2B-A81D33?logo=debian&logoColor=white)](#kompatibilität)
[![LXC](https://img.shields.io/badge/Proxmox-LXC%20ready-E57000?logo=proxmox&logoColor=white)](#kompatibilität)

Jedes Projekt bekommt seinen eigenen **Gunicorn-Port**, **nginx-Site**, **systemd-Service**,
seine eigene **Datenbank**, einen **Linux-App-User** und einen **SSH-Deploy-Key** —
vollautomatisch, wiederholbar und mit Checkpoint/Resume.

[Schnellstart](#-schnellstart) ·
[Features](#-features) ·
[Web-Manager](#-der-web-manager) ·
[Sicherheit](#-sicherheit) ·
[Architektur](#-architektur) ·
[FAQ / Voraussetzungen](#-voraussetzungen-für-eigene-projekte)

</div>

---

## ✨ Features

| | Installer (`Installv2.sh`) | | Web-Manager |
|---|---|---|---|
| 🐍 | Python-venv pro Projekt unter `/srv/<projekt>` | 📊 | Dashboard mit Server-Ressourcen (RAM/Disk/Load) & Projektstatus |
| 🌐 | nginx pro Projekt (server_name-Routing, gzip, Caching, Security-Header) | 🧙 | Install-Wizard mit Live-Terminal (Leer / GitHub / ZIP) |
| ⚙️ | systemd-Service mit Autostart & Restart=always | ▶️ | Start / Stop / Restart / Git-Update / ZIP-Update per Klick |
| 🔌 | **WSGI oder ASGI** wählbar (Gunicorn bzw. Gunicorn+Uvicorn-Worker) — WebSockets/Django Channels mit WebSocket-fähigem nginx | | |
| 🗄️ | PostgreSQL / MySQL / SQLite — lokal oder remote | 🔑 | Deploy-Key-Verwaltung (global, pro Projekt, Registry) |
| 🔥 | ufw-Firewall automatisch (Gunicorn-Ports nur intern) | 🧾 | Log-Viewer: systemd-Journal, nginx Access/Error |
| 🔐 | Let's Encrypt optional (certbot, opt-in) | 📈 | Zugriffsstatistiken + Ressourcen-Verlauf (RAM/Disk-Sparkline) |
| 🛡️ | fail2ban optional (SSH-Schutz) | 🔔 | Benachrichtigungen (E-Mail/Webhook) bei Backup-Fehler, Dienstausfall, CVE |
| 💾 | Tägliche Backups (DB-Dump + Projektarchiv, Rotation) | 🔍 | Security-Scanner: HTTP/TLS-Header, Portscan, Nuclei, OWASP ZAP, pip-audit |
| 🔁 | Checkpoint/Resume — abgebrochene Installs fortsetzbar | 👥 | Benutzerverwaltung mit Rollen (Admin / Operator / Viewer) + Projektrechten |
| 📦 | ZIP-Deployment (GitHub „Download ZIP" direkt nutzbar) | 🔐 | Login, TOTP-2FA mit Backup-Codes, IP-Whitelist, Audit-Log |
| 🤖 | NONINTERACTIVE-Modus für CI/CD | 🧰 | .env-Editor, Migrations-Übersicht, pip-Updates, Favoriten-Befehle |

---

## ⚡ Schnellstart

```bash
git clone https://github.com/Manjo80/DjangoMultiDeploy.git
cd DjangoMultiDeploy
chmod +x Installv2.sh
sudo ./Installv2.sh
```

> Muss als **root** auf Debian 12+ oder Ubuntu 22.04+ ausgeführt werden.

Das Skript fragt interaktiv, was installiert werden soll:

| Option | Beschreibung |
|:---:|---|
| **1** | Neues Django-Projekt (Gunicorn, nginx, DB, systemd, Backups, …) |
| **2** | Nur den Web-Manager installieren |
| **3** | Beides — Manager + erstes Django-Projekt |

Nach der Manager-Installation: DNS-Eintrag (oder `/etc/hosts`) auf die Server-IP zeigen lassen
und den Manager unter dem gewählten Hostnamen öffnen, z. B. `http://manager.intern.example.com/`.
Beim ersten Start wird ein Admin-Konto angelegt.

---

## 🖥️ Der Web-Manager

Der Manager ist eine Django-App hinter nginx (Gunicorn-Port 8888 ist nach außen gesperrt)
und verwaltet alle installierten Projekte im Browser.

### Projekte verwalten

- **Dashboard** — alle Projekte mit Status, Modus, DB, letztem Backup; Server-Leiste mit RAM-, Disk- und Load-Anzeige; Quick-Actions direkt auf der Karte
- **Install-Wizard** — neues Projekt per Formular anlegen (leeres Projekt, GitHub-Repo oder ZIP-Upload), Installationsfortschritt als Live-Terminal im Browser, Abbruch jederzeit möglich
- **Projektdetail** — Start/Stop/Restart, Git-Pull-Update, ZIP-Update, Backups (anzeigen/löschen), ALLOWED_HOSTS-Verwaltung mit automatischem nginx-Sync, nginx-Konfiguration einsehen, Konfiguration als Datei exportieren
- **Projekt klonen** — bestehendes Projekt als Vorlage für eine neue Instanz verwenden
- **Reset & Remove-Wizard** — granular entfernen: Dateien / Datenbank / Linux-User / Backups / Logs

### Betrieb & Diagnose

- **Log-Viewer** — systemd-Journal, nginx Access- und Error-Logs
- **Zugriffsstatistiken** — Requests/Tag (7 Tage), Status-Code-Verteilung, Top-10-URLs und -IPs, ⌀ Antwortzeit (via `$request_time`), Service-Ereignisse aus dem systemd-Journal (14 Tage)
- **Ressourcen-Verlauf** — RAM-/Disk-Auslastung der letzten 24 h als Sparkline im Dashboard (Samples werden automatisch gespeichert, 7 Tage Aufbewahrung)
- **Migrations-Übersicht** — offene Django-Migrationen pro Projekt anzeigen und ausführen
- **pip-Verwaltung** — veraltete Pakete anzeigen und gezielt aktualisieren (mit Versions-Pinning)
- **Favoriten-Befehle** — eigene `manage.py`-Kommandos als Quick-Action-Buttons pro Projekt
- **Eigene Update-Befehle** — zusätzliche `manage.py`-Schritte (z. B. `load_glossary`, `loaddata seed.json`, `clearsessions`), die automatisch beim „Git Pull + Update" laufen — pro Projekt frei zusammenstellbar, einzeln aktivierbar/deaktivierbar und jederzeit anpassbar
- **.env-Editor** — Umgebungsvariablen pro Projekt und für den Manager direkt im Browser bearbeiten (Secrets maskiert)
- **Firewall** — ufw-Status und Portverwaltung im Browser
- **Datenbanken & Benutzer** — Übersicht aller PostgreSQL-/MySQL-**Datenbanken** (Größe, Owner, Projektzuordnung), aller **DB-Benutzer/Rollen** (auch solche ohne eigene DB) und aller **Linux-App-User**. Alles wird gegen die Projekt-Registry abgeglichen und als *in Benutzung*, *verwaist* oder *System* markiert; **verwaiste** Objekte lassen sich gezielt aufräumen. Schutzregeln: System-DBs/-Benutzer und aktiv genutzte Objekte sind gesperrt, DB-Rollen mit eigener DB müssen diese erst verlieren, und Linux-User werden nur gelöscht, wenn sie eindeutig tool-erzeugte App-User sind (Deploy-Key vorhanden) — echte Login-Konten nie.

### TLS & Benachrichtigungen

- **Let's Encrypt (optional)** — Zertifikat pro Projekt-Hostname per certbot anfordern/erneuern, direkt aus dem Projektdetail. Rein opt-in: Setups mit vorgelagertem Reverse Proxy (eigene Zertifikate) brauchen es nicht. Im Installer per Abfrage `INSTALL_CERTBOT` (Standard: nein).
- **Benachrichtigungen** — E-Mail (SMTP) und/oder Webhook (Slack/Discord/Mattermost/generisch) bei fehlgeschlagenem Backup, ausgefallenem Dienst oder gefundener Schwachstelle (pip-audit). Pro Ereignis ein-/ausschaltbar, mit Test-Versand. Dienst-Überwachung optional per Cron (`manage.py check_health`).

### Security-Scanner

| Scan | Beschreibung |
|---|---|
| **HTTP/TLS** | Security-Header-Check (CSP, HSTS, X-Frame-Options, …) pro Projekt, Manager oder beliebigen Host |
| **Portscan** | Offene Ports eines Hosts prüfen |
| **Nuclei** | Template-basierter Schwachstellenscan (Installation & Updates aus dem Manager heraus) |
| **OWASP ZAP** | Baseline-Scan (Installation & Updates aus dem Manager heraus) |
| **pip-audit** | Bekannte CVEs in den Python-Abhängigkeiten jedes Projekts |

Langläufer (Nuclei, ZAP, Updates) laufen als Hintergrund-Jobs mit Polling — funktioniert auch hinter Cloudflare/Proxies mit kurzen Timeouts.

### Zugriffsschutz

| Mechanismus | Details |
|---|---|
| **Login + Rollen** | Admin / Operator / Viewer; Viewer sind read-only |
| **Projektrechte** | Operator/Viewer sehen nur explizit freigegebene Projekte |
| **2FA (TOTP)** | QR-Code-Setup, Backup-Codes (nur gehasht gespeichert), optional erzwingbar für alle Nutzer |
| **Login-Schutz** | Account-Sperre nach 5 Fehlversuchen (15 min), zusätzlich per-IP-Throttling |
| **IP-Whitelist** | Zugriff auf IPs/CIDRs einschränkbar; X-Forwarded-For nur von vertrauenswürdigen Proxies akzeptiert |
| **Audit-Log** | Wer hat wann was an welchem Projekt gemacht (inkl. IP) |
| **Session-Verwaltung** | Konfigurierbarer Timeout, Strict-SameSite, HttpOnly-Cookies |

---

## 🏗️ Architektur

```
Internet
   │
   ▼
Zoraxy / Reverse Proxy          (SSL-Terminierung, optional auf anderem Server)
   │  Host-Header weiterleiten ("Pass Host Header")
   ▼
nginx :80                       (server_name-Routing, Security-Header, gzip)
   ├── webapp.example.com   ──► Gunicorn 127.0.0.1:8000 ──► Django  /srv/webapp
   ├── shop.example.com     ──► Gunicorn 127.0.0.1:8001 ──► Django  /srv/shopapp
   ├── intern.example.com   ──► Gunicorn 127.0.0.1:8002 ──► Django  /srv/intranet
   └── manager.example.com  ──► Gunicorn 127.0.0.1:8888 ──► Manager /srv/djmanager
```

- Gunicorn-Ports (8000–8999) und der Manager-Port 8888 sind per ufw **nur intern** erreichbar — der öffentliche Zugriff läuft ausschließlich über nginx.
- Ein PostgreSQL-/MySQL-Server reicht für alle Projekte; das Setup zeigt vorhandene Datenbanken an.
- **Zoraxy:** `Pass Host Header` aktivieren, sonst schlägt Djangos CSRF-Prüfung fehl.

### Was pro Projekt entsteht

```
/srv/<projekt>/                      Projektcode + .venv + .env (chmod 600)
/etc/nginx/sites-available/<projekt> nginx-Site (+ Symlink in sites-enabled)
/etc/systemd/system/<projekt>.service
/etc/django-servers.d/<projekt>.conf Registry-Eintrag (Quelle für den Manager)
/usr/local/bin/<projekt>_update.sh   Backup → git pull → migrate → collectstatic → restart
/usr/local/bin/<projekt>_backup.sh   DB-Dump + .env + Projektarchiv, Rotation (max. 5)
/var/log/<projekt>/                  App-Logs (+ logrotate)
/var/backups/<projekt>/              Backups (Rechte 700)
/home/<appuser>/.ssh/id_ed25519      Deploy-Key (ed25519)
```

---

## 🔒 Sicherheit

| Bereich | Maßnahme |
|---|---|
| Secrets | ausschließlich in `.env` (chmod 600, automatisch in `.gitignore`); systemd nutzt `EnvironmentFile` |
| Django-Admin | unter `/djadmin/` statt `/admin/` |
| Netzwerk | ufw: nur 22/80/443 offen; Gunicorn & Manager nur via Loopback; fail2ban optional |
| Manager-Härtung | CSP mit Nonce, HSTS-fähig, Secure/HttpOnly/Strict-Cookies, Permissions-Policy, X-Frame-Options DENY |
| Authentifizierung | Login mit Rollen, TOTP-2FA (Secrets verschlüsselt at rest), Backup-Codes gehasht, Lockout + IP-Throttling |
| Nachvollziehbarkeit | Audit-Log aller Aktionen inkl. IP-Adresse |
| Deployment | ZIP-Extraktion mit Path-Traversal-Schutz; DB-Verbindungstest vor Migrationen; SSH-Host-Key-Pinning für GitHub |
| Abhängigkeiten | pip-audit-Integration; Versions-Pinning bei pip-Upgrades |

> **Empfehlung:** Den Manager nur im LAN/VPN oder hinter einem Reverse Proxy mit zusätzlicher Auth betreiben und die IP-Whitelist aktivieren.

---

## 📋 Voraussetzungen für eigene Projekte

> 📄 Ausführliche Checkliste inkl. fertigem `settings.py`-Muster, exakter Env-Variablen
> und aller Fallstricke: **[docs/WEBAPP-COMPATIBILITY.md](docs/WEBAPP-COMPATIBILITY.md)**

Damit ein bestehendes Django-Projekt (per GitHub oder ZIP) sauber deployt werden kann:

| Pflicht | Details |
|---|---|
| `manage.py` im Projekt-Root | das Tool sucht es im obersten Verzeichnis |
| `requirements.txt` | alle Abhängigkeiten; `gunicorn` und `python-dotenv` werden zusätzlich immer installiert |
| `wsgi.py` vorhanden | das Django-Modul wird darüber automatisch erkannt |
| Settings aus `.env` | `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, DB-Variablen müssen aus Umgebungsvariablen gelesen werden |

<details>
<summary><b>Minimale kompatible <code>settings.py</code> (Beispiel)</b></summary>

```python
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

SECRET_KEY = os.getenv('SECRET_KEY')
DEBUG = os.getenv('DEBUG', 'False') == 'True'
ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', '').split(',') if h.strip()]

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

STATIC_URL  = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL   = '/media/'
MEDIA_ROOT  = BASE_DIR / 'media'

# Empfohlen für PROD hinter Reverse Proxy:
USE_X_FORWARDED_HOST = os.getenv('USE_X_FORWARDED_HOST', 'False') == 'True'
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()]
```

</details>

<details>
<summary><b>ZIP-Format & ausgeschlossene Dateien</b></summary>

Beide ZIP-Strukturen werden automatisch erkannt — der GitHub-Button **„Code → Download ZIP"** funktioniert direkt:

```
# Flach                          # GitHub-Style (ein Top-Level-Ordner)
manage.py                        myapp-main/
requirements.txt                   manage.py
myapp/                             requirements.txt
  settings.py                      myapp/
  wsgi.py                            settings.py
                                     wsgi.py
```

**Nicht in die ZIP gehören:** `.env` (wird erzeugt), `.venv/`, `staticfiles/` (collectstatic), `media/`, `__pycache__/`, `*.log`.

Beim **ZIP-Update** eines bestehenden Projekts werden `.env`, `.venv/`, `media/` und `staticfiles/` nie überschrieben.

</details>

<details>
<summary><b>Was das Tool automatisch erledigt</b></summary>

- `.env` mit `SECRET_KEY`, `ALLOWED_HOSTS`, `DEBUG`, DB-Variablen erzeugen
- `/admin/` → `/djadmin/` umbenennen (in `urls.py`)
- Superuser anlegen, `migrate` + `collectstatic` ausführen
- `STATIC_ROOT`/`MEDIA_ROOT` setzen, falls nicht vorhanden
- nginx-Logformat mit `$request_time` für die Statistiken einrichten

</details>

---

## 🤖 NONINTERACTIVE-Modus (CI/CD)

Alle Prompts lassen sich über Umgebungsvariablen vorbelegen:

```bash
export NONINTERACTIVE=true
export _INSTALL_SEL=1              # 1=Projekt, 2=Manager, 3=Beides
export PROJECTNAME=myapp
export APPUSER=myuser
export MODESEL=2                   # 1=DEV, 2=PROD
export SOURCE_TYPE=github          # github | zip | new
export GITHUB_REPO_URL=git@github.com:user/repo.git
export DBTYPE_SEL=1                # 1=PostgreSQL, 2=MySQL, 3=SQLite
export DBMODE=2                    # 1=lokal, 2=remote
export DBHOST=localhost DBPORT=5432
export DBNAME=myapp DBUSER=myapp_user DBPASS=geheim
sudo ./Installv2.sh
```

<details>
<summary><b>Alle Variablen</b></summary>

| Variable | Bedeutung | Standard |
|---|---|---|
| `NONINTERACTIVE` | `true` = alle Prompts deaktivieren | `false` |
| `_INSTALL_SEL` | 1=Projekt, 2=Manager, 3=Beides | `3` |
| `PROJECTNAME` / `APPUSER` | Projektname / Linux-User | — |
| `MODESEL` | 1=DEV, 2=PROD | `1` |
| `SOURCE_TYPE` | `github` / `zip` / `new` | `new` |
| `GITHUB_REPO_URL` | Repo-URL (bei `github`) | — |
| `UPLOAD_ZIP_PATH` | Pfad zur ZIP (bei `zip`) | — |
| `GUNICORN_PORT` | Port (leer = automatisch ab 8000) | auto |
| `GUNICORN_WORKERS` | Worker-Anzahl | 2×CPU+1 |
| `SERVER_TYPE` | `wsgi` oder `asgi` (async/WebSockets/Channels) | `wsgi` |
| `ALLOWED_HOSTS` | kommasepariert | auto |
| `DBTYPE_SEL` / `DBMODE` | DB-Typ / lokal-remote | — |
| `DBNAME` / `DBUSER` / `DBPASS` / `DBHOST` / `DBPORT` | DB-Zugang | — |
| `LANGUAGE_CODE` / `TIME_ZONE` | Lokalisierung | `de-de` / `Europe/Berlin` |
| `EMAIL_HOST` u. a. | SMTP-Konfiguration | leer |
| `_BACKUP_TIME` | Backup-Cron (HH:MM) | `02:00` |
| `UPGRADE` | `j` = Systempakete updaten | `n` |
| `INSTALL_FAIL2BAN` | `j` = fail2ban installieren | `n` |
| `INSTALL_CERTBOT` | `j` = Let's-Encrypt-Zertifikat via certbot anfordern | `n` |
| `CERTBOT_DOMAIN` / `CERTBOT_EMAIL` | Domain / Kontakt-E-Mail für certbot | auto / leer |

</details>

---

## 💾 Backups & Updates

**Backup** (`/usr/local/bin/<projekt>_backup.sh`, täglicher Cron, Standard 02:00 Uhr):
DB-Dump (pg_dump `-Fc` / mysqldump / SQLite-Kopie) + `.env`-Sicherung + Projektarchiv ohne `.venv`/`__pycache__`/Logs.
Maximal **5 Backups** pro Projekt, ältere werden rotiert. Ablage in `/var/backups/<projekt>/` (700).

**Update** (`/usr/local/bin/<projekt>_update.sh`, per Klick im Manager):
Backup → `git pull` (als App-User mit Deploy-Key) → `pip install -r requirements.txt` → `migrate` → `collectstatic` → **eigene Update-Befehle** → Service-Restart → nginx-Reload.

Die **eigenen Update-Befehle** sind pro Projekt im Web-Manager (Projektdetail → *Update & Backup* → *Eigene Update-Befehle*) frei konfigurierbar: beliebige `manage.py`-Kommandos (z. B. `load_glossary`, `loaddata seed.json`, `clearsessions`), die nach `collectstatic` und vor dem abschließenden Neustart als App-User in der `.venv` ausgeführt werden. Einzeln aktivierbar/deaktivierbar und jederzeit ohne Neuinstallation anpassbar. Eingaben werden validiert (nur `manage.py`-Unterbefehle, keine Shell-Metazeichen).

---

## 🔁 Checkpoint / Resume

Jeder Installationsschritt setzt einen Checkpoint (`input_saved`, `pkgs_installed`, `db_setup`, `nginx_done`, …).
Bricht eine Installation ab — Stromausfall, SSH-Abbruch, Fehler —, setzt der nächste Aufruf
**genau dort fort**, statt von vorn zu beginnen.

---

## 🧪 Kompatibilität

- **Debian 12+**, **Ubuntu 22.04+** (root erforderlich)
- **Proxmox LXC** — empfohlen `nesting=1`, `keyctl=1`; ufw-Schritte werden übersprungen, wo nicht verfügbar
- VMs und Bare-Metal
- Vorab-Checks: Speicherplatz, RAM, DNS, HTTPS-Erreichbarkeit, Systemzeit, beschreibbares `/tmp`

---

## 🚫 Bewusste Nicht-Ziele

- **Kein HTTPS im Skript** — TLS-Terminierung übernimmt Zoraxy bzw. der vorgelagerte Reverse Proxy
- **Kein Auto-Scaling** — ein Gunicorn-Service pro Projekt
- **Kein Docker** — klassisches Deployment direkt auf dem Host/LXC

---

## 🧑‍💻 Entwicklung & Tests

Der Manager bringt eine Test-Suite mit (Sicherheits-Regression, Benachrichtigungen, Health-Historie, Validatoren):

```bash
cd manager
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DEBUG=True SECRET_KEY=dev python manage.py test control.tests
```

---

## 📄 Lizenz & Beiträge

Issues und Pull Requests sind willkommen. Bei Sicherheitsfunden bitte ein
[GitHub Security Advisory](https://github.com/Manjo80/DjangoMultiDeploy/security/advisories) erstellen
statt eines öffentlichen Issues.
