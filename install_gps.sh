#!/bin/bash
set -euo pipefail

echo "=== GPS Manager Installer (Debian 12) ==="

read -p "Linux-User für die App (z.B. gps): " APPUSER
APPDIR="/srv/gpsmgr"

read -s -p "Django SECRET_KEY (leer = automatisch generieren): " DJKEY
echo
if [ -z "$DJKEY" ]; then
  DJKEY="$(openssl rand -hex 32)"
fi

echo
echo "DB-Setup wählen:"
echo "  1) PostgreSQL lokal installieren + DB/User anlegen"
echo "  2) Remote PostgreSQL nutzen (Modus B: Admin-User darf DB + User anlegen)"
read -p "Auswahl (1/2): " DBMODE

DBNAME="gpsmgr"
DBUSER="gpsmgr_user"
DBPORT="5432"

apt update && apt upgrade -y
apt install -y sudo curl git nano ca-certificates openssl net-tools nginx \
               python3 python3-venv python3-pip build-essential libpq-dev

if ! id "$APPUSER" &>/dev/null; then
  adduser "$APPUSER"
fi

DBHOST="localhost"
DBPASS=""

cd /tmp   # <<< WICHTIG: verhindert psql Permission-Warnungen

if [ "$DBMODE" = "1" ]; then
  echo "=== Lokale PostgreSQL Installation ==="
  apt install -y postgresql postgresql-contrib
  systemctl enable --now postgresql

  read -s -p "Passwort für lokalen App-DB-User (${DBUSER}): " DBPASS
  echo

  sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DBNAME'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE $DBNAME;"

  sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DBUSER'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER $DBUSER WITH ENCRYPTED PASSWORD '$DBPASS';"

  sudo -u postgres psql -c "ALTER DATABASE $DBNAME OWNER TO $DBUSER;"
  sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DBNAME TO $DBUSER;"

else
  echo "=== Remote PostgreSQL (Modus B) ==="
  read -p "Remote DB Host/IP: " DBHOST
  read -p "Remote DB Port (default 5432): " TMPPORT
  DBPORT="${TMPPORT:-5432}"

  read -p "Remote Admin-User (vom DB-Admin angelegt): " PGADMIN
  read -s -p "Passwort für Remote Admin-User: " PGADMINPASS
  echo

  read -s -p "Passwort für App-DB-User (${DBUSER}): " DBPASS
  echo

  apt install -y postgresql-client
  export PGPASSWORD="$PGADMINPASS"

  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -tAc \
    "SELECT 1 FROM pg_database WHERE datname='$DBNAME'" | grep -q 1 || \
    psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "CREATE DATABASE $DBNAME;"

  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -tAc \
    "SELECT 1 FROM pg_roles WHERE rolname='$DBUSER'" | grep -q 1 || \
    psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "CREATE USER $DBUSER;"

  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c \
    "ALTER USER $DBUSER WITH ENCRYPTED PASSWORD '$DBPASS';"

  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c \
    "ALTER DATABASE $DBNAME OWNER TO $DBUSER;"

  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c \
    "GRANT ALL PRIVILEGES ON DATABASE $DBNAME TO $DBUSER;"

  unset PGPASSWORD
fi

echo "=== Projektverzeichnis ==="
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

echo "=== .env schreiben ==="
cat > "$APPDIR/.env" <<EOF
DEBUG=True
SECRET_KEY=$DJKEY
DB_NAME=$DBNAME
DB_USER=$DBUSER
DB_PASS=$DBPASS
DB_HOST=$DBHOST
DB_PORT=$DBPORT
ALLOWED_HOSTS=*
EOF

chown "$APPUSER:$APPUSER" "$APPDIR/.env"
chmod 600 "$APPDIR/.env"

echo "=== settings.py schreiben ==="
cat > "$APPDIR/core/settings.py" <<'EOF'
from pathlib import Path
from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("SECRET_KEY","unsafe-dev-key")
DEBUG = os.getenv("DEBUG","False") == "True"
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS","*").split(",")

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
 "BACKEND":"django.template.backends.django.DjangoTemplates",
 "DIRS":[],
 "APP_DIRS":True,
 "OPTIONS":{"context_processors":[
  "django.template.context_processors.request",
  "django.contrib.auth.context_processors.auth",
  "django.contrib.messages.context_processors.messages",
 ]}
}]

WSGI_APPLICATION = "core.wsgi.application"

DATABASES = {
 "default": {
  "ENGINE":"django.db.backends.postgresql",
  "NAME":os.getenv("DB_NAME"),
  "USER":os.getenv("DB_USER"),
  "PASSWORD":os.getenv("DB_PASS"),
  "HOST":os.getenv("DB_HOST"),
  "PORT":os.getenv("DB_PORT"),
 }
}

AUTH_PASSWORD_VALIDATORS = [
 {"NAME":"django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
 {"NAME":"django.contrib.auth.password_validation.MinimumLengthValidator"},
 {"NAME":"django.contrib.auth.password_validation.CommonPasswordValidator"},
 {"NAME":"django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "de-de"
TIME_ZONE = "Europe/Luxembourg"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
EOF

chown "$APPUSER:$APPUSER" "$APPDIR/core/settings.py"
python3 -m py_compile "$APPDIR/core/settings.py"

sudo -u "$APPUSER" bash <<EOF
cd "$APPDIR"
source .venv/bin/activate
python manage.py migrate
EOF

cat > /etc/systemd/system/gpsmgr.service <<EOF
[Unit]
Description=GPS Manager
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
systemctl enable --now gpsmgr

cat > /etc/nginx/sites-available/gpsmgr <<'EOF'
server {
 listen 80;
 server_name _;
 client_max_body_size 50M;

 location / {
  proxy_pass http://127.0.0.1:8000;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
 }
}
EOF

ln -sf /etc/nginx/sites-available/gpsmgr /etc/nginx/sites-enabled/gpsmgr
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

echo "======================================"
echo "FERTIG"
echo "Server IP(s): $(hostname -I)"
echo "Superuser anlegen:"
echo "sudo -u $APPUSER bash -lc 'cd $APPDIR && source .venv/bin/activate && python manage.py createsuperuser'"
echo "Dann öffnen: http://SERVER-IP/admin"
echo "======================================"
