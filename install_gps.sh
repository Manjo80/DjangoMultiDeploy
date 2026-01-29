#!/bin/bash
set -euo pipefail

# -------------------------------------------------------------------
# OS + systemd Check (Debian / Ubuntu)
# -------------------------------------------------------------------
if [ -r /etc/os-release ]; then
  . /etc/os-release
else
  echo "FEHLER: /etc/os-release nicht gefunden."
  exit 1
fi

case "${ID:-}" in
  debian|ubuntu) ;;
  *) echo "FEHLER: Nur Debian/Ubuntu unterstützt. Gefunden: ${ID:-unknown}"; exit 1 ;;
esac

if ! command -v systemctl >/dev/null 2>&1; then
  echo "FEHLER: systemd/systemctl nicht gefunden."
  exit 1
fi

if grep -qaE '(lxc|container)' /proc/1/environ 2>/dev/null || [ -f /run/systemd/container ]; then
  echo "HINWEIS: Container erkannt. In Proxmox LXC ggf. nesting=1,keyctl=1 aktivieren."
fi

export DEBIAN_FRONTEND=noninteractive

echo "=== Django Installer (${PRETTY_NAME:-$ID}) ==="

# -------------------------------------------------------------------
# Projektname
# -------------------------------------------------------------------
read -p "Projektname (Ordner unter /srv, z.B. gpsmgr): " PROJECTNAME
[ -z "${PROJECTNAME:-}" ] && echo "FEHLER: Projektname leer." && exit 1
APPDIR="/srv/$PROJECTNAME"

# -------------------------------------------------------------------
# Local IP (Default Hosts)
# -------------------------------------------------------------------
LOCAL_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {print $7; exit}')"
[ -z "${LOCAL_IP:-}" ] && LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "${LOCAL_IP:-}" ] && LOCAL_IP="127.0.0.1"

# -------------------------------------------------------------------
# Mode
# -------------------------------------------------------------------
echo
echo "Modus:"
echo "  1) DEV  (DEBUG=True)"
echo "  2) PROD (DEBUG=False, Reverse-Proxy geeignet)"
read -p "Auswahl (1/2) [1]: " MODESEL
MODESEL="${MODESEL:-1}"
[[ "$MODESEL" != "1" && "$MODESEL" != "2" ]] && echo "FEHLER." && exit 1

if [ "$MODESEL" = "1" ]; then MODE="dev"; DEBUG_VALUE="True"; else MODE="prod"; DEBUG_VALUE="False"; fi

# -------------------------------------------------------------------
# Hosts
# -------------------------------------------------------------------
DEFAULT_ALLOWED_HOSTS="${LOCAL_IP},127.0.0.1,localhost"
[ "$MODE" = "prod" ] && echo "PROD: DNS-Namen eintragen (z.B. app.intern.lan, app.example.com)"
read -p "ALLOWED_HOSTS [${DEFAULT_ALLOWED_HOSTS}]: " ALLOWED_HOSTS
ALLOWED_HOSTS="${ALLOWED_HOSTS:-$DEFAULT_ALLOWED_HOSTS}"
NGINX_SERVER_NAMES="$(echo "$ALLOWED_HOSTS" | tr ',' ' ' | xargs)"
[ -z "${NGINX_SERVER_NAMES:-}" ] && NGINX_SERVER_NAMES="_"

# -------------------------------------------------------------------
# DB Defaults
# -------------------------------------------------------------------
DBNAME="appdb"
DBUSER="appuser"
DBPORT="5432"
DBHOST="localhost"
DBPASS=""

# -------------------------------------------------------------------
# Linux User
# -------------------------------------------------------------------
read -p "Linux-User für App (wird erstellt): " APPUSER
[ -z "${APPUSER:-}" ] && echo "FEHLER: APPUSER leer." && exit 1

# -------------------------------------------------------------------
# Secret
# -------------------------------------------------------------------
read -s -p "Django SECRET_KEY (leer = auto): " DJKEY; echo
[ -z "${DJKEY:-}" ] && DJKEY="$(openssl rand -hex 32)"

# -------------------------------------------------------------------
# DB Mode
# -------------------------------------------------------------------
echo
echo "DB:"
echo "  1) Lokal (PostgreSQL installieren)"
echo "  2) Remote PostgreSQL"
read -p "Auswahl (1/2): " DBMODE
[[ "$DBMODE" != "1" && "$DBMODE" != "2" ]] && echo "FEHLER." && exit 1

# -------------------------------------------------------------------
# Pakete
# -------------------------------------------------------------------
apt update && apt upgrade -y
apt install -y sudo curl git nano ca-certificates openssl net-tools nginx \
               python3 python3-venv python3-pip build-essential libpq-dev iproute2

# -------------------------------------------------------------------
# User
# -------------------------------------------------------------------
if ! id "$APPUSER" &>/dev/null; then adduser "$APPUSER"; fi
usermod -aG sudo "$APPUSER"
echo "$APPUSER ALL=(ALL) ALL" > /etc/sudoers.d/$APPUSER
chmod 440 /etc/sudoers.d/$APPUSER

# -------------------------------------------------------------------
# PostgreSQL
# -------------------------------------------------------------------
if [ "$DBMODE" = "1" ]; then
  apt install -y postgresql postgresql-contrib
  systemctl enable --now postgresql

  read -s -p "Passwort für lokalen DB-User (${DBUSER}): " DBPASS; echo
  [ -z "$DBPASS" ] && echo "FEHLER: leer." && exit 1

  sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DBNAME'" | grep -q 1 || sudo -u postgres psql -c "CREATE DATABASE $DBNAME;"
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DBUSER'" | grep -q 1 || sudo -u postgres psql -c "CREATE USER $DBUSER WITH ENCRYPTED PASSWORD '$DBPASS';"
  sudo -u postgres psql -c "ALTER DATABASE $DBNAME OWNER TO $DBUSER;"
  sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DBNAME TO $DBUSER;"
else
  read -p "Remote DB Host/IP: " DBHOST
  read -p "Remote DB Port [5432]: " TMPPORT; DBPORT="${TMPPORT:-5432}"
  read -p "Remote Admin-User: " PGADMIN
  read -s -p "Passwort Admin: " PGADMINPASS; echo
  read -s -p "Passwort App-DB-User (${DBUSER}): " DBPASS; echo

  apt install -y postgresql-client
  export PGPASSWORD="$PGADMINPASS"
  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -tAc "SELECT 1 FROM pg_database WHERE datname='$DBNAME'" | grep -q 1 || psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "CREATE DATABASE $DBNAME;"
  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DBUSER'" | grep -q 1 || psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "CREATE USER $DBUSER;"
  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "ALTER USER $DBUSER WITH ENCRYPTED PASSWORD '$DBPASS';"
  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "ALTER DATABASE $DBNAME OWNER TO $DBUSER;"
  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "GRANT ALL PRIVILEGES ON DATABASE $DBNAME TO $DBUSER;"
  unset PGPASSWORD
fi

# -------------------------------------------------------------------
# Projekt
# -------------------------------------------------------------------
mkdir -p "$APPDIR"; chown "$APPUSER:$APPUSER" "$APPDIR"

sudo -u "$APPUSER" bash <<EOF
set -e
cd "$APPDIR"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install django gunicorn "psycopg[binary]" python-dotenv
django-admin startproject core .
python manage.py startapp app
EOF

# -------------------------------------------------------------------
# .env
# -------------------------------------------------------------------
cat > "$APPDIR/.env" <<EOF
MODE=$MODE
DEBUG=$DEBUG_VALUE
SECRET_KEY=$DJKEY
DB_NAME=$DBNAME
DB_USER=$DBUSER
DB_PASS=$DBPASS
DB_HOST=$DBHOST
DB_PORT=$DBPORT
ALLOWED_HOSTS=$ALLOWED_HOSTS
CSRF_TRUSTED_ORIGINS=
EOF

chown "$APPUSER:$APPUSER" "$APPDIR/.env"; chmod 600 "$APPDIR/.env"

# -------------------------------------------------------------------
# settings.py
# -------------------------------------------------------------------
cat > "$APPDIR/core/settings.py" <<'EOF'
from pathlib import Path
from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

MODE = os.getenv("MODE","dev").lower()
SECRET_KEY = os.getenv("SECRET_KEY","unsafe")
DEBUG = os.getenv("DEBUG","False") == "True"
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS","").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [h.strip() for h in os.getenv("CSRF_TRUSTED_ORIGINS","").split(",") if h.strip()]

INSTALLED_APPS = [
 "django.contrib.admin","django.contrib.auth","django.contrib.contenttypes",
 "django.contrib.sessions","django.contrib.messages","django.contrib.staticfiles","app",
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
 "DIRS": [], "APP_DIRS": True,
 "OPTIONS": {"context_processors": [
  "django.template.context_processors.request",
  "django.contrib.auth.context_processors.auth",
  "django.contrib.messages.context_processors.messages",
 ]},
}]

WSGI_APPLICATION = "core.wsgi.application"

DATABASES = {"default": {
 "ENGINE": "django.db.backends.postgresql",
 "NAME": os.getenv("DB_NAME"),
 "USER": os.getenv("DB_USER"),
 "PASSWORD": os.getenv("DB_PASS"),
 "HOST": os.getenv("DB_HOST"),
 "PORT": os.getenv("DB_PORT"),
}}

LANGUAGE_CODE="de-de"; TIME_ZONE="Europe/Luxembourg"
USE_I18N=True; USE_TZ=True
STATIC_URL="static/"; DEFAULT_AUTO_FIELD="django.db.models.BigAutoField"

if MODE == "prod":
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO","https")
    SESSION_COOKIE_HTTPONLY=True
EOF

chown "$APPUSER:$APPUSER" "$APPDIR/core/settings.py"

sudo -u "$APPUSER" bash -c "cd $APPDIR && .venv/bin/python manage.py migrate"

# -------------------------------------------------------------------
# systemd
# -------------------------------------------------------------------
cat > /etc/systemd/system/${PROJECTNAME}.service <<EOF
[Unit]
Description=$PROJECTNAME Django Service
After=network.target

[Service]
User=$APPUSER
WorkingDirectory=$APPDIR
EnvironmentFile=$APPDIR/.env
ExecStart=$APPDIR/.venv/bin/gunicorn core.wsgi:application --bind 127.0.0.1:8000 --workers 3
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$PROJECTNAME"

# -------------------------------------------------------------------
# nginx
# -------------------------------------------------------------------
cat > /etc/nginx/sites-available/$PROJECTNAME <<EOF
server {
  listen 80;
  server_name $NGINX_SERVER_NAMES;
  client_max_body_size 50M;
  location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
  }
}
EOF

ln -sf /etc/nginx/sites-available/$PROJECTNAME /etc/nginx/sites-enabled/$PROJECTNAME
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

# -------------------------------------------------------------------
# Done
# -------------------------------------------------------------------
echo "======================================"
echo "FERTIG: $APPDIR"
echo "Mode: $MODE (DEBUG=$DEBUG_VALUE)"
echo "nginx server_name: $NGINX_SERVER_NAMES"
echo
echo "Superuser anlegen:"
echo "sudo -u $APPUSER $APPDIR/.venv/bin/python $APPDIR/manage.py createsuperuser"
echo "======================================"