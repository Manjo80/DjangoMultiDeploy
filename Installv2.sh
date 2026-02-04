#!/bin/bash
set -euo pipefail

# ===================================================================
# Django Installer - Secure & Flexible (LXC/Container ready)
# ===================================================================

# -------------------------------------------------------------------
# OS + systemd Check (Debian / Ubuntu)
# -------------------------------------------------------------------
if [ -r /etc/os-release ]; then
  . /etc/os-release
else
  echo "❌ FEHLER: /etc/os-release nicht gefunden."
  exit 1
fi

case "${ID:-}" in
  debian|ubuntu) ;;
  *) echo "❌ FEHLER: Nur Debian/Ubuntu unterstützt. Gefunden: ${ID:-unknown}"; exit 1 ;;
esac

if ! command -v systemctl >/dev/null 2>&1; then
  echo "❌ FEHLER: systemd/systemctl nicht gefunden."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║         Django Installer (${PRETTY_NAME:-$ID})                ║"
echo "╚═══════════════════════════════════════════════════════════════╝"

# -------------------------------------------------------------------
# Projektname -> /srv/<name> (mit Validierung!)
# -------------------------------------------------------------------
read -p "Projektname (Ordner unter /srv, z.B. gpsmgr): " PROJECTNAME
[ -z "${PROJECTNAME:-}" ] && echo "❌ FEHLER: Projektname leer." && exit 1

# Validierung: nur alphanumerisch, _, - und 3-50 Zeichen
if [[ ! "$PROJECTNAME" =~ ^[a-zA-Z0-9_-]{3,50}$ ]]; then
  echo "❌ FEHLER: Ungültiger Projektname!"
  echo "   Erlaubt: a-z, A-Z, 0-9, _, - (3-50 Zeichen)"
  exit 1
fi

APPDIR="/srv/$PROJECTNAME"

# -------------------------------------------------------------------
# Existenz-Checks (vor Installation!)
# -------------------------------------------------------------------
if [[ -d "$APPDIR" ]]; then
  echo "❌ FEHLER: $APPDIR existiert bereits!"
  exit 1
fi

if [[ -f "/etc/systemd/system/${PROJECTNAME}.service" ]]; then
  echo "❌ FEHLER: Service ${PROJECTNAME} existiert bereits!"
  exit 1
fi

if [[ -f "/etc/nginx/sites-available/${PROJECTNAME}" ]]; then
  echo "❌ FEHLER: Nginx-Site ${PROJECTNAME} existiert bereits!"
  exit 1
fi

# -------------------------------------------------------------------
# GitHub Repository Option
# -------------------------------------------------------------------
echo
echo "GitHub Repository:"
echo "  • Öffentliches Repo: https://github.com/user/repo.git"
echo "  • Privates Repo:     git@github.com:user/repo.git (SSH-Key wird erstellt)"
echo "  • Leer lassen für neues Django-Projekt"
read -p "GitHub URL (leer für neues Projekt): " GITHUB_REPO_URL
USE_GITHUB="${GITHUB_REPO_URL:+true}"

# -------------------------------------------------------------------
# Local IP (Default Hosts)
# -------------------------------------------------------------------
LOCAL_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {print $7; exit}')"
[ -z "${LOCAL_IP:-}" ] && LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "${LOCAL_IP:-}" ] && LOCAL_IP="127.0.0.1"

HOSTNAME_FQDN="$(hostname -f 2>/dev/null || echo "$LOCAL_IP")"

# -------------------------------------------------------------------
# Mode: DEV vs PROD
# -------------------------------------------------------------------
echo
echo "Modus:"
echo "  1) DEV  (DEBUG=True, kein SSL-Proxy nötig)"
echo "  2) PROD (DEBUG=False, Reverse-Proxy mit SSL)"
read -p "Auswahl (1/2) [1]: " MODESEL
MODESEL="${MODESEL:-1}"
[[ "$MODESEL" != "1" && "$MODESEL" != "2" ]] && echo "❌ FEHLER: Bitte 1 oder 2." && exit 1
if [ "$MODESEL" = "1" ]; then MODE="dev"; DEBUG_VALUE="True"; else MODE="prod"; DEBUG_VALUE="False"; fi

# -------------------------------------------------------------------
# Hosts
# -------------------------------------------------------------------
DEFAULT_ALLOWED_HOSTS="${LOCAL_IP},127.0.0.1,localhost,${HOSTNAME_FQDN}"
[ "$MODE" = "prod" ] && echo "PROD: DNS-Namen eintragen (z.B. app.intern.lan)"
read -p "ALLOWED_HOSTS (Komma-separiert) [${DEFAULT_ALLOWED_HOSTS}]: " ALLOWED_HOSTS
ALLOWED_HOSTS="${ALLOWED_HOSTS:-$DEFAULT_ALLOWED_HOSTS}"
NGINX_SERVER_NAMES="$(echo "$ALLOWED_HOSTS" | tr ',' ' ' | xargs)"
[ -z "${NGINX_SERVER_NAMES:-}" ] && NGINX_SERVER_NAMES="_"

# -------------------------------------------------------------------
# CSRF_TRUSTED_ORIGINS automatisch bauen (nur PROD)
# - überspringt localhost + IPs, weil meist nicht per https genutzt
# - nimmt nur "echte" Hostnames -> https://hostname
# -------------------------------------------------------------------
CSRF_TRUSTED_ORIGINS_VALUE=""
if [ "$MODE" = "prod" ]; then
  CSRF_TRUSTED_ORIGINS_VALUE="$(echo "$ALLOWED_HOSTS" | tr ',' '\n' | awk '
    function is_ip(x) { return (x ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) }
    NF {
      gsub(/^[ \t]+|[ \t]+$/, "", $0)
      if ($0 == "" || $0 == "localhost" || is_ip($0)) next
      print "https://" $0
    }' | awk '!seen[$0]++' | paste -sd, -)"
fi

# -------------------------------------------------------------------
# Datenbank-Typ auswählen
# -------------------------------------------------------------------
echo
echo "Datenbank-Typ:"
echo "  1) PostgreSQL (lokal oder remote)"
echo "  2) MySQL/MariaDB (lokal oder remote)"
echo "  3) SQLite (nur für DEV, kein Remote möglich)"
read -p "Auswahl (1/2/3): " DBTYPE_SEL
case "$DBTYPE_SEL" in
  1) DBTYPE="postgresql"; DB_PACKAGE_LOCAL="postgresql postgresql-contrib"; DB_PACKAGE_CLIENT="postgresql-client";;
  2) DBTYPE="mysql"; DB_PACKAGE_LOCAL="mariadb-server mariadb-client"; DB_PACKAGE_CLIENT="mariadb-client";;
  3) DBTYPE="sqlite"; DB_PACKAGE_LOCAL=""; DB_PACKAGE_CLIENT="";;
  *) echo "❌ FEHLER: Ungültige Auswahl"; exit 1;;
esac

# SQLite nur im DEV-Modus erlauben
if [ "$DBTYPE" = "sqlite" ]; then
  if [ "$MODE" = "prod" ]; then
    echo "⚠️  WARNUNG: SQLite wird nicht für PROD empfohlen (keine Parallelität, Backup-Komplexität)."
    read -p "Trotzdem fortfahren? (j/N): " CONFIRM
    [[ ! "$CONFIRM" =~ ^[Jj]$ ]] && exit 1
  fi
  DB_PATH="$APPDIR/db.sqlite3"
  DB_ENGINE="django.db.backends.sqlite3"
  DBNAME=""; DBUSER=""; DBPASS=""; DBHOST=""; DBPORT=""
else
  # -------------------------------------------------------------------
  # DB-Modus: Lokal vs Remote
  # -------------------------------------------------------------------
  echo
  echo "DB-Betriebsmodus:"
  echo "  1) Lokal installieren"
  echo "  2) Remote-Verbindung"
  read -p "Auswahl (1/2): " DBMODE
  [[ "$DBMODE" != "1" && "$DBMODE" != "2" ]] && echo "❌ FEHLER: Bitte 1 oder 2." && exit 1

  # -------------------------------------------------------------------
  # DB-Zugangsdaten
  # -------------------------------------------------------------------
  DBNAME_DEFAULT="${PROJECTNAME//-/_}"
  DBUSER_DEFAULT="${PROJECTNAME//-/_}_user"
  
  read -p "DB Name [${DBNAME_DEFAULT}]: " TMP_DBNAME
  DBNAME="${TMP_DBNAME:-$DBNAME_DEFAULT}"
  DBNAME="${DBNAME//-/_}"

  read -p "DB User [${DBUSER_DEFAULT}]: " TMP_DBUSER
  DBUSER="${TMP_DBUSER:-$DBUSER_DEFAULT}"
  DBUSER="${DBUSER//-/_}"

  read -p "DB Host [localhost]: " DBHOST
  DBHOST="${DBHOST:-localhost}"

  read -p "DB Port [${DBTYPE}]: " DBPORT
  if [ "$DBTYPE" = "postgresql" ]; then
    DBPORT="${DBPORT:-5432}"
  else
    DBPORT="${DBPORT:-3306}"
  fi

  read -s -p "DB Passwort für ${DBUSER}: " DBPASS; echo
  [ -z "$DBPASS" ] && echo "❌ FEHLER: Passwort erforderlich." && exit 1

  # Django ENGINE setzen
  if [ "$DBTYPE" = "postgresql" ]; then
    DB_ENGINE="django.db.backends.postgresql"
  else
    DB_ENGINE="django.db.backends.mysql"
  fi
fi

# -------------------------------------------------------------------
# Linux User (ohne sudo!)
# -------------------------------------------------------------------
read -p "Linux-User für App (wird erstellt, z.B. gps): " APPUSER
[ -z "${APPUSER:-}" ] && echo "❌ FEHLER: APPUSER leer." && exit 1

# Validierung APPUSER
if [[ ! "$APPUSER" =~ ^[a-z][a-z0-9_-]{1,30}$ ]]; then
  echo "❌ FEHLER: Ungültiger Benutzername!"
  echo "   Muss mit Buchstabe beginnen, nur a-z, 0-9, _, -"
  exit 1
fi

# -------------------------------------------------------------------
# Secret
# -------------------------------------------------------------------
read -s -p "Django SECRET_KEY (leer = auto): " DJKEY; echo
[ -z "${DJKEY:-}" ] && DJKEY="$(openssl rand -hex 32)"

# -------------------------------------------------------------------
# SSH-Key für App-User erstellen (für GitHub + SSH-Login)
# -------------------------------------------------------------------
echo
echo "🔐 SSH-Key Konfiguration"
echo "   Dieser Key wird für BEIDES verwendet:"
echo "   • SSH-Login (WinSCP/PuTTY)"
echo "   • GitHub-Zugriff (für private Repos)"
echo
read -p "SSH-Key Passphrase (leer für kein Passwort): " SSH_KEY_PASSPHRASE

SSH_KEY_PATH="/home/${APPUSER}/.ssh/id_ed25519"

echo "🔑 Erstelle SSH-Key für Benutzer $APPUSER..."
sudo -u "$APPUSER" mkdir -p "/home/$APPUSER/.ssh"

# SSH-Key erstellen
if [ -z "$SSH_KEY_PASSPHRASE" ]; then
  # Ohne Passphrase
  sudo -u "$APPUSER" ssh-keygen -t ed25519 -C "${APPUSER}@$(hostname -f || echo 'server')" \
    -f "$SSH_KEY_PATH" -N "" -q
else
  # Mit Passphrase (interaktiv)
  sudo -u "$APPUSER" ssh-keygen -t ed25519 -C "${APPUSER}@$(hostname -f || echo 'server')" \
    -f "$SSH_KEY_PATH" -N "$SSH_KEY_PASSPHRASE" -q
fi

# Berechtigungen setzen
sudo -u "$APPUSER" chmod 700 "/home/$APPUSER/.ssh"
sudo -u "$APPUSER" chmod 600 "${SSH_KEY_PATH}"
sudo -u "$APPUSER" chmod 644 "${SSH_KEY_PATH}.pub"
chown -R "$APPUSER:$APPUSER" "/home/$APPUSER/.ssh"

echo "✅ SSH-Key erstellt: $SSH_KEY_PATH"
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔐 ÖFFENTLICHER KEY (für GitHub und SSH authorized_keys):"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
sudo -u "$APPUSER" cat "${SSH_KEY_PATH}.pub"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

# GitHub Setup (wenn Repo angegeben)
if [[ "$USE_GITHUB" == "true" ]]; then
  echo "📦 GitHub Repository erkannt: $GITHUB_REPO_URL"
  echo
  echo "⚠️  WICHTIG FÜR PRIVATE REPOS:"
  echo "   1. Kopiere den öffentlichen Key oben"
  echo "   2. Gehe zu: GitHub → Settings → SSH and GPG keys → New SSH key"
  echo "   3. Titel: '${PROJECTNAME} - $(hostname -f || echo 'server')'"
  echo "   4. Key einfügen und speichern"
  echo
  read -p "Fortfahren nachdem der Key zu GitHub hinzugefügt wurde? (J/n): " CONFIRM
  [[ ! "${CONFIRM:-J}" =~ ^[Jj]$ ]] && echo "❌ Abbruch." && exit 1
  
  # known_hosts für github.com
  echo "🔗 Konfiguriere SSH known_hosts für github.com..."
  sudo -u "$APPUSER" ssh-keyscan -H github.com >> "/home/$APPUSER/.ssh/known_hosts" 2>/dev/null || true
  sudo -u "$APPUSER" ssh-keyscan -H github.com >> "/home/$APPUSER/.ssh/known_hosts" 2>/dev/null || true
  chown "$APPUSER:$APPUSER" "/home/$APPUSER/.ssh/known_hosts"
fi

# SSH-Zugriff für App-User ermöglichen
echo "🔓 Konfiguriere SSH-Zugriff für $APPUSER..."
echo "   • Anmeldung per SSH-Key erlaubt"
echo "   • Anmeldung per Passwort: deaktiviert (sicher)"
echo

# Prüfen, ob SSHD PasswordAuthentication deaktiviert ist
if grep -q "^PasswordAuthentication no" /etc/ssh/sshd_config 2>/dev/null; then
  echo "✅ SSH-Passwort-Login ist bereits deaktiviert (sicher)"
elif grep -q "^PasswordAuthentication yes" /etc/ssh/sshd_config 2>/dev/null; then
  echo "⚠️  WARNUNG: SSH-Passwort-Login ist aktiviert!"
  echo "    Empfehlung: In /etc/ssh/sshd_config 'PasswordAuthentication no' setzen"
  echo "    Danach: sudo systemctl restart sshd"
fi

# -------------------------------------------------------------------
# System-Pakete (optional upgrade)
# -------------------------------------------------------------------
apt update

read -p "System-Pakete updaten? (empfohlen) [J/n]: " UPGRADE
[[ "${UPGRADE:-J}" =~ ^[Jj]$ ]] && apt upgrade -y

# Basis-Pakete
echo "📦 Installiere Basis-Pakete..."
apt install -y curl git nano ca-certificates openssl net-tools nginx \
               python3 python3-venv python3-pip build-essential iproute2

# Bildverarbeitung (Pillow) - Empfohlen für fast alle Projekte
echo "🖼️  Installiere Pillow für Bildunterstützung (ImageField)..."
apt install -y libjpeg-dev zlib1g-dev libpng-dev libwebp-dev

# DB-spezifische Pakete
if [ "$DBTYPE" = "postgresql" ]; then
  apt install -y libpq-dev
elif [ "$DBTYPE" = "mysql" ]; then
  apt install -y libmysqlclient-dev python3-dev default-libmysqlclient-dev
fi

# -------------------------------------------------------------------
# App-User erstellen (OHNE sudo!)
# -------------------------------------------------------------------
if ! id "$APPUSER" &>/dev/null; then
  echo "👤 Erstelle Benutzer: $APPUSER"
  adduser --disabled-password --gecos "" "$APPUSER" || adduser --disabled-password "$APPUSER"
fi

# ⚠️ KEIN sudo für APPUSER! Nur spezifische Befehle erlauben (siehe unten)

# -------------------------------------------------------------------
# PostgreSQL / MySQL Installation (lokal)
# -------------------------------------------------------------------
cd /tmp

if [ "${DBTYPE}" != "sqlite" ] && [ "${DBMODE:-}" = "1" ]; then
  echo "🗄️  Installiere lokale ${DBTYPE^^} Datenbank..."
  
  if [ "$DBTYPE" = "postgresql" ]; then
    apt install -y $DB_PACKAGE_LOCAL
    systemctl enable --now postgresql
    
    echo "🔐 Erstelle PostgreSQL Benutzer und Datenbank..."
    sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DBNAME'" | grep -q 1 || \
      sudo -u postgres psql -c "CREATE DATABASE \"$DBNAME\";"
    
    sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DBUSER'" | grep -q 1 || \
      sudo -u postgres psql -c "CREATE USER \"$DBUSER\" WITH ENCRYPTED PASSWORD '$DBPASS';"
    
    sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE \"$DBNAME\" TO \"$DBUSER\";"
    
  elif [ "$DBTYPE" = "mysql" ]; then
    apt install -y $DB_PACKAGE_LOCAL
    systemctl enable --now mariadb
    
    echo "🔐 Erstelle MySQL/MariaDB Benutzer und Datenbank..."
    mysql -u root <<SQL
CREATE DATABASE IF NOT EXISTS \`$DBNAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$DBUSER'@'localhost' IDENTIFIED BY '$DBPASS';
GRANT ALL PRIVILEGES ON \`$DBNAME\`.* TO '$DBUSER'@'localhost';
FLUSH PRIVILEGES;
SQL
  fi
elif [ "${DBTYPE}" != "sqlite" ] && [ "${DBMODE:-}" = "2" ]; then
  # Remote-DB: Nur Client installieren
  echo "🌐 Installiere ${DBTYPE^^} Client für Remote-Verbindung..."
  apt install -y $DB_PACKAGE_CLIENT
fi

# -------------------------------------------------------------------
# Projektverzeichnis
# -------------------------------------------------------------------
echo "📁 Erstelle Projektverzeichnis: $APPDIR"
mkdir -p "$APPDIR"
chown "$APPUSER:$APPUSER" "$APPDIR"

# -------------------------------------------------------------------
# Django Setup (Neu oder GitHub)
# -------------------------------------------------------------------
if [[ "$USE_GITHUB" == "true" ]]; then
  echo "📥 Klonen GitHub Repository: $GITHUB_REPO_URL"
  
  # Git clone mit SSH-Key
  sudo -u "$APPUSER" GIT_SSH_COMMAND="ssh -i ${SSH_KEY_PATH} -o IdentitiesOnly=yes" \
    git clone "$GITHUB_REPO_URL" "$APPDIR"
  
  echo "✅ Repository geklont nach $APPDIR"
  
  # .env erstellen (falls nicht im Repo vorhanden)
  if [ ! -f "$APPDIR/.env" ]; then
    echo "🔐 Erstelle .env Datei (nicht im Repository gefunden)..."
    cat > "$APPDIR/.env" <<EOF
MODE=$MODE
DEBUG=$DEBUG_VALUE
SECRET_KEY=$DJKEY
DB_ENGINE=$DB_ENGINE
DB_NAME=$DBNAME
DB_USER=$DBUSER
DB_PASS=$DBPASS
DB_HOST=$DBHOST
DB_PORT=$DBPORT
ALLOWED_HOSTS=$ALLOWED_HOSTS
CSRF_TRUSTED_ORIGINS=$CSRF_TRUSTED_ORIGINS_VALUE
EOF
    chown "$APPUSER:$APPUSER" "$APPDIR/.env"
    chmod 600 "$APPDIR/.env"
    echo "✅ .env erstellt"
  else
    echo "⚠️  .env bereits im Repository vorhanden (wird NICHT überschrieben)"
    echo "   Stelle sicher, dass die DB-Zugangsdaten korrekt sind!"
  fi
  
  # Virtual Environment erstellen
  echo "🐍 Erstelle Python Virtual Environment..."
  sudo -u "$APPUSER" bash <<EOF
cd "$APPDIR"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install django gunicorn python-dotenv pillow

# DB-spezifische Pakete
if [ "$DBTYPE" = "postgresql" ]; then
  pip install "psycopg[binary]"
elif [ "$DBTYPE" = "mysql" ]; then
  pip install mysqlclient
fi

# Requirements installieren (falls vorhanden)
if [ -f "$APPDIR/requirements.txt" ]; then
  echo "📦 Installiere requirements.txt..."
  pip install -r "$APPDIR/requirements.txt"
fi
EOF

else
  # NEUES PROJEKT erstellen (wie bisher)
  echo "🚀 Django Setup (neues Projekt)..."
  sudo -u "$APPUSER" bash <<EOF
set -e
cd "$APPDIR"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install django gunicorn python-dotenv pillow

# DB-spezifische Pakete
if [ "$DBTYPE" = "postgresql" ]; then
  pip install "psycopg[binary]"
elif [ "$DBTYPE" = "mysql" ]; then
  pip install mysqlclient
fi

# Django Projekt erstellen
django-admin startproject core .
python manage.py startapp app
EOF
fi

# -------------------------------------------------------------------
# .env Datei (nur bei neuem Projekt oder falls nicht vorhanden)
# -------------------------------------------------------------------
if [[ "$USE_GITHUB" != "true" ]] || [ ! -f "$APPDIR/.env" ]; then
  echo "🔐 Erstelle .env Datei..."
  cat > "$APPDIR/.env" <<EOF
MODE=$MODE
DEBUG=$DEBUG_VALUE
SECRET_KEY=$DJKEY
DB_ENGINE=$DB_ENGINE
DB_NAME=$DBNAME
DB_USER=$DBUSER
DB_PASS=$DBPASS
DB_HOST=$DBHOST
DB_PORT=$DBPORT
ALLOWED_HOSTS=$ALLOWED_HOSTS
CSRF_TRUSTED_ORIGINS=$CSRF_TRUSTED_ORIGINS_VALUE
EOF

  chown "$APPUSER:$APPUSER" "$APPDIR/.env"
  chmod 600 "$APPDIR/.env"  # Nur App-User darf lesen/schreiben!
fi

# -------------------------------------------------------------------
# .gitignore (nur bei neuem Projekt)
# -------------------------------------------------------------------
if [[ "$USE_GITHUB" != "true" ]]; then
  cat > "$APPDIR/.gitignore" <<EOF
.env
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
.venv/
venv/
ENV/
env/
staticfiles/
media/
*.sqlite3
db.sqlite3
EOF

  chown "$APPUSER:$APPUSER" "$APPDIR/.gitignore"
fi

# -------------------------------------------------------------------
# settings.py (nur bei neuem Projekt)
# -------------------------------------------------------------------
if [[ "$USE_GITHUB" != "true" ]]; then
  echo "⚙️  Konfiguriere Django settings.py..."
  cat > "$APPDIR/core/settings.py" <<'EOF'
from pathlib import Path
from dotenv import load_dotenv
import os
import time

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

MODE = os.getenv("MODE", "dev").lower()
SECRET_KEY = os.getenv("SECRET_KEY", "unsafe-default-key-change-in-production")
DEBUG = os.getenv("DEBUG", "False") == "True"

def env_list(name: str):
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "app",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {
        "context_processors": [
            "django.template.context_processors.debug",
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
        ],
    },
}]

WSGI_APPLICATION = "core.wsgi.application"

# Universelle DB-Konfiguration (PostgreSQL/MySQL/SQLite)
DATABASES = {
    "default": {
        "ENGINE": os.getenv("DB_ENGINE", "django.db.backends.sqlite3"),
        "NAME": os.getenv("DB_NAME", BASE_DIR / "db.sqlite3"),
        "USER": os.getenv("DB_USER", ""),
        "PASSWORD": os.getenv("DB_PASS", ""),
        "HOST": os.getenv("DB_HOST", ""),
        "PORT": os.getenv("DB_PORT", ""),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "de-de"
TIME_ZONE = time.tzname[0] if hasattr(time, 'tzname') and time.tzname[0] else "Europe/Luxembourg"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# PROD-spezifische Sicherheitseinstellungen
if MODE == "prod":
    USE_X_FORWARDED_HOST = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
EOF

  chown "$APPUSER:$APPUSER" "$APPDIR/core/settings.py"
fi

# -------------------------------------------------------------------
# Migration + Static Files
# -------------------------------------------------------------------
echo "📊 Führe Migrationen aus..."
sudo -u "$APPUSER" bash -c "cd $APPDIR && .venv/bin/python manage.py migrate"

echo "📦 Sammle statische Dateien..."
sudo -u "$APPUSER" bash -c "cd $APPDIR && .venv/bin/python manage.py collectstatic --noinput"

# -------------------------------------------------------------------
# systemd Service
# -------------------------------------------------------------------
echo "🔧 Erstelle systemd Service..."
cat > /etc/systemd/system/${PROJECTNAME}.service <<EOF
[Unit]
Description=$PROJECTNAME Django Application
After=network.target

[Service]
User=$APPUSER
Group=$APPUSER
WorkingDirectory=$APPDIR
EnvironmentFile=$APPDIR/.env
ExecStart=$APPDIR/.venv/bin/gunicorn core.wsgi:application --bind 127.0.0.1:8000 --workers 3 --timeout 120
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$PROJECTNAME"

# -------------------------------------------------------------------
# nginx Konfiguration (mit static/media)
# -------------------------------------------------------------------
echo "🌐 Konfiguriere Nginx..."
cat > /etc/nginx/sites-available/$PROJECTNAME <<EOF
server {
    listen 80;
    server_name $NGINX_SERVER_NAMES;
    client_max_body_size 50M;

    # Security Headers
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;

    # Static Files (direkt via Nginx)
    location /static/ {
        alias $APPDIR/staticfiles/;
        expires 1y;
        add_header Cache-Control "public, immutable";
        access_log off;
    }

    # Media Files
    location /media/ {
        alias $APPDIR/media/;
        expires 30d;
        access_log off;
    }

    # Django App (via Gunicorn)
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
EOF

ln -sf /etc/nginx/sites-available/$PROJECTNAME /etc/nginx/sites-enabled/$PROJECTNAME
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

if ! nginx -t; then
  echo "❌ FEHLER: Nginx Konfiguration ungültig!"
  exit 1
fi

systemctl restart nginx

# -------------------------------------------------------------------
# Sudoers: App-User darf NUR Service-Befehle ohne Passwort
# -------------------------------------------------------------------
echo "🔐 Konfiguriere sudoers für $APPUSER..."
cat > /etc/sudoers.d/${PROJECTNAME}-service <<EOF
$APPUSER ALL=NOPASSWD: /bin/systemctl restart $PROJECTNAME, /bin/systemctl status $PROJECTNAME, /bin/systemctl reload $PROJECTNAME, /bin/journalctl -u $PROJECTNAME*
EOF
chmod 440 /etc/sudoers.d/${PROJECTNAME}-service

# -------------------------------------------------------------------
# UPDATE-Skript (in /usr/local/bin für besseren PATH-Zugriff)
# -------------------------------------------------------------------
echo "🔄 Erstelle Update-Skript..."
cat > /usr/local/bin/${PROJECTNAME}_update.sh <<'EOF'
#!/bin/bash
set -euo pipefail

APPDIR="${APPDIR}"
SERVICE="${PROJECTNAME}"

echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║                  UPDATE START ($SERVICE)                      ║"
echo "╚═══════════════════════════════════════════════════════════════╝"

cd "$APPDIR"

# Git Pull (mit SSH-Key)
if [ -d "$APPDIR/.git" ]; then
  echo "📥 Git Pull..."
  GIT_SSH_COMMAND="ssh -i /home/${APPUSER}/.ssh/id_ed25519 -o IdentitiesOnly=yes" git pull
else
  echo "⚠️  Kein Git-Repository gefunden (überspringe git pull)"
fi

# Requirements installieren
if [ -f "$APPDIR/requirements.txt" ]; then
  echo "📦 Installiere Requirements..."
  . "$APPDIR/.venv/bin/activate"
  pip install -r "$APPDIR/requirements.txt"
else
  echo "⚠️  requirements.txt nicht gefunden (überspringe pip install)"
fi

# Migrationen prüfen
echo "🔍 Prüfe auf fehlende Migrationen..."
"$APPDIR/.venv/bin/python" "$APPDIR/manage.py" makemigrations --check --dry-run || {
  echo "⚠️  Neue Migrationen gefunden. Führe makemigrations aus..."
  "$APPDIR/.venv/bin/python" "$APPDIR/manage.py" makemigrations
}

# Migrationen ausführen
echo "📊 Führe Migrationen aus..."
"$APPDIR/.venv/bin/python" "$APPDIR/manage.py" migrate

# Statische Dateien sammeln
echo "📦 Sammle statische Dateien..."
"$APPDIR/.venv/bin/python" "$APPDIR/manage.py" collectstatic --noinput

# Service neustarten
echo "🔄 Neustart Service (sudo, nopasswd)..."
sudo systemctl restart "$SERVICE"

echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║                     UPDATE DONE ✅                            ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
EOF

chmod 755 /usr/local/bin/${PROJECTNAME}_update.sh

# -------------------------------------------------------------------
# LOGIN-Hinweis (MOTD)
# -------------------------------------------------------------------
cat > /etc/profile.d/${PROJECTNAME}_motd.sh <<EOF
# Zeige nur bei interaktiven Shells
case "\$-" in
  *i*) ;;
  *) return ;;
esac

# Zeige nur beim ersten Login pro Session
if [ -n "\${MOTD_${PROJECTNAME}_SHOWN:-}" ]; then
  return
fi
export MOTD_${PROJECTNAME}_SHOWN=1

echo
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║              $PROJECTNAME - Quick Reference                   ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo "📁 Projektverzeichnis: $APPDIR"
echo "⚙️  Service:           $PROJECTNAME"
echo "🌐 Modus:              $MODE (DEBUG=$DEBUG_VALUE)"
EOF

if [[ "$USE_GITHUB" == "true" ]]; then
  echo "📦 GitHub Repo:       $GITHUB_REPO_URL"
fi

cat >> /etc/profile.d/${PROJECTNAME}_motd.sh <<EOF
echo
echo "🔐 SSH-Zugriff für $APPUSER:"
echo "   IP:          ssh $APPUSER@${LOCAL_IP}"
echo "   Hostname:    ssh $APPUSER@${HOSTNAME_FQDN}"
echo "   Private Key: ${SSH_KEY_PATH}"
echo
echo "📦 Update (als $APPUSER, kein sudo nötig):"
echo "   ${PROJECTNAME}_update.sh"
echo
echo "📊 Status/Logs:"
echo "   systemctl status $PROJECTNAME"
echo "   journalctl -u $PROJECTNAME -f"
echo
echo "👑 Superuser erstellen:"
echo "   sudo -u $APPUSER bash -c 'cd $APPDIR && .venv/bin/python manage.py createsuperuser'"
echo
echo "💾 Backup-Tipp:"
echo "   DB: pg_dump/mysqldump -u $DBUSER -h $DBHOST $DBNAME > backup.sql"
echo "   .env: cp $APPDIR/.env /sicherer/ort/"
echo
echo "🔐 CSRF_TRUSTED_ORIGINS (PROD):"
echo "   $CSRF_TRUSTED_ORIGINS_VALUE"
echo "═══════════════════════════════════════════════════════════════"
echo
EOF
chmod 644 /etc/profile.d/${PROJECTNAME}_motd.sh

# -------------------------------------------------------------------
# Firewall-Hinweis (falls aktiv)
# -------------------------------------------------------------------
if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
  echo "⚠️  Firewall aktiviert! Ports ggf. freigeben:"
  echo "   sudo ufw allow 22/tcp    # SSH"
  echo "   sudo ufw allow 80/tcp    # HTTP"
  echo "   sudo ufw allow 443/tcp   # HTTPS"
fi

# -------------------------------------------------------------------
# Done
# -------------------------------------------------------------------
echo
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║                      INSTALLATION FERTIG ✅                   ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo "📁 Projektverzeichnis: $APPDIR"
echo "👤 App-Benutzer:       $APPUSER (kein sudo!)"
echo "🌐 OS:                 ${PRETTY_NAME:-$ID}"
echo "⚙️  Modus:             $MODE (DEBUG=$DEBUG_VALUE)"
echo "🗄️  Datenbank:         ${DBTYPE^^}"
echo "   DB-Engine:          $DB_ENGINE"
echo "   DB-Name:            $DBNAME"
echo "   DB-Host:            $DBHOST"
echo "   DB-Port:            $DBPORT"
echo
echo "🖼️  Pillow installiert: Ja (für ImageField/Bildverarbeitung)"
echo
echo "🔐 SSH-ZUGRIFF:"
echo "   Benutzer:     $APPUSER"
echo "   IP-Adresse:   $LOCAL_IP"
echo "   Hostname:     $HOSTNAME_FQDN"
echo "   Private Key:  $SSH_KEY_PATH"
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "⚠️  WICHTIG: Private Key herunterladen für WinSCP/PuTTY!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "📥 SO LÄDTST DU DEN PRIVATE KEY HERUNTER:"
echo
echo "   Methode 1 - WinSCP (empfohlen):"
echo "   1. WinSCP öffnen"
echo "   2. Neue Verbindung erstellen"
echo "   3. Datei-Protokoll: SCP"
echo "   4. Hostname: $LOCAL_IP oder $HOSTNAME_FQDN"
echo "   5. Benutzername: $APPUSER"
echo "   6. Authentifizierung: SSH-Schlüssel"
echo "   7. Privater Schlüssel: ${SSH_KEY_PATH}"
echo "   8. Verbindung testen"
echo
echo "   Methode 2 - PuTTY:"
echo "   1. Private Key herunterladen: scp root@${LOCAL_IP}:${SSH_KEY_PATH} C:\\temp\\id_ed25519"
echo "   2. PuTTYgen öffnen"
echo "   3. 'Load' → Private Key laden (id_ed25519)"
echo "   4. 'Save private key' → id_ed25519.ppk speichern"
echo "   5. PuTTY öffnen → Connection → SSH → Auth → Browse (id_ed25519.ppk)"
echo "   6. Hostname: $LOCAL_IP oder $HOSTNAME_FQDN"
echo "   7. Login: $APPUSER"
echo
echo "   Methode 3 - Direkt vom Server kopieren:"
echo "   sudo cat ${SSH_KEY_PATH}"
echo "   (Den gesamten Key kopieren und lokal speichern)"
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

if [[ "$USE_GITHUB" == "true" ]]; then
  echo "📦 GitHub Repository: $GITHUB_REPO_URL"
  echo
  echo "⚠️  WICHTIG FÜR PRIVATE REPOS:"
  echo "   • Öffentlicher Key wurde oben angezeigt"
  echo "   • Zu GitHub hinzufügen: Settings → SSH and GPG keys → New SSH key"
  echo "   • Titel: '${PROJECTNAME} - ${HOSTNAME_FQDN}'"
  echo
fi

echo "🌐 ALLOWED_HOSTS:      $ALLOWED_HOSTS"
echo "🔐 CSRF_TRUSTED_ORIGINS: $CSRF_TRUSTED_ORIGINS_VALUE"
echo
echo "🔄 Update-Skript (als $APPUSER):"
echo "   ${PROJECTNAME}_update.sh"
echo
echo "👑 Superuser erstellen:"
echo "   sudo -u $APPUSER bash -c 'cd $APPDIR && .venv/bin/python manage.py createsuperuser'"
echo
echo "📊 Service-Befehle:"
echo "   sudo systemctl status $PROJECTNAME"
echo "   sudo systemctl restart $PROJECTNAME"
echo "   journalctl -u $PROJECTNAME -f"
echo
echo "💡 Wichtige Hinweise:"
echo "   • .env ist mit chmod 600 gesichert (nur $APPUSER)"
echo "   • SSL wird extern vom Reverse Proxy terminiert"
echo "   • Statische Dateien werden direkt von Nginx ausgeliefert"
echo "   • Bei Login werden alle Befehle angezeigt"
echo
echo "✅ FERTIG! Viel Erfolg mit deinem Django-Projekt! 🚀"
echo "═══════════════════════════════════════════════════════════════"
