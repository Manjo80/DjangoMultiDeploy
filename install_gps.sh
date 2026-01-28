#!/bin/bash
set -euo pipefail

echo "=== GPS Manager Installer (Debian 12) ==="

# --- Projektname abfragen (Ordner unter /srv) ---
read -p "Projektname (Ordner unter /srv, z.B. gpsmgr): " PROJECTNAME
if [ -z "${PROJECTNAME:-}" ]; then
  echo "FEHLER: Projektname darf nicht leer sein."
  exit 1
fi

APPDIR="/srv/$PROJECTNAME"

# --- Fixe Defaults ---
DBNAME="gpsmgr"
DBUSER="gpsmgr_user"
DBPORT="5432"
DBHOST="localhost"
DBPASS=""

# --- Linux User ---
read -p "Linux-User für die App (z.B. gps): " APPUSER
if [ -z "${APPUSER:-}" ]; then
  echo "FEHLER: APPUSER darf nicht leer sein."
  exit 1
fi

# --- Secret Key ---
read -s -p "Django SECRET_KEY (leer = automatisch generieren): " DJKEY
echo
if [ -z "${DJKEY:-}" ]; then
  DJKEY="$(openssl rand -hex 32)"
fi

echo
echo "DB-Setup wählen:"
echo "  1) PostgreSQL lokal installieren + DB/User anlegen"
echo "  2) Remote PostgreSQL nutzen (Modus B: Admin-User darf DB + User anlegen)"
read -p "Auswahl (1/2): " DBMODE

if [[ "$DBMODE" != "1" && "$DBMODE" != "2" ]]; then
  echo "FEHLER: Bitte 1 oder 2 eingeben."
  exit 1
fi

# --- Pakete ---
apt update && apt upgrade -y
apt install -y sudo curl git nano ca-certificates openssl net-tools nginx \
               python3 python3-venv python3-pip build-essential libpq-dev

# --- App-User anlegen ---
if ! id "$APPUSER" &>/dev/null; then
  adduser "$APPUSER"
fi

cd /tmp

# --- DB Setup ---
if [ "$DBMODE" = "1" ]; then
  echo "=== Lokale PostgreSQL Installation ==="
  apt install -y postgresql postgresql-contrib
  systemctl enable --now postgresql

  read -s -p "Passwort für lokalen App-DB-User (${DBUSER}): " DBPASS
  echo
  [ -z "$DBPASS" ] && echo "FEHLER: DB Passwort leer." && exit 1

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

  read -p "Remote Admin-User: " PGADMIN
  read -s -p "Passwort für Remote Admin-User: " PGADMINPASS
  echo
  read -s -p "Passwort für App-DB-User (${DBUSER}): " DBPASS
  echo

  apt install -y postgresql-client
  export PGPASSWORD="$PGADMINPASS"

  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -tAc "SELECT 1 FROM pg_database WHERE datname='$DBNAME'" | grep -q 1 || \
    psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "CREATE DATABASE $DBNAME;"

  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DBUSER'" | grep -q 1 || \
    psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "CREATE USER $DBUSER;"

  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "ALTER USER $DBUSER WITH ENCRYPTED PASSWORD '$DBPASS';"
  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "ALTER DATABASE $DBNAME OWNER TO $DBUSER;"
  psql -h "$DBHOST" -p "$DBPORT" -U "$PGADMIN" -c "GRANT ALL PRIVILEGES ON DATABASE $DBNAME TO $DBUSER;"

  unset PGPASSWORD
fi

# --- Projektverzeichnis ---
echo "=== Projektverzeichnis: $APPDIR ==="
mkdir -p "$APPDIR"
chown "$APPUSER:$APPUSER" "$APPDIR"

# --- Django Projekt ---
sudo -u "$APPUSER" env APPDIR="$APPDIR" bash <<'EOF'
set -euo pipefail
cd "$APPDIR"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install django gunicorn "psycopg[binary]" python-dotenv
django-admin startproject core .
python manage.py startapp app
EOF

# --- .env ---
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

# --- settings.py ---
cat > "$APPDIR/core/settings.py" <<'EOF'
[... bleibt identisch wie vorher ...]
EOF
