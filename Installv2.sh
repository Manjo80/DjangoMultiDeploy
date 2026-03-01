#!/bin/bash
set -euo pipefail

# ===================================================================
# Django Installer - Secure & Flexible (LXC/Container ready)
# Version: 2.0 - Improved & Optimized
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
# Root-Check
# -------------------------------------------------------------------
echo "🔧 Prüfe Systemumgebung..."
if [ "$(id -u)" -ne 0 ]; then
  echo "❌ FEHLER: Skript muss als root ausgeführt werden!"
  exit 1
fi

echo "✅ Ausführung als root bestätigt"

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
echo "GitHub Repository (optional):"
echo "  • Öffentliches Repo: https://github.com/user/repo.git"
echo "  • Privates Repo:     git@github.com:user/repo.git"
echo "  • Leer lassen für neues Django-Projekt (ohne GitHub)"
read -p "GitHub URL (leer für neues Projekt): " GITHUB_REPO_URL
USE_GITHUB="${GITHUB_REPO_URL:+true}"

if [[ "$USE_GITHUB" == "true" ]]; then
  echo "✅ GitHub-Modus aktiviert: Repository wird geklont"
else
  echo "✅ Lokaler Modus: Neues Django-Projekt wird erstellt"
fi

# -------------------------------------------------------------------
# Local IPs (Default Hosts) - ALLE Netzwerk-IPs erkennen
# -------------------------------------------------------------------
LOCAL_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {print $7; exit}')"
[ -z "${LOCAL_IP:-}" ] && LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "${LOCAL_IP:-}" ] && LOCAL_IP="127.0.0.1"

# Alle IPs sammeln (für ALLOWED_HOSTS)
ALL_LOCAL_IPS="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$' | sort -u | paste -sd, -)"
[ -z "${ALL_LOCAL_IPS:-}" ] && ALL_LOCAL_IPS="$LOCAL_IP"

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
DEFAULT_ALLOWED_HOSTS="${ALL_LOCAL_IPS},127.0.0.1,localhost,${HOSTNAME_FQDN}"
[ "$MODE" = "prod" ] && echo "PROD: DNS-Namen eintragen (z.B. app.intern.lan)"
read -p "ALLOWED_HOSTS (Komma-separiert) [${DEFAULT_ALLOWED_HOSTS}]: " ALLOWED_HOSTS
ALLOWED_HOSTS="${ALLOWED_HOSTS:-$DEFAULT_ALLOWED_HOSTS}"
NGINX_SERVER_NAMES="$(echo "$ALLOWED_HOSTS" | tr ',' ' ' | xargs)"
[ -z "${NGINX_SERVER_NAMES:-}" ] && NGINX_SERVER_NAMES="_"

# -------------------------------------------------------------------
# CSRF_TRUSTED_ORIGINS automatisch bauen (für alle Modi)
# -------------------------------------------------------------------
CSRF_TRUSTED_ORIGINS_VALUE="$(echo "$ALLOWED_HOSTS" | tr ',' '\n' | awk '
  NF {
    gsub(/^[ \t]+|[ \t]+$/, "", $0)
    if ($0 == "") next
    if ($0 == "localhost" || $0 == "127.0.0.1") {
      print "http://" $0
      print "https://" $0
    } else {
      print "https://" $0
    }
  }' | awk '!seen[$0]++' | paste -sd, -)"

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
# Linux User
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
# SSH-Key für App-User erstellen
# -------------------------------------------------------------------
echo
echo "🔐 SSH-Key für SSH-Zugriff (WinSCP/PuTTY)"
echo "   Dieser Key ermöglicht SSH-Login als $APPUSER"
echo
read -p "SSH-Key Passphrase (leer für kein Passwort): " SSH_KEY_PASSPHRASE

SSH_KEY_PATH="/home/${APPUSER}/.ssh/id_ed25519"

# -------------------------------------------------------------------
# System-Pakete aktualisieren
# -------------------------------------------------------------------
apt update

read -p "System-Pakete updaten? (empfohlen) [J/n]: " UPGRADE
[[ "${UPGRADE:-J}" =~ ^[Jj]$ ]] && apt upgrade -y

# Basis-Pakete
echo "📦 Installiere Basis-Pakete..."
apt install -y curl git nano ca-certificates openssl net-tools nginx \
               python3 python3-venv python3-pip build-essential iproute2 sudo

# Bildverarbeitung (Pillow)
echo "🖼️  Installiere Pillow-Abhängigkeiten..."
apt install -y libjpeg-dev zlib1g-dev libpng-dev libwebp-dev

# DB-spezifische Pakete
if [ "$DBTYPE" = "postgresql" ]; then
  apt install -y libpq-dev
elif [ "$DBTYPE" = "mysql" ]; then
  apt install -y libmysqlclient-dev python3-dev default-libmysqlclient-dev
fi

# -------------------------------------------------------------------
# fail2ban installieren
# -------------------------------------------------------------------
read -p "fail2ban installieren (schützt SSH)? [J/n]: " INSTALL_FAIL2BAN
INSTALL_FAIL2BAN="${INSTALL_FAIL2BAN:-J}"
if [[ "$INSTALL_FAIL2BAN" =~ ^[Jj]$ ]]; then
  echo "🛡️  Installiere fail2ban..."
  apt install -y fail2ban
  cat > /etc/fail2ban/jail.local <<EOF
[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 3
bantime = 3600
EOF
  systemctl enable --now fail2ban
  echo "✅ fail2ban aktiviert"
else
  echo "⏭️  fail2ban übersprungen"
fi

# -------------------------------------------------------------------
# SSH-Server: PasswordAuthentication + PermitRootLogin sicherstellen
# -------------------------------------------------------------------
echo "🔐 Prüfe SSH-Server Konfiguration..."
SSHD_CONFIG="/etc/ssh/sshd_config"
SSHD_CHANGED=false

# PasswordAuthentication aktivieren (nötig für SCP Key-Download mit Passwort)
if grep -qE '^\s*PasswordAuthentication\s+no' "$SSHD_CONFIG" 2>/dev/null; then
  sed -i 's/^\s*PasswordAuthentication\s\+no/PasswordAuthentication yes/' "$SSHD_CONFIG"
  SSHD_CHANGED=true
  echo "  ✅ PasswordAuthentication auf 'yes' gesetzt"
elif ! grep -qE '^\s*PasswordAuthentication\s+yes' "$SSHD_CONFIG" 2>/dev/null; then
  echo "PasswordAuthentication yes" >> "$SSHD_CONFIG"
  SSHD_CHANGED=true
  echo "  ✅ PasswordAuthentication hinzugefügt"
else
  echo "  ✅ PasswordAuthentication bereits aktiv"
fi

# PermitRootLogin aktivieren (nötig für scp root@...)
if grep -qE '^\s*PermitRootLogin\s+(no|prohibit-password|forced-commands-only)' "$SSHD_CONFIG" 2>/dev/null; then
  sed -i 's/^\s*PermitRootLogin\s\+\(no\|prohibit-password\|forced-commands-only\)/PermitRootLogin yes/' "$SSHD_CONFIG"
  SSHD_CHANGED=true
  echo "  ✅ PermitRootLogin auf 'yes' gesetzt"
elif ! grep -qE '^\s*PermitRootLogin' "$SSHD_CONFIG" 2>/dev/null; then
  echo "PermitRootLogin yes" >> "$SSHD_CONFIG"
  SSHD_CHANGED=true
  echo "  ✅ PermitRootLogin hinzugefügt"
else
  echo "  ✅ PermitRootLogin bereits aktiv"
fi

if [ "$SSHD_CHANGED" = true ]; then
  systemctl restart sshd 2>/dev/null || systemctl restart ssh 2>/dev/null || true
  echo "  🔄 SSH-Server neu gestartet"
fi

# -------------------------------------------------------------------
# App-User erstellen
# -------------------------------------------------------------------
if ! id "$APPUSER" &>/dev/null; then
  echo "👤 Erstelle Benutzer: $APPUSER"
  adduser --disabled-password --gecos "" "$APPUSER" 2>/dev/null || adduser --disabled-password "$APPUSER"

  # Home-Verzeichnis sicherstellen
  if [ ! -d "/home/$APPUSER" ]; then
    mkdir -p "/home/$APPUSER"
    chown "$APPUSER:$APPUSER" "/home/$APPUSER"
  fi
fi

# Passwort für App-User setzen (für SSH-Login + Django Admin)
echo
echo "🔑 Passwort für Linux-Benutzer '$APPUSER' setzen"
echo "   (Wird auch für SSH/SCP-Login benötigt)"
while true; do
  read -s -p "Passwort für $APPUSER: " APPUSER_PASS; echo
  [ -z "$APPUSER_PASS" ] && echo "❌ Passwort darf nicht leer sein." && continue
  read -s -p "Passwort bestätigen: " APPUSER_PASS2; echo
  if [ "$APPUSER_PASS" = "$APPUSER_PASS2" ]; then
    echo "$APPUSER:$APPUSER_PASS" | chpasswd
    echo "✅ Passwort für $APPUSER gesetzt"
    break
  else
    echo "❌ Passwörter stimmen nicht überein. Erneut versuchen."
  fi
done

# SSH-Verzeichnis erstellen
echo "🔑 Erstelle SSH-Key für Benutzer $APPUSER..."
mkdir -p "/home/$APPUSER/.ssh"
chown "$APPUSER:$APPUSER" "/home/$APPUSER/.ssh"
chmod 700 "/home/$APPUSER/.ssh"

# SSH-Key erstellen
if [ -z "$SSH_KEY_PASSPHRASE" ]; then
  ssh-keygen -t ed25519 -C "${APPUSER}@$(hostname -f 2>/dev/null || echo 'server')" \
    -f "$SSH_KEY_PATH" -N "" -q
else
  ssh-keygen -t ed25519 -C "${APPUSER}@$(hostname -f 2>/dev/null || echo 'server')" \
    -f "$SSH_KEY_PATH" -N "$SSH_KEY_PASSPHRASE" -q
fi

# Berechtigungen setzen
chmod 600 "$SSH_KEY_PATH"
chmod 644 "${SSH_KEY_PATH}.pub"
chown "$APPUSER:$APPUSER" "$SSH_KEY_PATH"
chown "$APPUSER:$APPUSER" "${SSH_KEY_PATH}.pub"

# Public Key in authorized_keys eintragen (ermöglicht SSH-Login mit diesem Key)
cat "${SSH_KEY_PATH}.pub" >> "/home/$APPUSER/.ssh/authorized_keys"
chown "$APPUSER:$APPUSER" "/home/$APPUSER/.ssh/authorized_keys"
chmod 600 "/home/$APPUSER/.ssh/authorized_keys"

echo "✅ SSH-Key erstellt: $SSH_KEY_PATH"
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔐 ÖFFENTLICHER KEY (für SSH authorized_keys):"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
cat "${SSH_KEY_PATH}.pub"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

# GitHub Setup
GITHUB_SSH_OPTS="-o ConnectTimeout=30"
if [[ "$USE_GITHUB" == "true" ]]; then
  echo "📦 GitHub Repository erkannt: $GITHUB_REPO_URL"
  echo
  echo "⚠️  WICHTIG FÜR PRIVATE REPOS:"
  echo "   1. Kopiere den öffentlichen Key oben"
  echo "   2. Gehe zu: GitHub → Settings → SSH and GPG keys → New SSH key"
  echo "   3. Titel: '${PROJECTNAME} - $(hostname -f 2>/dev/null || echo 'server')'"
  echo "   4. Key einfügen und speichern"
  echo
  read -p "Fortfahren nachdem der Key zu GitHub hinzugefügt wurde? (J/n): " CONFIRM
  [[ ! "${CONFIRM:-J}" =~ ^[Jj]$ ]] && echo "❌ Abbruch." && exit 1

  # known_hosts für github.com
  mkdir -p "/home/$APPUSER/.ssh"
  ssh-keyscan -H github.com >> "/home/$APPUSER/.ssh/known_hosts" 2>/dev/null || true
  chown "$APPUSER:$APPUSER" "/home/$APPUSER/.ssh/known_hosts"
  chmod 644 "/home/$APPUSER/.ssh/known_hosts"

  # SSH-Verbindung zu GitHub testen (mit Timeout - verhindert endloses Hängen)
  echo "🔍 Teste SSH-Verbindung zu GitHub (Port 22)..."
  SSH_TEST_RESULT=$(su - "$APPUSER" -s /bin/bash -c \
    "timeout 15 ssh -i '$SSH_KEY_PATH' -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
     -o ConnectTimeout=10 -T git@github.com 2>&1" || true)

  if echo "$SSH_TEST_RESULT" | grep -q "successfully authenticated"; then
    echo "✅ SSH Port 22 erfolgreich verbunden"
  else
    # Port 22 hängt oder blockiert - Fallback auf Port 443
    echo "⚠️  SSH Port 22 nicht erreichbar oder hängt - teste Port 443 (ssh.github.com)..."
    ssh-keyscan -H -p 443 ssh.github.com >> "/home/$APPUSER/.ssh/known_hosts" 2>/dev/null || true

    SSH_TEST_443=$(su - "$APPUSER" -s /bin/bash -c \
      "timeout 15 ssh -i '$SSH_KEY_PATH' -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
       -o ConnectTimeout=10 -p 443 -T git@ssh.github.com 2>&1" || true)

    if echo "$SSH_TEST_443" | grep -q "successfully authenticated"; then
      echo "✅ GitHub SSH über Port 443 erreichbar - erstelle SSH-Config..."
      # SSH-Config erstellen: github.com wird automatisch über Port 443 geleitet
      cat > "/home/$APPUSER/.ssh/config" <<SSHCONFIGEOF
Host github.com
    Hostname ssh.github.com
    Port 443
    User git
    IdentityFile ${SSH_KEY_PATH}
    IdentitiesOnly yes
SSHCONFIGEOF
      chown "$APPUSER:$APPUSER" "/home/$APPUSER/.ssh/config"
      chmod 600 "/home/$APPUSER/.ssh/config"
      echo "✅ SSH-Config für GitHub Port 443 erstellt (/home/$APPUSER/.ssh/config)"
    else
      echo "⚠️  SSH zu GitHub nicht erreichbar (Port 22 + Port 443 fehlgeschlagen)"
      echo "   Mögliche Ursachen:"
      echo "   - SSH-Key wurde noch nicht korrekt zu GitHub hinzugefügt"
      echo "   - Firewall blockiert ausgehende SSH-Verbindungen"
      echo "   Klonen wird trotzdem versucht (mit 30s Timeout)..."
    fi
  fi
else
  echo "⏭️  GitHub nicht genutzt - überspringe GitHub-Setup"
fi

# -------------------------------------------------------------------
# PostgreSQL / MySQL Installation (lokal)
# -------------------------------------------------------------------
cd /tmp

if [ "${DBTYPE}" != "sqlite" ] && [ "${DBMODE:-}" = "1" ]; then
  echo "🗄️  Installiere lokale ${DBTYPE^^} Datenbank..."
  
  if [ "$DBTYPE" = "postgresql" ]; then
    apt install -y $DB_PACKAGE_LOCAL
    systemctl enable --now postgresql
    
    # Warten bis PostgreSQL läuft
    for i in {1..10}; do
      if systemctl is-active --quiet postgresql; then
        break
      fi
      echo "Warte auf PostgreSQL..."
      sleep 2
    done
    
    echo "🔐 Erstelle PostgreSQL Benutzer und Datenbank..."
    su -s /bin/bash postgres <<PGEOF
psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DBNAME'" | grep -q 1 || \
  psql -c "CREATE DATABASE \"$DBNAME\";"

psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DBUSER'" | grep -q 1 || \
  psql -c "CREATE USER \"$DBUSER\" WITH ENCRYPTED PASSWORD '$DBPASS';"

psql -c "GRANT ALL PRIVILEGES ON DATABASE \"$DBNAME\" TO \"$DBUSER\";"

# PostgreSQL 15+ compatibility
psql -d "$DBNAME" -c "GRANT ALL ON SCHEMA public TO \"$DBUSER\";" 2>/dev/null || true
PGEOF
    
  elif [ "$DBTYPE" = "mysql" ]; then
    apt install -y $DB_PACKAGE_LOCAL
    systemctl enable --now mariadb
    
    # Warten bis MariaDB läuft
    for i in {1..10}; do
      if systemctl is-active --quiet mariadb; then
        break
      fi
      echo "Warte auf MariaDB..."
      sleep 2
    done
    
    echo "🔐 Erstelle MySQL/MariaDB Benutzer und Datenbank..."
    mysql -u root <<SQL
CREATE DATABASE IF NOT EXISTS \`$DBNAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$DBUSER'@'localhost' IDENTIFIED BY '$DBPASS';
GRANT ALL PRIVILEGES ON \`$DBNAME\`.* TO '$DBUSER'@'localhost';
FLUSH PRIVILEGES;
SQL
  fi
elif [ "${DBTYPE}" != "sqlite" ] && [ "${DBMODE:-}" = "2" ]; then
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
  
  # Git clone als APPUSER (mit ConnectTimeout um endloses Hängen bei SSH-Problemen zu verhindern)
  su - "$APPUSER" -s /bin/bash -c "GIT_SSH_COMMAND='ssh -i ${SSH_KEY_PATH} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new ${GITHUB_SSH_OPTS}' git clone '$GITHUB_REPO_URL' '$APPDIR'"
  
  echo "✅ Repository geklont nach $APPDIR"

  # Django-Modul automatisch erkennen (Verzeichnis das wsgi.py enthält)
  DJANGO_MODULE=$(find "$APPDIR" -maxdepth 2 -name "wsgi.py" ! -path "*/.venv/*" 2>/dev/null | head -1 | xargs -I{} dirname {} | xargs -I{} basename {} 2>/dev/null)
  if [ -z "$DJANGO_MODULE" ]; then
    echo "⚠️  WARNUNG: wsgi.py nicht gefunden, verwende 'core' als Standard"
    DJANGO_MODULE="core"
  fi
  echo "📌 Django-Modul erkannt: $DJANGO_MODULE"

  # Admin-URL auf /djadmin/ setzen (beide Schreibweisen: einfache und doppelte Anführungszeichen)
  URLS_FILE="$APPDIR/$DJANGO_MODULE/urls.py"
  if [ -f "$URLS_FILE" ]; then
    sed -i "s|path('admin/', admin.site.urls)|path('djadmin/', admin.site.urls)|g" "$URLS_FILE"
    sed -i 's|path("admin/", admin.site.urls)|path("djadmin/", admin.site.urls)|g' "$URLS_FILE"
    echo "✅ Admin-URL auf /djadmin/ gesetzt in $URLS_FILE"
  fi

  # Virtual Environment erstellen
  echo "🐍 Erstelle Python Virtual Environment..."
  su - "$APPUSER" -s /bin/bash <<EOF
set -e
cd "$APPDIR"
python3 -m venv .venv
source .venv/bin/activate
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
  # NEUES PROJEKT erstellen
  echo "🚀 Django Setup (neues Projekt ohne GitHub)..."
  su - "$APPUSER" -s /bin/bash <<EOF
set -e
cd "$APPDIR"
python3 -m venv .venv
source .venv/bin/activate
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

# Admin-URL auf /djadmin/ umbenennen
sed -i "s|path('admin/', admin.site.urls)|path('djadmin/', admin.site.urls)|g" core/urls.py
EOF
DJANGO_MODULE="core"
fi

# -------------------------------------------------------------------
# .env Datei
# -------------------------------------------------------------------
if [[ "$USE_GITHUB" != "true" ]] || [ ! -f "$APPDIR/.env" ]; then
  echo "🔐 Erstelle .env Datei..."
  # Doppelte Anführungszeichen im Passwort escapen
  DBPASS_ESCAPED="${DBPASS//\"/\\\"}"
  cat > "$APPDIR/.env" <<EOF
PROJECTNAME="$PROJECTNAME"
MODE="$MODE"
DEBUG=$DEBUG_VALUE
SECRET_KEY="$DJKEY"
DB_ENGINE="$DB_ENGINE"
DB_NAME="$DBNAME"
DB_USER="$DBUSER"
DB_PASS="$DBPASS_ESCAPED"
DB_HOST="$DBHOST"
DB_PORT="$DBPORT"
ALLOWED_HOSTS=$ALLOWED_HOSTS
CSRF_TRUSTED_ORIGINS=$CSRF_TRUSTED_ORIGINS_VALUE
EOF

  chown "$APPUSER:$APPUSER" "$APPDIR/.env"
  chmod 600 "$APPDIR/.env"
fi

# -------------------------------------------------------------------
# .gitignore
# -------------------------------------------------------------------
if [[ "$USE_GITHUB" != "true" ]] && [ ! -f "$APPDIR/.gitignore" ]; then
  cat > "$APPDIR/.gitignore" <<EOF
.env
__pycache__/
*.py[cod]
*\$py.class
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
*.log
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

# Datenbank-Konfiguration
db_engine = os.getenv("DB_ENGINE", "django.db.backends.sqlite3")
db_name = os.getenv("DB_NAME", str(BASE_DIR / "db.sqlite3"))

DATABASES = {
    "default": {
        "ENGINE": db_engine,
        "NAME": db_name,
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
TIME_ZONE = "Europe/Luxembourg"
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
    
# Logging-Konfiguration
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'file': {
            'level': 'ERROR',
            'class': 'logging.FileHandler',
            'filename': f'/var/log/{os.getenv("PROJECTNAME", "django")}/django.log',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['file'],
            'level': 'ERROR',
            'propagate': True,
        },
    },
}
EOF

  chown "$APPUSER:$APPUSER" "$APPDIR/core/settings.py"
fi

# -------------------------------------------------------------------
# Log-Verzeichnis (muss vor Migrationen existieren, da settings.py darauf zugreift)
# -------------------------------------------------------------------
mkdir -p "/var/log/${PROJECTNAME}"
chown "$APPUSER:adm" "/var/log/${PROJECTNAME}"
chmod 750 "/var/log/${PROJECTNAME}"

# -------------------------------------------------------------------
# Migrationen + Static Files
# -------------------------------------------------------------------
echo "📊 Führe Migrationen aus..."
su - "$APPUSER" -s /bin/bash -c "cd $APPDIR && source .venv/bin/activate && python manage.py migrate"

echo "📦 Sammle statische Dateien..."
su - "$APPUSER" -s /bin/bash -c "cd $APPDIR && source .venv/bin/activate && python manage.py collectstatic --noinput"

# -------------------------------------------------------------------
# Django Superuser erstellen
# -------------------------------------------------------------------
echo
echo "👑 Django Superuser erstellen (Admin-Login für /djadmin/)"
read -p "Admin-Username [admin]: " DJANGO_ADMIN_USER
DJANGO_ADMIN_USER="${DJANGO_ADMIN_USER:-admin}"

read -p "Admin-Email [admin@localhost]: " DJANGO_ADMIN_EMAIL
DJANGO_ADMIN_EMAIL="${DJANGO_ADMIN_EMAIL:-admin@localhost}"

while true; do
  read -s -p "Admin-Passwort: " DJANGO_ADMIN_PASS; echo
  [ -z "$DJANGO_ADMIN_PASS" ] && echo "❌ Passwort darf nicht leer sein." && continue
  read -s -p "Admin-Passwort bestätigen: " DJANGO_ADMIN_PASS2; echo
  if [ "$DJANGO_ADMIN_PASS" = "$DJANGO_ADMIN_PASS2" ]; then
    break
  else
    echo "❌ Passwörter stimmen nicht überein. Erneut versuchen."
  fi
done

su - "$APPUSER" -s /bin/bash -c "cd $APPDIR && source .venv/bin/activate && \
  DJANGO_SUPERUSER_PASSWORD='$DJANGO_ADMIN_PASS' \
  python manage.py createsuperuser --noinput \
    --username '$DJANGO_ADMIN_USER' \
    --email '$DJANGO_ADMIN_EMAIL'"

echo "✅ Django Superuser '$DJANGO_ADMIN_USER' erstellt"
echo "   Login unter: http://${LOCAL_IP}/djadmin/"

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
ExecStart=$APPDIR/.venv/bin/gunicorn $DJANGO_MODULE.wsgi:application --bind 127.0.0.1:8000 --workers 3 --timeout 120 --access-logfile /var/log/${PROJECTNAME}/access.log --error-logfile /var/log/${PROJECTNAME}/error.log
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$PROJECTNAME"

# Kurz warten und Status prüfen
sleep 2
if ! systemctl is-active --quiet "$PROJECTNAME"; then
  echo "⚠️  WARNUNG: Service konnte nicht gestartet werden!"
  journalctl -u "$PROJECTNAME" -n 20 --no-pager
fi

# -------------------------------------------------------------------
# Log-Rotation
# -------------------------------------------------------------------
cat > /etc/logrotate.d/${PROJECTNAME} <<EOF
/var/log/${PROJECTNAME}/*.log {
  daily
  missingok
  rotate 14
  compress
  delaycompress
  notifempty
  create 640 ${APPUSER} adm
  sharedscripts
  postrotate
    systemctl reload ${PROJECTNAME} > /dev/null 2>&1 || true
  endscript
}
EOF

# -------------------------------------------------------------------
# nginx Konfiguration
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

    # Static Files
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

    # Django App
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
# Sudoers
# -------------------------------------------------------------------
echo "🔐 Konfiguriere sudoers für $APPUSER..."
cat > /etc/sudoers.d/${PROJECTNAME}-service <<EOF
$APPUSER ALL=NOPASSWD: /bin/systemctl restart $PROJECTNAME, /bin/systemctl status $PROJECTNAME, /bin/systemctl reload $PROJECTNAME, /bin/journalctl -u $PROJECTNAME*
EOF
chmod 440 /etc/sudoers.d/${PROJECTNAME}-service

# -------------------------------------------------------------------
# Update-Skript
# -------------------------------------------------------------------
echo "🔄 Erstelle Update-Skript..."
cat > /usr/local/bin/${PROJECTNAME}_update.sh <<UPDATEEOF
#!/bin/bash
set -euo pipefail

APPDIR="$APPDIR"
SERVICE="$PROJECTNAME"
APPUSER="$APPUSER"
SSH_KEY_PATH="$SSH_KEY_PATH"

echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║                  UPDATE START (\$SERVICE)                      ║"
echo "╚═══════════════════════════════════════════════════════════════╝"

cd "\$APPDIR"

# Git Pull (falls Git-Repo vorhanden)
if [ -d "\$APPDIR/.git" ]; then
  echo "📥 Git Pull..."
  su - "\$APPUSER" -s /bin/bash -c "cd \$APPDIR && GIT_SSH_COMMAND='ssh -i \$SSH_KEY_PATH -o IdentitiesOnly=yes -o ConnectTimeout=30' git pull"
else
  echo "⏭️  Kein Git-Repository gefunden (überspringe git pull)"
fi

# Requirements installieren
if [ -f "\$APPDIR/requirements.txt" ]; then
  echo "📦 Installiere Requirements..."
  su - "\$APPUSER" -s /bin/bash -c "cd \$APPDIR && source .venv/bin/activate && pip install -r requirements.txt"
fi

# Migrationen prüfen und ausführen
echo "🔍 Prüfe auf neue Migrationen..."
su - "\$APPUSER" -s /bin/bash -c "cd \$APPDIR && source .venv/bin/activate && python manage.py makemigrations --check --dry-run" || {
  echo "⚠️  Neue Migrationen gefunden. Führe makemigrations aus..."
  su - "\$APPUSER" -s /bin/bash -c "cd \$APPDIR && source .venv/bin/activate && python manage.py makemigrations"
}

echo "📊 Führe Migrationen aus..."
su - "\$APPUSER" -s /bin/bash -c "cd \$APPDIR && source .venv/bin/activate && python manage.py migrate"

# Statische Dateien sammeln
echo "📦 Sammle statische Dateien..."
su - "\$APPUSER" -s /bin/bash -c "cd \$APPDIR && source .venv/bin/activate && python manage.py collectstatic --noinput"

# Service neustarten
echo "🔄 Neustart Service..."
sudo systemctl restart "\$SERVICE"

echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║                     UPDATE DONE ✅                            ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
UPDATEEOF

chmod 755 /usr/local/bin/${PROJECTNAME}_update.sh

# -------------------------------------------------------------------
# Backup-Skript
# -------------------------------------------------------------------
BACKUP_DIR="/var/backups/${PROJECTNAME}"
mkdir -p "$BACKUP_DIR"
chown "$APPUSER:$APPUSER" "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

cat > /usr/local/bin/${PROJECTNAME}_backup.sh <<BACKUPEOF
#!/bin/bash
set -euo pipefail
PROJECT="$PROJECTNAME"
APPUSER="$APPUSER"
APPDIR="$APPDIR"
DBTYPE="$DBTYPE"
DBNAME="$DBNAME"
BACKUP_DIR="$BACKUP_DIR"
TIMESTAMP=\$(date +%Y%m%d_%H%M%S)

echo "📦 Backup startet für \$PROJECT..."

# DB-Dump
if [ "\$DBTYPE" = "postgresql" ]; then
  su -s /bin/sh postgres -c "pg_dump -Fc \$DBNAME" > "\$BACKUP_DIR/db_\${TIMESTAMP}.dump" 2>/dev/null || echo "⚠️ DB-Dump fehlgeschlagen"
elif [ "\$DBTYPE" = "mysql" ]; then
  mysqldump -u root "\$DBNAME" > "\$BACKUP_DIR/db_\${TIMESTAMP}.sql" 2>/dev/null || echo "⚠️ DB-Dump fehlgeschlagen"
elif [ "\$DBTYPE" = "sqlite" ] && [ -f "\$APPDIR/db.sqlite3" ]; then
  cp "\$APPDIR/db.sqlite3" "\$BACKUP_DIR/db_\${TIMESTAMP}.sqlite3"
fi

# .env sichern
[ -f "\$APPDIR/.env" ] && cp "\$APPDIR/.env" "\$BACKUP_DIR/env_\${TIMESTAMP}.backup" && chmod 600 "\$BACKUP_DIR/env_\${TIMESTAMP}.backup"

# Projekt sichern
tar --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' --exclude='*.log' \\
    -czf "\$BACKUP_DIR/project_\${TIMESTAMP}.tar.gz" -C /srv "\$PROJECT" 2>/dev/null || echo "⚠️ Projekt-Backup fehlgeschlagen"

# Alte Backups bereinigen (>14 Tage)
find "\$BACKUP_DIR" -type f -mtime +14 -delete 2>/dev/null

echo "✅ Backup fertig in \$BACKUP_DIR"
BACKUPEOF

chmod 755 /usr/local/bin/${PROJECTNAME}_backup.sh
echo "💾 Backup-Skript erstellt: /usr/local/bin/${PROJECTNAME}_backup.sh"

# -------------------------------------------------------------------
# Health-Check (nur bei neuem Projekt)
# -------------------------------------------------------------------
if [[ "$USE_GITHUB" != "true" ]]; then
  cat > "$APPDIR/core/views.py" <<'HEALTHEOF'
from django.http import JsonResponse
from django.db import connection
import os

def health_check(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db_ok = True
    except Exception as e:
        db_ok = False
    
    return JsonResponse({
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "error",
        "mode": os.getenv("MODE", "unknown")
    })
HEALTHEOF
  chown "$APPUSER:$APPUSER" "$APPDIR/core/views.py"

  # URL Pattern hinzufügen
  if [ -f "$APPDIR/core/urls.py" ]; then
    if ! grep -q "health_check" "$APPDIR/core/urls.py"; then
      su - "$APPUSER" -s /bin/bash <<URLEOF
cd "$APPDIR"
python3 << 'PYEOF'
import re

with open("core/urls.py", "r") as f:
    content = f.read()

# Import hinzufügen
if "from . import views" not in content:
    content = re.sub(
        r"(from django\.urls import.*)",
        r"\1\nfrom . import views",
        content
    )

# URL Pattern hinzufügen
if "health/" not in content:
    content = re.sub(
        r"(urlpatterns = \[)",
        r"\1\n    path('health/', views.health_check),",
        content
    )

with open("core/urls.py", "w") as f:
    f.write(content)
PYEOF
URLEOF
    fi
  fi
  echo "✅ Health-Check Endpoint erstellt: /health/"
fi

# -------------------------------------------------------------------
# MOTD
# -------------------------------------------------------------------
cat > /etc/profile.d/${PROJECTNAME}_motd.sh <<MOTDEOF
# Nur bei interaktiven Shells
case "\\\$-" in
  *i*) ;;
  *) return ;;
esac

# Nur einmal pro Session
if [ -n "\\\${MOTD_${PROJECTNAME}_SHOWN:-}" ]; then
  return
fi
export MOTD_${PROJECTNAME}_SHOWN=1

echo
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║              $PROJECTNAME - Serverübersicht                   ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo "📁 Projektverzeichnis: $APPDIR"
echo "👤 App-Benutzer:       $APPUSER"
echo "🌐 Modus:              $MODE (DEBUG=$DEBUG_VALUE)"
echo "🗄️  Datenbank:         ${DBTYPE^^}"
MOTDEOF

if [ "$DBTYPE" != "sqlite" ]; then
  cat >> /etc/profile.d/${PROJECTNAME}_motd.sh <<MOTDEOF
echo "   DB-Engine:          $DB_ENGINE"
echo "   DB-Name:            $DBNAME"
echo "   DB-Host:            $DBHOST"
echo "   DB-Port:            $DBPORT"
MOTDEOF
fi

if [[ "$USE_GITHUB" == "true" ]]; then
  cat >> /etc/profile.d/${PROJECTNAME}_motd.sh <<MOTDEOF
echo "📦 GitHub Repo:       $GITHUB_REPO_URL"
MOTDEOF
fi

cat >> /etc/profile.d/${PROJECTNAME}_motd.sh <<MOTDEOF
echo
echo "🔐 SSH-ZUGRIFF:"
echo "   Benutzer:     $APPUSER"
echo "   IP-Adresse:   $LOCAL_IP"
echo "   Hostname:     $HOSTNAME_FQDN"
echo "   Private Key:  $SSH_KEY_PATH"
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📥 Private Key für WinSCP/PuTTY herunterladen:"
echo "   scp root@${LOCAL_IP}:${SSH_KEY_PATH} ."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "📦 Update:     ${PROJECTNAME}_update.sh"
echo "💾 Backup:     ${PROJECTNAME}_backup.sh"
echo
echo "📊 Status:     systemctl status $PROJECTNAME"
echo "📋 Logs:       journalctl -u $PROJECTNAME -f"
echo
echo "🌐 Django Admin: http://$LOCAL_IP/djadmin/"
echo
echo "👑 Superuser:  sudo -u $APPUSER bash -c 'cd $APPDIR && source .venv/bin/activate && python manage.py createsuperuser'"
echo "═══════════════════════════════════════════════════════════════"
echo
MOTDEOF
chmod 644 /etc/profile.d/${PROJECTNAME}_motd.sh

# -------------------------------------------------------------------
# Abschluss
# -------------------------------------------------------------------
echo
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║                      INSTALLATION FERTIG ✅                   ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo "📁 Projektverzeichnis: $APPDIR"
echo "👤 App-Benutzer:       $APPUSER"
echo "🌐 Modus:              $MODE (DEBUG=$DEBUG_VALUE)"
echo "🗄️  Datenbank:         ${DBTYPE^^}"
if [ "$DBTYPE" != "sqlite" ]; then
  echo "   DB-Engine:          $DB_ENGINE"
  echo "   DB-Name:            $DBNAME"
  echo "   DB-Host:            $DBHOST"
  echo "   DB-Port:            $DBPORT"
fi
echo
echo "🔐 SSH-ZUGRIFF:"
echo "   Benutzer:     $APPUSER"
echo "   IP-Adresse:   $LOCAL_IP"
echo "   Hostname:     $HOSTNAME_FQDN"
echo "   Private Key:  $SSH_KEY_PATH"
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📥 Private Key für WinSCP/PuTTY herunterladen:"
echo "   scp root@${LOCAL_IP}:${SSH_KEY_PATH} ."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "📦 Update:     ${PROJECTNAME}_update.sh"
echo "💾 Backup:     ${PROJECTNAME}_backup.sh"
echo
echo "✅ FERTIG! Viel Erfolg mit deinem Django-Projekt! 🚀"
echo "═══════════════════════════════════════════════════════════════"
