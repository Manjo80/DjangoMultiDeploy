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

export DEBIAN_FRONTEND=noninteractive

echo "=== Django Installer (${PRETTY_NAME:-$ID}) ==="

# -------------------------------------------------------------------
# Projektname -> /srv/<name>
# -------------------------------------------------------------------
read -p "Projektname (Ordner unter /srv, z.B. gpsmgr oder gps-manager): " PROJECTNAME
[ -z "${PROJECTNAME:-}" ] && echo "FEHLER: Projektname leer." && exit 1
APPDIR="/srv/$PROJECTNAME"

# -------------------------------------------------------------------
# Local IP (Default Hosts)
# -------------------------------------------------------------------
LOCAL_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {print $7; exit}')"
[ -z "${LOCAL_IP:-}" ] && LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "${LOCAL_IP:-}" ] && LOCAL_IP="127.0.0.1"

# -------------------------------------------------------------------
# Mode: DEV vs PROD
# -------------------------------------------------------------------
echo
echo "Modus:"
echo "  1) DEV  (DEBUG=True)"
echo "  2) PROD (DEBUG=False, Reverse-Proxy geeignet)"
read -p "Auswahl (1/2) [1]: " MODESEL
MODESEL="${MODESEL:-1}"
[[ "$MODESEL" != "1" && "$MODESEL" != "2" ]] && echo "FEHLER: Bitte 1 oder 2." && exit 1
if [ "$MODESEL" = "1" ]; then MODE="dev"; DEBUG_VALUE="True"; else MODE="prod"; DEBUG_VALUE="False"; fi

# -------------------------------------------------------------------
# Hosts
# -------------------------------------------------------------------
DEFAULT_ALLOWED_HOSTS="${LOCAL_IP},127.0.0.1,localhost"
[ "$MODE" = "prod" ] && echo "PROD: DNS-Namen eintragen (z.B. gps.famhub.eu, app.intern.lan)"
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
# Defaults DB (Postgres: keine '-' in unquoted identifiers)
# -------------------------------------------------------------------
DBNAME_DEFAULT="${PROJECTNAME//-/_}"
DBUSER_DEFAULT="${PROJECTNAME//-/_}_user"
DBPORT="5432"
DBHOST="localhost"
DBPASS=""

echo
read -p "DB Name [${DBNAME_DEFAULT}]: " TMP_DBNAME
DBNAME="${TMP_DBNAME:-$DBNAME_DEFAULT}"
DBNAME="${DBNAME//-/_}"

read -p "DB User [${DBUSER_DEFAULT}]: " TMP_DBUSER
DBUSER="${TMP_DBUSER:-$DBUSER_DEFAULT}"
DBUSER="${DBUSER//-/_}"

# -------------------------------------------------------------------
# Linux User
# -------------------------------------------------------------------
read -p "Linux-User für App (wird erstellt, z.B. gps): " APPUSER
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
[[ "$DBMODE" != "1" && "$DBMODE" != "2" ]] && echo "FEHLER: Bitte 1 oder 2." && exit 1

# -------------------------------------------------------------------
# Pakete
# -------------------------------------------------------------------
apt update && apt upgrade -y
apt install -y sudo curl git nano ca-certificates openssl net-tools nginx \
               python3 python3-venv python3-pip build-essential libpq-dev iproute2

# -------------------------------------------------------------------
# App-User
# -------------------------------------------------------------------
if ! id "$APPUSER" &>/dev/null; then
  adduser "$APPUSER"
fi

# Optional: generelles sudo (wie du wolltest)
usermod -aG sudo "$APPUSER"
echo "$APPUSER ALL=(ALL) ALL" > /etc/sudoers.d/$APPUSER
chmod 440 /etc/sudoers.d/$APPUSER

# -------------------------------------------------------------------
# PostgreSQL (neutrales Arbeitsverzeichnis)
# -------------------------------------------------------------------
cd /tmp

if [ "$DBMODE" = "1" ]; then
  echo "=== Lokale PostgreSQL Installation ==="
  apt install -y postgresql postgresql-contrib
  systemctl enable --now postgresql

  read -s -p "Passwort für lokalen DB-User (${DBUSER}): " DBPASS; echo
  [ -z "$DBPASS" ] && echo "FEHLER: Passwort leer." && exit 1

  sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DBNAME'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE \"$DBNAME\";"

  sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DBUSER'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER \"$DBUSER\" WITH ENCRYPTED PASSWORD '$DBPASS';"

  sudo -u postgres psql -c "ALTER DATABASE \"$DBNAME\" OWNER TO \"$DBUSER\";"
  sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE \"$DBNAME\" TO \"$DBUSER\";"
else
  echo "=== Remote PostgreSQL ==="
  read -p "Remote DB Host/IP: " DBHOST
  read -p "Remote DB Port [5432]: " TMPPORT
  DBPORT="${TMPPORT:-5432}"
  read -p "Remote Admin-User: " PGADMIN
  read -s -p "Passwort Admin: " PGADMINPASS; echo
  read -s -p "Passwort App-DB-User (${DBUSER}): " DBPASS; echo

  apt install -y postgresql-client
  export PGPASSWORD="$PGADMINPASS"

  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -tAc "SELECT 1 FROM pg_database WHERE datname='$DBNAME'" | grep -q 1 || \
    psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "CREATE DATABASE \"$DBNAME\";"

  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DBUSER'" | grep -q 1 || \
    psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "CREATE USER \"$DBUSER\";"

  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "ALTER USER \"$DBUSER\" WITH ENCRYPTED PASSWORD '$DBPASS';"
  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "ALTER DATABASE \"$DBNAME\" OWNER TO \"$DBUSER\";"
  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "GRANT ALL PRIVILEGES ON DATABASE \"$DBNAME\" TO \"$DBUSER\";"

  unset PGPASSWORD
fi

# -------------------------------------------------------------------
# Projektverzeichnis
# -------------------------------------------------------------------
mkdir -p "$APPDIR"
chown "$APPUSER:$APPUSER" "$APPDIR"

# -------------------------------------------------------------------
# Django Setup
# -------------------------------------------------------------------
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
CSRF_TRUSTED_ORIGINS=$CSRF_TRUSTED_ORIGINS_VALUE
EOF

chown "$APPUSER:$APPUSER" "$APPDIR/.env"
chmod 600 "$APPDIR/.env"

# -------------------------------------------------------------------
# settings.py (proxy/CSRF/Static korrekt)
# -------------------------------------------------------------------
cat > "$APPDIR/core/settings.py" <<'EOF'
from pathlib import Path
from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

MODE = os.getenv("MODE", "dev").lower()
SECRET_KEY = os.getenv("SECRET_KEY", "unsafe")
DEBUG = os.getenv("DEBUG", "False") == "True"

def env_list(name: str):
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin","django.contrib.auth","django.contrib.contenttypes",
    "django.contrib.sessions","django.contrib.messages","django.contrib.staticfiles",
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
    "HOST": os.getenv("DB_HOST", "localhost"),
    "PORT": os.getenv("DB_PORT", "5432"),
}}

LANGUAGE_CODE = "de-de"
TIME_ZONE = "Europe/Luxembourg"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

if MODE == "prod":
    USE_X_FORWARDED_HOST = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_SECURE = True
EOF

chown "$APPUSER:$APPUSER" "$APPDIR/core/settings.py"

# -------------------------------------------------------------------
# Migration
# -------------------------------------------------------------------
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
# Sudoers (Lösung A): App-User darf NUR Service restart/status/logs ohne Passwort
# -------------------------------------------------------------------
cat > /etc/sudoers.d/${PROJECTNAME}-service <<EOF
$APPUSER ALL=NOPASSWD: /bin/systemctl restart $PROJECTNAME, /bin/systemctl status $PROJECTNAME, /bin/journalctl -u $PROJECTNAME
EOF
chmod 440 /etc/sudoers.d/${PROJECTNAME}-service

# -------------------------------------------------------------------
# UPDATE-Skript (Lösung A) + migration check
# -------------------------------------------------------------------
cat > /usr/local/sbin/${PROJECTNAME}_update.sh <<EOF
#!/bin/bash
set -euo pipefail

APPDIR="$APPDIR"
SERVICE="$PROJECTNAME"

echo "=== UPDATE START (\$SERVICE) ==="
cd "\$APPDIR"

echo "-- git pull"
git pull

if [ -f "\$APPDIR/requirements.txt" ]; then
  echo "-- install requirements"
  . "\$APPDIR/.venv/bin/activate"
  pip install -r "\$APPDIR/requirements.txt"
else
  echo "-- requirements.txt nicht gefunden (überspringe pip install)"
fi

echo "-- migration check (warn if missing migrations)"
"\$APPDIR/.venv/bin/python" "\$APPDIR/manage.py" makemigrations --check --dry-run

echo "-- migrate"
"\$APPDIR/.venv/bin/python" "\$APPDIR/manage.py" migrate

echo "-- restart service (sudo, nopasswd)"
sudo systemctl restart "\$SERVICE"

echo "=== UPDATE DONE ==="
EOF

chmod 755 /usr/local/sbin/${PROJECTNAME}_update.sh

# -------------------------------------------------------------------
# LOGIN-Hinweis: bei jedem Login Kommandos anzeigen
# -------------------------------------------------------------------
cat > /etc/profile.d/${PROJECTNAME}_motd.sh <<EOF
case "\$-" in
  *i*) ;;
  *) return ;;
esac

echo
echo "=== $PROJECTNAME Commands ==="
echo "Project dir: $APPDIR"
echo "Service:     $PROJECTNAME"
echo
echo "Update (run as $APPUSER, no sudo needed):"
echo "  ${PROJECTNAME}_update.sh"
echo
echo "Status/Logs:"
echo "  systemctl status $PROJECTNAME"
echo "  journalctl -u $PROJECTNAME -f"
echo
echo "Create Django superuser:"
echo "  sudo -u $APPUSER bash -c 'cd $APPDIR && .venv/bin/python manage.py createsuperuser'"
echo
echo "CSRF_TRUSTED_ORIGINS in PROD auto:"
echo "  $CSRF_TRUSTED_ORIGINS_VALUE"
echo "============================="
echo
EOF
chmod 644 /etc/profile.d/${PROJECTNAME}_motd.sh

# -------------------------------------------------------------------
# Done
# -------------------------------------------------------------------
echo "======================================"
echo "FERTIG: $APPDIR"
echo "OS: ${PRETTY_NAME:-$ID}"
echo "Mode: $MODE (DEBUG=$DEBUG_VALUE)"
echo "ALLOWED_HOSTS: $ALLOWED_HOSTS"
echo "CSRF_TRUSTED_ORIGINS: $CSRF_TRUSTED_ORIGINS_VALUE"
echo
echo "Update Script (run as $APPUSER):"
echo "  ${PROJECTNAME}_update.sh"
echo
echo "Superuser anlegen:"
echo "  sudo -u $APPUSER bash -c 'cd $APPDIR && .venv/bin/python manage.py createsuperuser'"
echo
echo "Hinweis: Die Kommandos werden bei jedem Login angezeigt."
echo "======================================"
