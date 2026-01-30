#!/bin/bash
set -euo pipefail

# ================================================================
# OS / systemd Check (Debian / Ubuntu)
# ================================================================
. /etc/os-release || { echo "FEHLER: /etc/os-release fehlt"; exit 1; }
case "$ID" in debian|ubuntu) ;; *) echo "Nur Debian/Ubuntu unterstützt"; exit 1 ;; esac
command -v systemctl >/dev/null || { echo "systemd fehlt"; exit 1; }

export DEBIAN_FRONTEND=noninteractive

echo "=== Django Installer (${PRETTY_NAME}) ==="

# ================================================================
# Projekt
# ================================================================
read -p "Projektname (unter /srv): " PROJECTNAME
[ -z "$PROJECTNAME" ] && echo "FEHLER: leer" && exit 1
APPDIR="/srv/$PROJECTNAME"

# ================================================================
# Lokale IP
# ================================================================
LOCAL_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {print $7; exit}')"
[ -z "$LOCAL_IP" ] && LOCAL_IP="$(hostname -I | awk '{print $1}')"
[ -z "$LOCAL_IP" ] && LOCAL_IP="127.0.0.1"

# ================================================================
# Mode
# ================================================================
echo "1) DEV   2) PROD"
read -p "Modus [1]: " MODESEL
MODESEL="${MODESEL:-1}"
if [ "$MODESEL" = "2" ]; then MODE="prod"; DEBUG_VALUE="False"; else MODE="dev"; DEBUG_VALUE="True"; fi

# ================================================================
# Hosts
# ================================================================
DEFAULT_ALLOWED_HOSTS="${LOCAL_IP},127.0.0.1,localhost"
read -p "ALLOWED_HOSTS [$DEFAULT_ALLOWED_HOSTS]: " ALLOWED_HOSTS
ALLOWED_HOSTS="${ALLOWED_HOSTS:-$DEFAULT_ALLOWED_HOSTS}"
NGINX_SERVER_NAMES="$(echo "$ALLOWED_HOSTS" | tr ',' ' ' | xargs)"
[ -z "$NGINX_SERVER_NAMES" ] && NGINX_SERVER_NAMES="_"

# ================================================================
# CSRF_TRUSTED_ORIGINS automatisch aus ALLOWED_HOSTS
# ================================================================
CSRF_TRUSTED_ORIGINS="$(echo "$ALLOWED_HOSTS" | tr ',' '\n' | awk '
  NF {
    h=$0
    gsub(/^[ \t]+|[ \t]+$/, "", h)
    if (h == "" || h == "localhost") next
    if (h ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) next
    print "https://" h
  }' | awk '!seen[$0]++' | paste -sd, -)"

# ================================================================
# Linux App User
# ================================================================
read -p "Linux App-User (z.B. gps): " APPUSER
[ -z "$APPUSER" ] && echo "FEHLER: leer" && exit 1

# ================================================================
# DB Zugangsdaten (ABGEFRAGT)
# ================================================================
read -p "PostgreSQL DB-Name: " DBNAME
[ -z "$DBNAME" ] && echo "FEHLER: leer" && exit 1

read -p "PostgreSQL DB-User: " DBUSER
[ -z "$DBUSER" ] && echo "FEHLER: leer" && exit 1

read -s -p "PostgreSQL DB-Passwort: " DBPASS
echo
[ -z "$DBPASS" ] && echo "FEHLER: leer" && exit 1

DBHOST="localhost"
DBPORT="5432"

# ================================================================
# Django Secret
# ================================================================
read -s -p "Django SECRET_KEY (leer = auto): " DJKEY
echo
[ -z "$DJKEY" ] && DJKEY="$(openssl rand -hex 32)"

# ================================================================
# DB Mode
# ================================================================
echo "1) Lokale PostgreSQL   2) Remote PostgreSQL"
read -p "DB Modus: " DBMODE
[[ "$DBMODE" != "1" && "$DBMODE" != "2" ]] && exit 1

# ================================================================
# Pakete
# ================================================================
apt update && apt upgrade -y
apt install -y sudo git nginx python3 python3-venv python3-pip \
               build-essential libpq-dev postgresql-client iproute2 openssl

# ================================================================
# Linux User
# ================================================================
id "$APPUSER" &>/dev/null || adduser "$APPUSER"
usermod -aG sudo "$APPUSER"
echo "$APPUSER ALL=(ALL) ALL" > /etc/sudoers.d/$APPUSER
chmod 440 /etc/sudoers.d/$APPUSER

# ================================================================
# PostgreSQL
# ================================================================
if [ "$DBMODE" = "1" ]; then
  apt install -y postgresql postgresql-contrib
  systemctl enable --now postgresql

  sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='$DBUSER'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER $DBUSER WITH PASSWORD '$DBPASS';"

  sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='$DBNAME'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE $DBNAME OWNER $DBUSER;"
else
  read -p "Remote DB Host: " DBHOST
fi

# ================================================================
# Projekt + venv (als App-User)
# ================================================================
mkdir -p "$APPDIR"
chown "$APPUSER:$APPUSER" "$APPDIR"

sudo -u "$APPUSER" bash <<EOF
cd "$APPDIR"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install django gunicorn psycopg[binary] python-dotenv
django-admin startproject core .
python manage.py startapp app
EOF

# ================================================================
# .env
# ================================================================
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
CSRF_TRUSTED_ORIGINS=$CSRF_TRUSTED_ORIGINS
EOF

chown "$APPUSER:$APPUSER" "$APPDIR/.env"
chmod 600 "$APPDIR/.env"

# ================================================================
# settings.py
# ================================================================
cat > "$APPDIR/core/settings.py" <<'EOF'
from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("SECRET_KEY")
DEBUG = os.getenv("DEBUG") == "True"

ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS","").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [h.strip() for h in os.getenv("CSRF_TRUSTED_ORIGINS","").split(",") if h.strip()]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME"),
        "USER": os.getenv("DB_USER"),
        "PASSWORD": os.getenv("DB_PASS"),
        "HOST": os.getenv("DB_HOST"),
        "PORT": os.getenv("DB_PORT"),
    }
}

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
WSGI_APPLICATION = "core.wsgi.application"
LANGUAGE_CODE="de-de"
TIME_ZONE="Europe/Luxembourg"
USE_I18N=True
USE_TZ=True
STATIC_URL="static/"
DEFAULT_AUTO_FIELD="django.db.models.BigAutoField"

if os.getenv("MODE") == "prod":
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO","https")
EOF

chown "$APPUSER:$APPUSER" "$APPDIR/core/settings.py"

# ================================================================
# Migration
# ================================================================
sudo -u "$APPUSER" "$APPDIR/.venv/bin/python" "$APPDIR/manage.py" migrate

# ================================================================
# systemd
# ================================================================
cat > /etc/systemd/system/$PROJECTNAME.service <<EOF
[Unit]
Description=$PROJECTNAME
After=network.target

[Service]
User=$APPUSER
WorkingDirectory=$APPDIR
EnvironmentFile=$APPDIR/.env
ExecStart=$APPDIR/.venv/bin/gunicorn core.wsgi:application --bind 127.0.0.1:8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$PROJECTNAME"

# ================================================================
# nginx
# ================================================================
cat > /etc/nginx/sites-available/$PROJECTNAME <<EOF
server {
  listen 80;
  server_name $NGINX_SERVER_NAMES;
  location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-For \$remote_addr;
    proxy_set_header X-Forwarded-Proto \$scheme;
  }
}
EOF

ln -sf /etc/nginx/sites-available/$PROJECTNAME /etc/nginx/sites-enabled/$PROJECTNAME
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

# ================================================================
# Update-Skript mit Logging
# ================================================================
LOGDIR="/var/log/$PROJECTNAME"
mkdir -p "$LOGDIR"
chmod 750 "$LOGDIR"

cat > /usr/local/sbin/${PROJECTNAME}_update.sh <<EOF
#!/bin/bash
set -euo pipefail

APPDIR="$APPDIR"
APPUSER="$APPUSER"
PROJECT="$PROJECTNAME"
LOGDIR="$LOGDIR"
TS=\$(date +%Y%m%d_%H%M%S)
LOGFILE="\$LOGDIR/update-\$TS.log"

exec > >(tee -a "\$LOGFILE") 2>&1

echo "=== UPDATE START \$(date -Is) ==="

cd "\$APPDIR" || exit 1

if [ -d .git ]; then
  sudo -u "\$APPUSER" git pull
fi

if [ -f requirements.txt ]; then
  sudo -u "\$APPUSER" "\$APPDIR/.venv/bin/pip" install -r requirements.txt
fi

sudo -u "\$APPUSER" "\$APPDIR/.venv/bin/python" manage.py migrate
systemctl restart "\$PROJECT"
systemctl is-active "\$PROJECT"

echo "=== UPDATE OK ==="
echo "Log: \$LOGFILE"
EOF

chmod 750 /usr/local/sbin/${PROJECTNAME}_update.sh

# ================================================================
# Done
# ================================================================
echo "======================================"
echo "FERTIG"
echo "Projekt: $APPDIR"
echo "Linux-User: $APPUSER"
echo "DB-User: $DBUSER"
echo "Update:"
echo "  sudo /usr/local/sbin/${PROJECTNAME}_update.sh"
echo "======================================"
