#!/bin/bash
set -euo pipefail

# ===================================================================
# DjangoMultiDeploy - Multi-Server Django Installer
# Mehrere Django-Projekte auf einem Server, jedes mit eigenem Port,
# Gunicorn, nginx, systemd, PostgreSQL/MySQL/SQLite, Backup & MOTD
# Zoraxy Reverse Proxy ready | LXC/Container ready
# Version: 3.0
# ===================================================================

# -------------------------------------------------------------------
# NONINTERACTIVE-Modus: alle Eingabevariablen als Env-Vars übergeben
# Wird vom DjangoMultiDeploy Manager (Web-Interface) genutzt
# Beispiel: export NONINTERACTIVE=true PROJECTNAME=myapp ...
# -------------------------------------------------------------------
NONINTERACTIVE="${NONINTERACTIVE:-false}"
# _read: überspringt read-Prompts wenn NONINTERACTIVE=true
_read() { [ "$NONINTERACTIVE" = "true" ] && return 0; read "$@"; }

# ===================================================================
# Checkpoint / Resume System
# ===================================================================
_RESUME=false

# Prüft ob ein Schritt bereits abgeschlossen wurde
is_done() {
  grep -q "^STEP_${1}=\"done\"" "${STATE_FILE:-/dev/null}" 2>/dev/null || return 1
}

# Markiert einen Schritt als abgeschlossen und speichert in State-Datei
mark_done() {
  sed -i "/^STEP_${1}=/d" "$STATE_FILE" 2>/dev/null || true
  sed -i "/^LAST_STEP=/d" "$STATE_FILE" 2>/dev/null || true
  printf 'STEP_%s="done"\n' "${1}" >> "$STATE_FILE"
  printf 'LAST_STEP="%s"\n' "${1}" >> "$STATE_FILE"
}

# Unterbrochene Installationen suchen
mapfile -t _STATES < <(compgen -G "/tmp/django_install_*.state" 2>/dev/null || true)
if [ "${#_STATES[@]}" -gt 0 ] && [ -f "${_STATES[0]}" ]; then
  echo
  echo "┌──────────────────────────────────────────────────────────────────┐"
  echo "│       ⚠️  Unterbrochene Installation(en) gefunden               │"
  echo "└──────────────────────────────────────────────────────────────────┘"
  _IDX=1
  for _sf in "${_STATES[@]}"; do
    _proj=$(grep "^PROJECTNAME=" "$_sf" 2>/dev/null | cut -d= -f2 | tr -d '"' || echo "?")
    _lstep=$(grep "^LAST_STEP=" "$_sf" 2>/dev/null | cut -d= -f2 | tr -d '"' || echo "noch keiner")
    echo "  ${_IDX}) Projekt: ${_proj}  |  Letzter Schritt: ${_lstep}"
    _IDX=$((_IDX + 1))
  done
  echo
  _RC="${_RC:-}"
  [ "$NONINTERACTIVE" != "true" ] && read -p "Installation fortsetzen? [J/n]: " _RC
  _RC="${_RC:-J}"
  if [[ "$_RC" =~ ^[Jj]$ ]]; then
    if [ "${#_STATES[@]}" -gt 1 ]; then
      _RN="${_RN:-}"
      [ "$NONINTERACTIVE" != "true" ] && read -p "Welche Installation fortsetzen? (Nummer): " _RN
      _RESUME_FILE="${_STATES[$((_RN - 1))]}"
    else
      _RESUME_FILE="${_STATES[0]}"
    fi
    # shellcheck disable=SC1090
    source "$_RESUME_FILE"
    STATE_FILE="$_RESUME_FILE"
    _RESUME=true
    echo "✅ Fortsetze Installation: $PROJECTNAME"
    echo "   Abgeschlossene Schritte werden übersprungen..."
    echo
  fi
fi

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
# System-Voraussetzungen prüfen (vor jeder Installation)
# -------------------------------------------------------------------
echo
echo "🔍 Prüfe System-Voraussetzungen..."
_PRE_OK=true

# --- /tmp beschreibbar? ---
if ! touch /tmp/.django_write_test 2>/dev/null; then
  echo "⚠️  /tmp ist nicht beschreibbar (Read-only) — versuche Remount..."
  if mount -o remount,rw /tmp 2>/dev/null; then
    echo "   ✅ /tmp erfolgreich als read-write remountet"
  else
    echo "   /tmp remount fehlgeschlagen — versuche tmpfs neu einzuhängen..."
    if mount -t tmpfs -o size=1g,mode=1777 tmpfs /tmp 2>/dev/null; then
      echo "   ✅ Neues tmpfs auf /tmp eingehängt"
    else
      echo "   ❌ /tmp konnte nicht beschreibbar gemacht werden!"
      echo "      Mögliche Ursachen:"
      echo "      • Root-Dateisystem read-only (Disk-Fehler auf Proxmox-Host)"
      echo "      • Proxmox-Host Speicher voll → Container neu starten"
      echo "      • dmesg | grep -i 'error\|readonly' auf dem HOST prüfen"
      _PRE_OK=false
    fi
  fi
else
  rm -f /tmp/.django_write_test
  echo "  ✅ /tmp beschreibbar"
fi

# --- Root-Dateisystem schreibbar? ---
if ! touch /root/.django_write_test 2>/dev/null; then
  echo "  ❌ Root-Dateisystem (/) ist read-only!"
  echo "     → Container neu starten und danach: dmesg | grep -i readonly"
  echo "     → Auf Proxmox-Host: Disk-Fehler im Proxmox-Log prüfen"
  _PRE_OK=false
else
  rm -f /root/.django_write_test
  echo "  ✅ Root-Dateisystem beschreibbar"
fi

# --- Freier Speicher auf / (mind. 2 GB) ---
_FREE_ROOT_MB=$(df -m / 2>/dev/null | awk 'NR==2{print $4}')
if [ -n "$_FREE_ROOT_MB" ]; then
  if [ "$_FREE_ROOT_MB" -lt 2048 ]; then
    echo "  ❌ Zu wenig Speicher auf /: ${_FREE_ROOT_MB} MB frei (Minimum: 2048 MB)"
    echo "     → df -h /  →  ggf. alte Pakete mit: apt autoremove && apt clean"
    _PRE_OK=false
  elif [ "$_FREE_ROOT_MB" -lt 4096 ]; then
    echo "  ⚠️  Wenig Speicher auf /: ${_FREE_ROOT_MB} MB frei (Empfehlung: ≥ 4096 MB)"
  else
    echo "  ✅ Freier Speicher auf /: ${_FREE_ROOT_MB} MB"
  fi
fi

# --- Freier Speicher auf /tmp (mind. 512 MB) ---
_FREE_TMP_MB=$(df -m /tmp 2>/dev/null | awk 'NR==2{print $4}')
if [ -n "$_FREE_TMP_MB" ] && [ "$_FREE_TMP_MB" -lt 512 ]; then
  echo "  ❌ Zu wenig Speicher auf /tmp: ${_FREE_TMP_MB} MB (Minimum: 512 MB)"
  echo "     → mount -t tmpfs -o size=1g,mode=1777 tmpfs /tmp"
  _PRE_OK=false
else
  echo "  ✅ Freier Speicher auf /tmp: ${_FREE_TMP_MB:-?} MB"
fi

# --- DNS-Auflösung ---
if ! getent hosts pypi.org >/dev/null 2>&1; then
  echo "  ❌ DNS-Auflösung fehlgeschlagen (pypi.org nicht erreichbar)"
  echo "     → cat /etc/resolv.conf  →  ggf. nameserver 1.1.1.1 eintragen"
  _PRE_OK=false
else
  echo "  ✅ DNS-Auflösung funktioniert"
fi

# --- HTTPS-Verbindung zu pypi.org ---
if command -v curl >/dev/null 2>&1; then
  if ! curl -fsSL --max-time 15 https://pypi.org/simple/ -o /dev/null 2>/dev/null; then
    echo "  ❌ HTTPS-Verbindung zu pypi.org fehlgeschlagen!"
    echo "     → Firewall / Netzwerk prüfen: curl -v https://pypi.org"
    echo "     → Uhrzeit korrekt? (SSL-Fehler bei falscher Systemzeit)"
    echo "        Aktuelle Zeit: $(date)  → Prüfen: timedatectl"
    _PRE_OK=false
  else
    echo "  ✅ HTTPS-Verbindung zu pypi.org erfolgreich"
  fi
fi

# --- Systemzeit (SSL-Zertifikate versagen bei falscher Uhrzeit) ---
_NOW_TS=$(date +%s)
if [ "$_NOW_TS" -lt 1700000000 ]; then
  echo "  ⚠️  Systemzeit scheint falsch: $(date)"
  echo "     → timedatectl set-ntp true  oder  date -s 'YYYY-MM-DD HH:MM:SS'"
fi

# --- Abbruch wenn kritische Checks fehlgeschlagen ---
if [ "$_PRE_OK" = "false" ]; then
  echo
  echo "❌ ABBRUCH: Kritische Voraussetzungen nicht erfüllt."
  echo "   Probleme beheben und Skript erneut starten."
  echo "   (Bereits abgeschlossene Schritte werden beim Neustart übersprungen)"
  exit 1
fi

echo "✅ Alle Voraussetzungen erfüllt — Installation wird gestartet"
echo

# -------------------------------------------------------------------
# Installations-Modus auswählen
# -------------------------------------------------------------------
if [ "${NONINTERACTIVE:-false}" != "true" ]; then
  echo "╔═══════════════════════════════════════════════════════════════╗"
  echo "║          DjangoMultiDeploy — Was installieren?               ║"
  echo "╠═══════════════════════════════════════════════════════════════╣"
  echo "║  1) Django-Projekt           (neue Django-Webanwendung)      ║"
  echo "║  2) DjangoMultiDeploy Manager (Web-Interface Port 8888)      ║"
  echo "║  3) Beides                                                   ║"
  echo "╚═══════════════════════════════════════════════════════════════╝"
  _read -p "Auswahl (1/2/3) [3]: " _INSTALL_SEL
fi
_INSTALL_SEL="${_INSTALL_SEL:-3}"
case "$_INSTALL_SEL" in
  1) INSTALL_PROJECT=true;  INSTALL_MANAGER=false ;;
  2) INSTALL_PROJECT=false; INSTALL_MANAGER=true  ;;
  3) INSTALL_PROJECT=true;  INSTALL_MANAGER=true  ;;
  *) echo "❌ FEHLER: Ungültige Auswahl (1/2/3)"; exit 1 ;;
esac

# Defaults für nicht-benötigte Variablen setzen
if [ "$INSTALL_PROJECT" = "false" ]; then
  PROJECTNAME="${PROJECTNAME:-}"
  APPUSER="${APPUSER:-}"
fi

echo "✅ Modus: $([ "$INSTALL_PROJECT" = "true" ] && echo "Django-Projekt" || true)$([ "$INSTALL_PROJECT" = "true" ] && [ "$INSTALL_MANAGER" = "true" ] && echo " + " || true)$([ "$INSTALL_MANAGER" = "true" ] && echo "Manager" || true)"
echo

# ===================================================================
# PROJEKT-INSTALLATION (nur wenn INSTALL_PROJECT=true)
# ===================================================================
if [ "${INSTALL_PROJECT:-true}" = "true" ]; then

# -------------------------------------------------------------------
# Projektname -> /srv/<name> (mit Validierung!)
# -------------------------------------------------------------------
if [[ "$_RESUME" != "true" ]]; then
  _read -p "Projektname (Ordner unter /srv, z.B. webapp): " PROJECTNAME
  [ -z "${PROJECTNAME:-}" ] && echo "❌ FEHLER: Projektname leer." && exit 1

  # Validierung: nur alphanumerisch, _, - und 3-50 Zeichen
  if [[ ! "$PROJECTNAME" =~ ^[a-zA-Z0-9_-]{3,50}$ ]]; then
    echo "❌ FEHLER: Ungültiger Projektname!"
    echo "   Erlaubt: a-z, A-Z, 0-9, _, - (3-50 Zeichen)"
    exit 1
  fi

  APPDIR="/srv/$PROJECTNAME"

  # State-Datei für dieses Projekt initialisieren
  STATE_FILE="/tmp/django_install_${PROJECTNAME}.state"
  : > "$STATE_FILE"
  chmod 600 "$STATE_FILE"
  printf 'PROJECTNAME="%s"\n' "$PROJECTNAME" >> "$STATE_FILE"
  printf 'APPDIR="%s"\n' "$APPDIR" >> "$STATE_FILE"
  printf 'STATE_FILE="%s"\n' "$STATE_FILE" >> "$STATE_FILE"

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
fi

# -------------------------------------------------------------------
# Eingaben (bei Resume aus State-Datei geladen, sonst neu abfragen)
# -------------------------------------------------------------------
if ! is_done "input_saved"; then

# -------------------------------------------------------------------
# GitHub Repository Option
# -------------------------------------------------------------------
echo
echo "GitHub Repository (optional):"
echo "  • Öffentliches Repo: https://github.com/user/repo.git"
echo "  • Privates Repo:     git@github.com:user/repo.git"
echo "  • Leer lassen für neues Django-Projekt (ohne GitHub)"
_read -p "GitHub URL (leer für neues Projekt): " GITHUB_REPO_URL
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
_read -p "Auswahl (1/2) [1]: " MODESEL
MODESEL="${MODESEL:-1}"
[[ "$MODESEL" != "1" && "$MODESEL" != "2" ]] && echo "❌ FEHLER: Bitte 1 oder 2." && exit 1
if [ "$MODESEL" = "1" ]; then MODE="dev"; DEBUG_VALUE="True"; else MODE="prod"; DEBUG_VALUE="False"; fi

# -------------------------------------------------------------------
# Gunicorn Port (automatisch freien Port vorschlagen)
# -------------------------------------------------------------------
_DEFAULT_PORT=8000
while ss -tlnp 2>/dev/null | grep -q ":${_DEFAULT_PORT} "; do
  _DEFAULT_PORT=$((_DEFAULT_PORT + 1))
done
echo
echo "Gunicorn-Port (jede Django-Instanz braucht einen eigenen Port):"
echo "  Aktuell laufende Dienste auf diesem Server:"
ss -tlnp 2>/dev/null | awk '/LISTEN/{print "  ",$0}' | grep -E ':(80[0-9][0-9]|9[0-9][0-9][0-9]) ' || echo "  (keine auf 8000-9999)"
_read -p "Gunicorn-Port [${_DEFAULT_PORT}]: " GUNICORN_PORT
GUNICORN_PORT="${GUNICORN_PORT:-$_DEFAULT_PORT}"
if [[ ! "$GUNICORN_PORT" =~ ^[0-9]+$ ]] || [ "$GUNICORN_PORT" -lt 1024 ] || [ "$GUNICORN_PORT" -gt 65535 ]; then
  echo "❌ FEHLER: Ungültiger Port! Erlaubt: 1024-65535"
  exit 1
fi
if ss -tlnp 2>/dev/null | grep -q ":${GUNICORN_PORT} "; then
  echo "⚠️  WARNUNG: Port ${GUNICORN_PORT} ist bereits belegt!"
  _read -p "Trotzdem verwenden? (j/N): " _PC
  [[ ! "$_PC" =~ ^[Jj]$ ]] && exit 1
fi
echo "✅ Gunicorn-Port: $GUNICORN_PORT"

# -------------------------------------------------------------------
# Hosts
# -------------------------------------------------------------------
DEFAULT_ALLOWED_HOSTS="${ALL_LOCAL_IPS},127.0.0.1,localhost,${HOSTNAME_FQDN}"
[ "$MODE" = "prod" ] && echo "PROD: DNS-Namen eintragen (z.B. app.intern.lan)"
_read -p "ALLOWED_HOSTS (Komma-separiert) [${DEFAULT_ALLOWED_HOSTS}]: " ALLOWED_HOSTS
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
_read -p "Auswahl (1/2/3): " DBTYPE_SEL
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
    _read -p "Trotzdem fortfahren? (j/N): " CONFIRM
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
  _read -p "Auswahl (1/2): " DBMODE
  [[ "$DBMODE" != "1" && "$DBMODE" != "2" ]] && echo "❌ FEHLER: Bitte 1 oder 2." && exit 1

  # -------------------------------------------------------------------
  # DB-Zugangsdaten
  # -------------------------------------------------------------------
  # ℹ️  Tipp für Multi-Server-Betrieb:
  # Den gleichen PostgreSQL-Server (gleicher Host, gleicher Port 5432)
  # können ALLE Django-Projekte auf diesem Server nutzen!
  # Pro Projekt nur einen eigenen DB-Namen und DB-User vergeben.
  # Beispiel: webapp  → DB: webapp  / User: webapp_user  (Port 5432 ✅)
  #           shopapp → DB: shopapp / User: shopapp_user  (Port 5432 ✅)
  if [ "$DBTYPE" = "postgresql" ]; then
    _EXISTING_DBS=""
    if command -v psql >/dev/null 2>&1; then
      _EXISTING_DBS=$(su -s /bin/bash postgres -c "psql -tAc \"SELECT datname FROM pg_database WHERE datistemplate=false AND datname NOT IN ('postgres')\" 2>/dev/null" 2>/dev/null || true)
    fi
    if [ -n "$_EXISTING_DBS" ]; then
      echo
      echo "ℹ️  Bereits vorhandene PostgreSQL-Datenbanken auf diesem Server:"
      echo "$_EXISTING_DBS" | while read -r _db; do echo "     • $_db"; done
      echo "   → Du kannst denselben PostgreSQL-Server (Port 5432) weiterverwenden!"
    fi
  fi

  DBNAME_DEFAULT="${PROJECTNAME//-/_}"
  DBUSER_DEFAULT="${PROJECTNAME//-/_}_user"

  _read -p "DB Name [${DBNAME_DEFAULT}]: " TMP_DBNAME
  DBNAME="${TMP_DBNAME:-$DBNAME_DEFAULT}"
  DBNAME="${DBNAME//-/_}"

  _read -p "DB User [${DBUSER_DEFAULT}]: " TMP_DBUSER
  DBUSER="${TMP_DBUSER:-$DBUSER_DEFAULT}"
  DBUSER="${DBUSER//-/_}"

  _read -p "DB Host [localhost]: " DBHOST
  DBHOST="${DBHOST:-localhost}"

  _read -p "DB Port [${DBTYPE}]: " DBPORT
  if [ "$DBTYPE" = "postgresql" ]; then
    DBPORT="${DBPORT:-5432}"
  else
    DBPORT="${DBPORT:-3306}"
  fi

  _read -s -p "DB Passwort für ${DBUSER}: " DBPASS; echo
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
_read -p "Linux-User für App (wird erstellt, z.B. webuser): " APPUSER
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
_read -s -p "Django SECRET_KEY (leer = auto): " DJKEY; echo
[ -z "${DJKEY:-}" ] && DJKEY="$(openssl rand -hex 32)"

# -------------------------------------------------------------------
# SSH-Key für App-User erstellen
# -------------------------------------------------------------------
echo
echo "🔐 SSH-Key für SSH-Zugriff (WinSCP/PuTTY)"
echo "   Dieser Key ermöglicht SSH-Login als $APPUSER"
echo
_read -p "SSH-Key Passphrase (leer für kein Passwort): " SSH_KEY_PASSPHRASE

SSH_KEY_PATH="/home/${APPUSER}/.ssh/id_ed25519"

# -------------------------------------------------------------------
# Gunicorn Worker (automatisch aus CPU-Kernen berechnen)
# -------------------------------------------------------------------
_CPU_COUNT=$(nproc 2>/dev/null || echo 2)
_DEFAULT_WORKERS=$(( _CPU_COUNT * 2 + 1 ))
echo
echo "Gunicorn Worker (Empfehlung: 2 × CPU-Kerne + 1):"
echo "  Dieser Server hat ${_CPU_COUNT} CPU-Kern(e) → Empfehlung: ${_DEFAULT_WORKERS} Worker"
_read -p "Anzahl Worker [${_DEFAULT_WORKERS}]: " GUNICORN_WORKERS
GUNICORN_WORKERS="${GUNICORN_WORKERS:-$_DEFAULT_WORKERS}"
if [[ ! "$GUNICORN_WORKERS" =~ ^[0-9]+$ ]] || [ "$GUNICORN_WORKERS" -lt 1 ] || [ "$GUNICORN_WORKERS" -gt 32 ]; then
  echo "❌ FEHLER: Ungültige Worker-Anzahl (1-32)!"
  exit 1
fi
echo "✅ Gunicorn Worker: $GUNICORN_WORKERS"

# -------------------------------------------------------------------
# Sprache & Zeitzone
# -------------------------------------------------------------------
echo
echo "Sprache & Zeitzone für Django:"
echo "  Beispiele: de-de / Europe/Berlin | en-us / Europe/London | fr-fr / Europe/Paris"
_read -p "Sprachcode [de-de]: " LANGUAGE_CODE
LANGUAGE_CODE="${LANGUAGE_CODE:-de-de}"
_read -p "Zeitzone   [Europe/Berlin]: " TIME_ZONE
TIME_ZONE="${TIME_ZONE:-Europe/Berlin}"
echo "✅ Sprache: $LANGUAGE_CODE | Zeitzone: $TIME_ZONE"

# -------------------------------------------------------------------
# E-Mail / SMTP Konfiguration (optional)
# -------------------------------------------------------------------
echo
echo "E-Mail / SMTP Konfiguration (optional — leer lassen zum Überspringen):"
_read -p "SMTP Host (z.B. smtp.gmail.com) [leer = deaktiviert]: " EMAIL_HOST
if [ -n "${EMAIL_HOST:-}" ]; then
  _read -p "SMTP Port [587]: " EMAIL_PORT
  EMAIL_PORT="${EMAIL_PORT:-587}"
  _read -p "SMTP User (E-Mail-Adresse): " EMAIL_HOST_USER
  _read -s -p "SMTP Passwort: " EMAIL_HOST_PASSWORD; echo
  _read -p "TLS verwenden? [J/n]: " _TLS_CHOICE
  [[ "${_TLS_CHOICE:-J}" =~ ^[Jj]$ ]] && EMAIL_USE_TLS="True" || EMAIL_USE_TLS="False"
  _read -p "Absender-Adresse [${EMAIL_HOST_USER:-noreply@localhost}]: " DEFAULT_FROM_EMAIL
  DEFAULT_FROM_EMAIL="${DEFAULT_FROM_EMAIL:-${EMAIL_HOST_USER:-noreply@localhost}}"
  echo "✅ E-Mail konfiguriert: ${EMAIL_HOST}:${EMAIL_PORT} (TLS: ${EMAIL_USE_TLS})"
else
  EMAIL_HOST=""; EMAIL_PORT=""; EMAIL_HOST_USER=""
  EMAIL_HOST_PASSWORD=""; EMAIL_USE_TLS=""; DEFAULT_FROM_EMAIL=""
  echo "⏭️  E-Mail übersprungen (console backend aktiv)"
fi

# -------------------------------------------------------------------
# Backup-Cron Uhrzeit
# -------------------------------------------------------------------
echo
_read -p "Automatisches tägliches Backup um (HH:MM) [02:00]: " _BACKUP_TIME
_BACKUP_TIME="${_BACKUP_TIME:-02:00}"
BACKUP_CRON_HOUR=$(echo "$_BACKUP_TIME" | cut -d: -f1 | sed 's/^0*//' )
BACKUP_CRON_MIN=$(echo "$_BACKUP_TIME"  | cut -d: -f2 | sed 's/^0*//' )
[[ ! "$BACKUP_CRON_HOUR" =~ ^[0-9]+$ ]] || [ "${BACKUP_CRON_HOUR:-99}" -gt 23 ] && BACKUP_CRON_HOUR=2
[[ ! "$BACKUP_CRON_MIN"  =~ ^[0-9]+$ ]] || [ "${BACKUP_CRON_MIN:-99}"  -gt 59 ] && BACKUP_CRON_MIN=0
BACKUP_CRON_HOUR="${BACKUP_CRON_HOUR:-2}"
BACKUP_CRON_MIN="${BACKUP_CRON_MIN:-0}"
echo "✅ Backup täglich um $(printf '%02d:%02d' "$BACKUP_CRON_HOUR" "$BACKUP_CRON_MIN")"

  # ---- Alle Eingaben in State-Datei speichern (für Resume) ----
  cat >> "$STATE_FILE" << STATEEOF
GITHUB_REPO_URL="${GITHUB_REPO_URL}"
USE_GITHUB="${USE_GITHUB}"
LOCAL_IP="${LOCAL_IP}"
ALL_LOCAL_IPS="${ALL_LOCAL_IPS}"
HOSTNAME_FQDN="${HOSTNAME_FQDN}"
MODE="${MODE}"
MODESEL="${MODESEL}"
DEBUG_VALUE="${DEBUG_VALUE}"
ALLOWED_HOSTS="${ALLOWED_HOSTS}"
NGINX_SERVER_NAMES="${NGINX_SERVER_NAMES}"
CSRF_TRUSTED_ORIGINS_VALUE="${CSRF_TRUSTED_ORIGINS_VALUE}"
GUNICORN_PORT="${GUNICORN_PORT}"
DBTYPE="${DBTYPE}"
DBTYPE_SEL="${DBTYPE_SEL}"
DB_PACKAGE_LOCAL="${DB_PACKAGE_LOCAL:-}"
DB_PACKAGE_CLIENT="${DB_PACKAGE_CLIENT:-}"
DBMODE="${DBMODE:-}"
DBNAME="${DBNAME:-}"
DBUSER="${DBUSER:-}"
DBPASS="${DBPASS:-}"
DBHOST="${DBHOST:-}"
DBPORT="${DBPORT:-}"
DB_ENGINE="${DB_ENGINE:-}"
DB_PATH="${DB_PATH:-}"
APPUSER="${APPUSER}"
DJKEY="${DJKEY}"
SSH_KEY_PASSPHRASE="${SSH_KEY_PASSPHRASE}"
SSH_KEY_PATH="${SSH_KEY_PATH}"
GUNICORN_WORKERS="${GUNICORN_WORKERS}"
LANGUAGE_CODE="${LANGUAGE_CODE}"
TIME_ZONE="${TIME_ZONE}"
EMAIL_HOST="${EMAIL_HOST:-}"
EMAIL_PORT="${EMAIL_PORT:-}"
EMAIL_HOST_USER="${EMAIL_HOST_USER:-}"
EMAIL_HOST_PASSWORD="${EMAIL_HOST_PASSWORD:-}"
EMAIL_USE_TLS="${EMAIL_USE_TLS:-}"
DEFAULT_FROM_EMAIL="${DEFAULT_FROM_EMAIL:-}"
BACKUP_CRON_HOUR="${BACKUP_CRON_HOUR}"
BACKUP_CRON_MIN="${BACKUP_CRON_MIN}"
STATEEOF
  chmod 600 "$STATE_FILE"
  mark_done "input_saved"

fi  # end of input section (! is_done "input_saved")

# -------------------------------------------------------------------
# System-Pakete aktualisieren
# -------------------------------------------------------------------
if ! is_done "pkgs_installed"; then
apt update

_read -p "System-Pakete updaten? (empfohlen) [J/n]: " UPGRADE
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
_read -p "fail2ban installieren (schützt SSH)? [J/n]: " INSTALL_FAIL2BAN
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

mark_done "pkgs_installed"
else
  echo "⏭️  Pakete bereits installiert - überspringe"
fi  # end pkgs_installed

# -------------------------------------------------------------------
# SSH-Server: PasswordAuthentication + PermitRootLogin sicherstellen
# -------------------------------------------------------------------
if ! is_done "sshd_configured"; then
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

mark_done "sshd_configured"
else
  echo "⏭️  SSH-Konfiguration bereits erledigt - überspringe"
fi  # end sshd_configured

# -------------------------------------------------------------------
# App-User erstellen
# -------------------------------------------------------------------
if ! is_done "appuser_created"; then
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
  _read -s -p "Passwort für $APPUSER: " APPUSER_PASS; echo
  [ -z "$APPUSER_PASS" ] && echo "❌ Passwort darf nicht leer sein." && continue
  _read -s -p "Passwort bestätigen: " APPUSER_PASS2; echo
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

mark_done "appuser_created"
else
  echo "⏭️  App-User bereits erstellt - überspringe"
fi  # end appuser_created

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
  _read -p "Fortfahren nachdem der Key zu GitHub hinzugefügt wurde? (J/n): " CONFIRM
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
if ! is_done "db_setup"; then
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

mark_done "db_setup"
else
  echo "⏭️  Datenbank bereits eingerichtet - überspringe"
fi  # end db_setup

# -------------------------------------------------------------------
# Projektverzeichnis
# -------------------------------------------------------------------
if ! is_done "project_setup"; then
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

mark_done "project_setup"
else
  echo "⏭️  Projekt bereits eingerichtet - überspringe"
  # Django-Modul erkennen (für nachfolgende Schritte)
  DJANGO_MODULE=$(find "$APPDIR" -maxdepth 2 -name "wsgi.py" ! -path "*/.venv/*" 2>/dev/null | head -1 | xargs -I{} dirname {} | xargs -I{} basename {} 2>/dev/null || echo "core")
fi  # end project_setup

# -------------------------------------------------------------------
# .env Datei
# -------------------------------------------------------------------
if ! is_done "config_done"; then
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
LANGUAGE_CODE="$LANGUAGE_CODE"
TIME_ZONE="$TIME_ZONE"
EMAIL_HOST="${EMAIL_HOST:-}"
EMAIL_PORT="${EMAIL_PORT:-}"
EMAIL_HOST_USER="${EMAIL_HOST_USER:-}"
EMAIL_HOST_PASSWORD="${EMAIL_HOST_PASSWORD:-}"
EMAIL_USE_TLS="${EMAIL_USE_TLS:-}"
DEFAULT_FROM_EMAIL="${DEFAULT_FROM_EMAIL:-}"
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

LANGUAGE_CODE = os.getenv("LANGUAGE_CODE", "de-de")
TIME_ZONE = os.getenv("TIME_ZONE", "Europe/Berlin")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# E-Mail-Konfiguration
_email_host = os.getenv("EMAIL_HOST", "")
if _email_host:
    EMAIL_BACKEND       = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST          = _email_host
    EMAIL_PORT          = int(os.getenv("EMAIL_PORT", "587"))
    EMAIL_HOST_USER     = os.getenv("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS       = os.getenv("EMAIL_USE_TLS", "True") == "True"
    DEFAULT_FROM_EMAIL  = os.getenv("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER)
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

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

mark_done "config_done"
else
  echo "⏭️  Konfiguration bereits erledigt - überspringe"
fi  # end config_done

# -------------------------------------------------------------------
# Log-Verzeichnis (muss vor Migrationen existieren, da settings.py darauf zugreift)
# -------------------------------------------------------------------
if ! is_done "logdir_done"; then
  mkdir -p "/var/log/${PROJECTNAME}"
  chown "$APPUSER:adm" "/var/log/${PROJECTNAME}"
  chmod 750 "/var/log/${PROJECTNAME}"
  mark_done "logdir_done"
fi  # end logdir_done

# -------------------------------------------------------------------
# Migrationen
# -------------------------------------------------------------------
if ! is_done "migrations_done"; then
  # DB-Verbindung testen bevor Migrationen gestartet werden
  echo "🔍 Teste Datenbankverbindung..."
  if [ "$DBTYPE" = "postgresql" ]; then
    if ! PGPASSWORD="$DBPASS" psql -h "$DBHOST" -p "$DBPORT" -U "$DBUSER" -d "$DBNAME" \
         -c "SELECT 1" >/dev/null 2>&1; then
      echo "❌ FEHLER: PostgreSQL-Verbindung fehlgeschlagen!"
      echo "   Host: $DBHOST | Port: $DBPORT | User: $DBUSER | DB: $DBNAME"
      echo "   Prüfe: Zugangsdaten, ob PostgreSQL läuft, ob User und DB existieren."
      exit 1
    fi
    echo "✅ PostgreSQL-Verbindung erfolgreich"
  elif [ "$DBTYPE" = "mysql" ]; then
    if ! mysql -h "$DBHOST" -P "$DBPORT" -u "$DBUSER" -p"$DBPASS" \
         "$DBNAME" -e "SELECT 1" >/dev/null 2>&1; then
      echo "❌ FEHLER: MySQL/MariaDB-Verbindung fehlgeschlagen!"
      echo "   Host: $DBHOST | Port: $DBPORT | User: $DBUSER | DB: $DBNAME"
      exit 1
    fi
    echo "✅ MySQL-Verbindung erfolgreich"
  fi

  echo "📊 Führe Migrationen aus..."
  su - "$APPUSER" -s /bin/bash -c "cd $APPDIR && source .venv/bin/activate && python manage.py migrate"
  mark_done "migrations_done"
else
  echo "⏭️  Migrationen bereits ausgeführt - überspringe"
fi  # end migrations_done

# -------------------------------------------------------------------
# Statische Dateien
# -------------------------------------------------------------------
if ! is_done "static_done"; then
  echo "📦 Sammle statische Dateien..."
  su - "$APPUSER" -s /bin/bash -c "cd $APPDIR && source .venv/bin/activate && python manage.py collectstatic --noinput"
  mark_done "static_done"
else
  echo "⏭️  Statische Dateien bereits gesammelt - überspringe"
fi  # end static_done

# -------------------------------------------------------------------
# Django Superuser erstellen
# -------------------------------------------------------------------
if ! is_done "superuser_done"; then
echo
echo "👑 Django Superuser erstellen (Admin-Login für /djadmin/)"
_read -p "Admin-Username [admin]: " DJANGO_ADMIN_USER
DJANGO_ADMIN_USER="${DJANGO_ADMIN_USER:-admin}"

_read -p "Admin-Email [admin@localhost]: " DJANGO_ADMIN_EMAIL
DJANGO_ADMIN_EMAIL="${DJANGO_ADMIN_EMAIL:-admin@localhost}"

while true; do
  _read -s -p "Admin-Passwort: " DJANGO_ADMIN_PASS; echo
  [ -z "$DJANGO_ADMIN_PASS" ] && echo "❌ Passwort darf nicht leer sein." && continue
  _read -s -p "Admin-Passwort bestätigen: " DJANGO_ADMIN_PASS2; echo
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

mark_done "superuser_done"
else
  echo "⏭️  Django Superuser bereits erstellt - überspringe"
fi  # end superuser_done

# -------------------------------------------------------------------
# systemd Service
# -------------------------------------------------------------------
if ! is_done "systemd_done"; then
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
ExecStart=$APPDIR/.venv/bin/gunicorn $DJANGO_MODULE.wsgi:application --bind 127.0.0.1:${GUNICORN_PORT} --workers ${GUNICORN_WORKERS} --timeout 120 --access-logfile /var/log/${PROJECTNAME}/access.log --error-logfile /var/log/${PROJECTNAME}/error.log
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

mark_done "systemd_done"
else
  echo "⏭️  systemd Service bereits konfiguriert - überspringe"
fi  # end systemd_done

# -------------------------------------------------------------------
# Log-Rotation
# -------------------------------------------------------------------
if ! is_done "logrotate_done"; then
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

mark_done "logrotate_done"
fi  # end logrotate_done

# -------------------------------------------------------------------
# nginx Konfiguration
# -------------------------------------------------------------------
if ! is_done "nginx_done"; then
echo "🌐 Konfiguriere Nginx..."
cat > /etc/nginx/sites-available/$PROJECTNAME <<EOF
server {
    listen 80;
    server_name $NGINX_SERVER_NAMES;
    client_max_body_size 50M;

    # Gzip Komprimierung
    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 6;
    gzip_types text/plain text/css text/xml application/json
               application/javascript application/xml+rss
               application/atom+xml image/svg+xml;

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
        proxy_pass http://127.0.0.1:${GUNICORN_PORT};
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

mark_done "nginx_done"

# Zoraxy Konfiguration direkt anzeigen
_ZORAXY_HOST=$(echo "${ALLOWED_HOSTS:-$LOCAL_IP}" | cut -d, -f1 | tr -d ' ')
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔀 Zoraxy Reverse Proxy — so eintragen:"
echo
echo "   Zoraxy → Proxy Rules → New Proxy Rule:"
echo "   ┌──────────────────────────────────────────────────────────────┐"
echo "   │  Matching Domain:  ${_ZORAXY_HOST}"
echo "   │  Target:           http://${LOCAL_IP}:80"
echo "   │  ✅ Pass / Preserve Host Header aktivieren"
echo "   └──────────────────────────────────────────────────────────────┘"
echo
echo "   nginx server_name (dieser Server): ${NGINX_SERVER_NAMES}"
echo "   Gunicorn intern:                   127.0.0.1:${GUNICORN_PORT}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
else
  echo "⏭️  Nginx bereits konfiguriert - überspringe"
fi  # end nginx_done

# -------------------------------------------------------------------
# Projekt-Registry (für MOTD und Verwaltung aller Django-Server)
# -------------------------------------------------------------------
if ! is_done "registry_done"; then
  mkdir -p /etc/django-servers.d
  chmod 755 /etc/django-servers.d
  # Ersten ALLOWED_HOST als Primary-Domain ermitteln (für Zoraxy-Anzeige)
  _PRIMARY_HOST=$(echo "${ALLOWED_HOSTS:-$LOCAL_IP}" | cut -d, -f1 | tr -d ' ')
  cat > /etc/django-servers.d/${PROJECTNAME}.conf <<REGEOF
PROJECTNAME="${PROJECTNAME}"
APPDIR="${APPDIR}"
APPUSER="${APPUSER}"
MODE="${MODE}"
DEBUG="${DEBUG_VALUE}"
GUNICORN_PORT="${GUNICORN_PORT}"
DBTYPE="${DBTYPE}"
DBNAME="${DBNAME:-}"
DBHOST="${DBHOST:-}"
DBPORT="${DBPORT:-}"
LOCAL_IP="${LOCAL_IP}"
HOSTNAME_FQDN="${HOSTNAME_FQDN}"
PRIMARY_HOST="${_PRIMARY_HOST}"
GITHUB_REPO_URL="${GITHUB_REPO_URL:-}"
GUNICORN_WORKERS="${GUNICORN_WORKERS}"
LANGUAGE_CODE="${LANGUAGE_CODE}"
TIME_ZONE="${TIME_ZONE}"
EMAIL_HOST="${EMAIL_HOST:-}"
BACKUP_CRON_HOUR="${BACKUP_CRON_HOUR}"
BACKUP_CRON_MIN="${BACKUP_CRON_MIN}"
INSTALL_DATE="$(date '+%Y-%m-%d %H:%M')"
REGEOF
  chmod 644 /etc/django-servers.d/${PROJECTNAME}.conf
  echo "✅ Projekt-Registry eingetragen: /etc/django-servers.d/${PROJECTNAME}.conf"
  mark_done "registry_done"
else
  echo "⏭️  Registry bereits eingetragen - überspringe"
fi  # end registry_done

# -------------------------------------------------------------------
# Sudoers + Update-Skript
# -------------------------------------------------------------------
if ! is_done "scripts_done"; then
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

# Backup vor dem Update
echo "💾 Erstelle Sicherung vor Update..."
/usr/local/bin/\${SERVICE}_backup.sh && echo "✅ Backup erstellt" \
  || echo "⚠️  Backup fehlgeschlagen — Update wird trotzdem fortgesetzt"

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
# Automatischer Backup-Cron
# -------------------------------------------------------------------
echo "⏰ Richte Backup-Cron ein (täglich $(printf '%02d:%02d' "$BACKUP_CRON_HOUR" "$BACKUP_CRON_MIN"))..."
( crontab -l 2>/dev/null | grep -v "${PROJECTNAME}_backup.sh" || true
  echo "${BACKUP_CRON_MIN} ${BACKUP_CRON_HOUR} * * * /usr/local/bin/${PROJECTNAME}_backup.sh >> /var/log/${PROJECTNAME}/backup.log 2>&1"
) | crontab -
echo "✅ Backup-Cron: $(printf '%02d:%02d' "$BACKUP_CRON_HOUR" "$BACKUP_CRON_MIN") täglich"

# -------------------------------------------------------------------
# Deinstallations-Skript
# -------------------------------------------------------------------
echo "🗑️  Erstelle Deinstallations-Skript..."
cat > /usr/local/bin/${PROJECTNAME}_remove.sh <<REMOVEEOF
#!/bin/bash
set -euo pipefail

PROJECT="${PROJECTNAME}"
APPDIR="${APPDIR}"
APPUSER="${APPUSER}"
DBTYPE="${DBTYPE}"
DBNAME="${DBNAME:-}"
DBUSER_DB="${DBUSER:-}"
BACKUP_DIR="/var/backups/\${PROJECT}"

echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║           DEINSTALLATION: \${PROJECT}                          ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo "⚠️  Entfernt Service, nginx, Configs und optional alle Daten."
_read -p "Wirklich deinstallieren? (j/N): " _C
[[ ! "\${_C:-N}" =~ ^[Jj]\$ ]] && echo "Abbruch." && exit 0

# Service stoppen
echo "🛑 Stoppe Service..."
systemctl stop "\${PROJECT}"   2>/dev/null || true
systemctl disable "\${PROJECT}" 2>/dev/null || true
rm -f "/etc/systemd/system/\${PROJECT}.service"
systemctl daemon-reload

# nginx entfernen
rm -f "/etc/nginx/sites-enabled/\${PROJECT}"
rm -f "/etc/nginx/sites-available/\${PROJECT}"
nginx -t 2>/dev/null && systemctl reload nginx || true

# Konfigurationen entfernen
rm -f "/etc/sudoers.d/\${PROJECT}-service"
rm -f "/etc/logrotate.d/\${PROJECT}"
rm -f "/etc/django-servers.d/\${PROJECT}.conf"

# Cron-Job entfernen
( crontab -l 2>/dev/null | grep -v "\${PROJECT}_backup.sh" || true ) | crontab - 2>/dev/null || true
echo "✅ Service, nginx, Configs und Cron entfernt"

# Projektverzeichnis?
_read -p "Projektverzeichnis '\${APPDIR}' löschen? (j/N): " _R
[[ "\${_R:-N}" =~ ^[Jj]\$ ]] && rm -rf "\${APPDIR}" && echo "✅ Projektverzeichnis entfernt"

# Logs?
_read -p "Log-Verzeichnis '/var/log/\${PROJECT}' löschen? (j/N): " _R
[[ "\${_R:-N}" =~ ^[Jj]\$ ]] && rm -rf "/var/log/\${PROJECT}" && echo "✅ Logs entfernt"

# Backups?
_read -p "Backup-Verzeichnis '\${BACKUP_DIR}' löschen? (j/N): " _R
[[ "\${_R:-N}" =~ ^[Jj]\$ ]] && rm -rf "\${BACKUP_DIR}" && echo "✅ Backups entfernt"

# Datenbank?
if [ -n "\${DBNAME:-}" ]; then
  if [ "\${DBTYPE}" = "postgresql" ]; then
    _read -p "PostgreSQL DB '\${DBNAME}' + User '\${DBUSER_DB}' löschen? (j/N): " _R
    if [[ "\${_R:-N}" =~ ^[Jj]\$ ]]; then
      su -s /bin/bash postgres -c "psql -c \"DROP DATABASE IF EXISTS \\\"\${DBNAME}\\\";\"" 2>/dev/null || true
      su -s /bin/bash postgres -c "psql -c \"DROP USER IF EXISTS \\\"\${DBUSER_DB}\\\";\"" 2>/dev/null || true
      echo "✅ PostgreSQL DB + User entfernt"
    fi
  elif [ "\${DBTYPE}" = "mysql" ]; then
    _read -p "MySQL DB '\${DBNAME}' + User '\${DBUSER_DB}' löschen? (j/N): " _R
    if [[ "\${_R:-N}" =~ ^[Jj]\$ ]]; then
      mysql -u root -e "DROP DATABASE IF EXISTS \`\${DBNAME}\`; DROP USER IF EXISTS '\${DBUSER_DB}'@'localhost';" 2>/dev/null || true
      echo "✅ MySQL DB + User entfernt"
    fi
  fi
fi

# Linux-User?
_read -p "Linux-User '\${APPUSER}' + Home-Verzeichnis löschen? (j/N): " _R
if [[ "\${_R:-N}" =~ ^[Jj]\$ ]]; then
  deluser --remove-home "\${APPUSER}" 2>/dev/null || userdel -r "\${APPUSER}" 2>/dev/null || true
  echo "✅ Linux-User entfernt"
fi

# Skripte selbst entfernen
rm -f "/usr/local/bin/\${PROJECT}_update.sh"
rm -f "/usr/local/bin/\${PROJECT}_backup.sh"
rm -f "\$0"

echo
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║           DEINSTALLATION ABGESCHLOSSEN ✅                    ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
REMOVEEOF
chmod 755 /usr/local/bin/${PROJECTNAME}_remove.sh
echo "🗑️  Deinstallations-Skript: /usr/local/bin/${PROJECTNAME}_remove.sh"

# -------------------------------------------------------------------
# Server-Status-Skript (global, einmalig erstellt/überschrieben)
# -------------------------------------------------------------------
cat > /usr/local/bin/django_status.sh <<'STATUSEOF'
#!/bin/bash
CONF_DIR="/etc/django-servers.d"
[ -d "$CONF_DIR" ] || { echo "Keine Django-Projekte registriert."; exit 0; }

_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/{print $7;exit}')
[ -z "$_IP" ] && _IP=$(hostname -I 2>/dev/null | awk '{print $1}')

echo
echo "╔════════════════════════════════════════════════════════════════════════╗"
printf "║  %-70s  ║\n" "Django Server Status — $(hostname -f 2>/dev/null)"
echo "╠════════════════════════════════════════════════════════════════════════╣"
printf "║  %-20s %-7s %-6s %-10s %-12s %-10s  ║\n" \
  "PROJEKT" "PORT" "MODUS" "DB" "SERVICE" "/health/"
echo "╠════════════════════════════════════════════════════════════════════════╣"

for _conf in "$CONF_DIR"/*.conf; do
  [ -f "$_conf" ] || continue
  _PROJ=$(grep '^PROJECTNAME='  "$_conf" | cut -d= -f2 | tr -d '"')
  _PORT=$(grep '^GUNICORN_PORT=' "$_conf" | cut -d= -f2 | tr -d '"')
  _MODE=$(grep '^MODE='         "$_conf" | cut -d= -f2 | tr -d '"')
  _DB=$(grep   '^DBTYPE='       "$_conf" | cut -d= -f2 | tr -d '"')
  systemctl is-active --quiet "$_PROJ" 2>/dev/null && _SVC="aktiv ✅" || _SVC="gestoppt ❌"
  _HTTP=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 \
    "http://127.0.0.1:${_PORT:-8000}/health/" 2>/dev/null || echo "---")
  case "$_HTTP" in 200) _HSTR="200 ✅";; ---) _HSTR="timeout ⏱";; *) _HSTR="${_HTTP} ⚠️";; esac
  printf "║  %-20s %-7s %-6s %-10s %-12s %-10s  ║\n" \
    "$_PROJ" "${_PORT:-?}" "${_MODE:-?}" "${_DB:-?}" "$_SVC" "$_HSTR"
done

echo "╚════════════════════════════════════════════════════════════════════════╝"
printf "   %s  |  %s\n" "$(date '+%d.%m.%Y %H:%M:%S')" "Server: $_IP"
echo
STATUSEOF
chmod 755 /usr/local/bin/django_status.sh
echo "📊 Status-Skript: django_status.sh"

mark_done "scripts_done"
else
  echo "⏭️  Sudoers und Skripte bereits erstellt - überspringe"
fi  # end scripts_done

# -------------------------------------------------------------------
# Health-Check (nur bei neuem Projekt)
# -------------------------------------------------------------------
if ! is_done "healthcheck_done"; then
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

mark_done "healthcheck_done"
else
  echo "⏭️  Health-Check bereits erstellt - überspringe"
fi  # end healthcheck_done

# -------------------------------------------------------------------
# MOTD (dynamisch - zeigt alle Django-Server beim Login)
# -------------------------------------------------------------------
if ! is_done "motd_done"; then

# Alte projektspezifische MOTD-Dateien aufräumen
rm -f /etc/profile.d/${PROJECTNAME}_motd.sh 2>/dev/null || true

# Gemeinsames MOTD-Skript erstellen (statisch, liest Registry zur Laufzeit)
# Wird bei jedem neuen Projekt überschrieben (identischer Inhalt)
cat > /etc/profile.d/00_django_motd.sh <<'MOTDEOF'
#!/bin/bash
# Django Multi-Server MOTD - liest /etc/django-servers.d/*.conf

# Nur bei interaktiven Shells
case "$-" in
  *i*) ;;
  *) return ;;
esac

# Nur einmal pro Session anzeigen
if [ -n "${DJANGO_MOTD_SHOWN:-}" ]; then
  return
fi
export DJANGO_MOTD_SHOWN=1

CONF_DIR="/etc/django-servers.d"
[ -d "$CONF_DIR" ] || return

# Projekte zählen
PROJECT_COUNT=0
for _c in "$CONF_DIR"/*.conf; do
  [ -f "$_c" ] && PROJECT_COUNT=$(( PROJECT_COUNT + 1 ))
done
[ "$PROJECT_COUNT" -eq 0 ] && return

# Systeminfo ermitteln
_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/{print $7;exit}')
[ -z "$_IP" ] && _IP=$(hostname -I 2>/dev/null | awk '{print $1}')
_HOST=$(hostname -f 2>/dev/null || hostname)

echo
echo "╔══════════════════════════════════════════════════════════════════════════╗"
printf "║  %-72s  ║\n" "Django Server Übersicht - $_HOST"
echo "╠══════════════════════════════════════════════════════════════════════════╣"
printf "║  %-22s %-7s %-6s %-10s %-10s %-14s ║\n" \
  "PROJEKT" "PORT" "MODUS" "DATENBANK" "STATUS" "BENUTZER"
echo "╠══════════════════════════════════════════════════════════════════════════╣"

for _conf in "$CONF_DIR"/*.conf; do
  [ -f "$_conf" ] || continue
  _PROJ=$(grep '^PROJECTNAME=' "$_conf" | cut -d= -f2 | tr -d '"')
  _PORT=$(grep '^GUNICORN_PORT=' "$_conf" | cut -d= -f2 | tr -d '"')
  _MODE=$(grep '^MODE=' "$_conf" | cut -d= -f2 | tr -d '"')
  _DB=$(grep '^DBTYPE=' "$_conf" | cut -d= -f2 | tr -d '"')
  _USER=$(grep '^APPUSER=' "$_conf" | cut -d= -f2 | tr -d '"')
  if systemctl is-active --quiet "$_PROJ" 2>/dev/null; then
    _STATUS="aktiv ✅"
  else
    _STATUS="gestoppt ❌"
  fi
  printf "║  %-22s %-7s %-6s %-10s %-10s %-14s ║\n" \
    "$_PROJ" "${_PORT:-8000}" "${_MODE:-?}" "${_DB:-?}" "$_STATUS" "${_USER:-?}"
done

echo "╠══════════════════════════════════════════════════════════════════════════╣"
printf "║  %-72s  ║\n" "IP: $_IP  |  $(date '+%d.%m.%Y %H:%M')  |  Uptime: $(uptime -p 2>/dev/null)"
echo "╚══════════════════════════════════════════════════════════════════════════╝"
echo

# Detailansicht je Projekt
_idx=1
for _conf in "$CONF_DIR"/*.conf; do
  [ -f "$_conf" ] || continue
  _PROJ=$(grep '^PROJECTNAME=' "$_conf" | cut -d= -f2 | tr -d '"')
  _APPDIR=$(grep '^APPDIR=' "$_conf" | cut -d= -f2 | tr -d '"')
  _USER=$(grep '^APPUSER=' "$_conf" | cut -d= -f2 | tr -d '"')
  _PORT=$(grep '^GUNICORN_PORT=' "$_conf" | cut -d= -f2 | tr -d '"')
  _MODE=$(grep '^MODE=' "$_conf" | cut -d= -f2 | tr -d '"')
  _DB=$(grep '^DBTYPE=' "$_conf" | cut -d= -f2 | tr -d '"')
  _DBNAME=$(grep '^DBNAME=' "$_conf" | cut -d= -f2 | tr -d '"')
  _DBHOST=$(grep '^DBHOST=' "$_conf" | cut -d= -f2 | tr -d '"')
  _DBPORT=$(grep '^DBPORT=' "$_conf" | cut -d= -f2 | tr -d '"')
  _GITHUB=$(grep '^GITHUB_REPO_URL=' "$_conf" | cut -d= -f2 | tr -d '"')
  _DATE=$(grep '^INSTALL_DATE=' "$_conf" | cut -d= -f2- | tr -d '"')
  _LIP=$(grep '^LOCAL_IP=' "$_conf" | cut -d= -f2 | tr -d '"')
  _KEYPATH="/home/$_USER/.ssh/id_ed25519"

  echo "  [$_idx] $_PROJ  (installiert: ${_DATE:-?})"
  echo "  ┌─────────────────────────────────────────────────────────────────"
  echo "  │  👤 App-User:    $_USER"
  echo "  │  📁 Pfad:        $_APPDIR"
  echo "  │  🌐 Modus:       $_MODE  |  🔌 Gunicorn: 127.0.0.1:${_PORT:-8000}"
  if [ -n "$_DBNAME" ]; then
    echo "  │  🗄️  DB:         ${_DB:-?}  |  Name: $_DBNAME  |  Host: ${_DBHOST:-localhost}:${_DBPORT:-5432}"
  else
    echo "  │  🗄️  DB:         ${_DB:-?}"
  fi
  [ -n "$_GITHUB" ] && \
  echo "  │  📦 GitHub:      $_GITHUB"
  echo "  │"
  echo "  │  ── Befehle (als root ausführen) ─────────────────────────────"
  if [ -n "$_GITHUB" ]; then
    echo "  │  🔄 Git Pull:    su - $_USER -s /bin/bash -c \"cd $_APPDIR && git pull\""
  fi
  echo "  │  🚀 Update:      ${_PROJ}_update.sh          (pull+migrate+static+restart)"
  echo "  │  🔁 Neustart:    systemctl restart $_PROJ"
  echo "  │  📊 Status:      systemctl status $_PROJ"
  echo "  │  📋 Logs live:   journalctl -u $_PROJ -f"
  echo "  │  💾 Backup:      ${_PROJ}_backup.sh"
  echo "  │"
  echo "  │  ── Zugriff ───────────────────────────────────────────────────"
  echo "  │  🌍 Django-Admin: http://${_LIP:-$_IP}/djadmin/"
  echo "  │  🔐 SSH als User: ssh $_USER@${_LIP:-$_IP}  (Key: $_KEYPATH)"
  echo "  │  📥 Key herunterladen: scp root@${_LIP:-$_IP}:$_KEYPATH ."
  echo "  └─────────────────────────────────────────────────────────────────"
  echo
  _idx=$(( _idx + 1 ))
done

echo "  🔀 Zoraxy Reverse Proxy Konfiguration:"
echo "     nginx auf diesem Server hört auf Port 80, leitet per server_name."
echo "     In Zoraxy (anderer Server) je Domain einen Eintrag anlegen:"
for _conf in "$CONF_DIR"/*.conf; do
  [ -f "$_conf" ] || continue
  _LIP=$(grep '^LOCAL_IP=' "$_conf" | cut -d= -f2 | tr -d '"')
  _PHOST=$(grep '^PRIMARY_HOST=' "$_conf" | cut -d= -f2 | tr -d '"')
  _PROJ=$(grep '^PROJECTNAME=' "$_conf" | cut -d= -f2 | tr -d '"')
  printf "     %-32s →  http://%s:80\n" "${_PHOST:-$_PROJ}" "${_LIP:-$_IP}"
done
echo "     ⚠️  'Pass Host Header' / 'Preserve Host' in Zoraxy aktivieren!"
echo "  ════════════════════════════════════════════════════════════════════"
echo
MOTDEOF

chmod 644 /etc/profile.d/00_django_motd.sh

mark_done "motd_done"
else
  echo "⏭️  MOTD bereits konfiguriert - überspringe"
fi  # end motd_done

# -------------------------------------------------------------------
# Abschluss + State-Datei aufräumen
# -------------------------------------------------------------------
echo
echo "╔════════════════════════════════════════════════════════════════════╗"
echo "║                    INSTALLATION FERTIG ✅                         ║"
echo "╚════════════════════════════════════════════════════════════════════╝"
echo "📁 Projektverzeichnis:  $APPDIR"
echo "👤 App-Benutzer:        $APPUSER"
echo "🌐 Modus:               $MODE (DEBUG=$DEBUG_VALUE)"
echo "🔌 Gunicorn-Port:       127.0.0.1:${GUNICORN_PORT}  (intern)"
echo "🗄️  Datenbank:          ${DBTYPE^^}"
if [ "$DBTYPE" != "sqlite" ]; then
  echo "   DB-Engine:           $DB_ENGINE"
  echo "   DB-Name:             $DBNAME"
  echo "   DB-Host:             $DBHOST"
  echo "   DB-Port:             $DBPORT"
fi
echo
echo "🔐 SSH-ZUGRIFF:"
echo "   Benutzer:      $APPUSER"
echo "   IP-Adresse:    $LOCAL_IP"
echo "   Hostname:      $HOSTNAME_FQDN"
echo "   Private Key:   $SSH_KEY_PATH"
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📥 Private Key für WinSCP/PuTTY herunterladen:"
echo "   scp root@${LOCAL_IP}:${SSH_KEY_PATH} ."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "📦 Update:      ${PROJECTNAME}_update.sh"
echo "💾 Backup:      ${PROJECTNAME}_backup.sh"
echo "📊 Status:      systemctl status ${PROJECTNAME}"
echo "📋 Logs:        journalctl -u ${PROJECTNAME} -f"
echo
echo "🌐 Django Admin: http://${LOCAL_IP}/djadmin/"
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔀 Zoraxy Reverse Proxy Konfiguration:"
echo "   Datenfluss:  Internet → Zoraxy (SSL) → nginx:80 → gunicorn:${GUNICORN_PORT}"
echo
echo "   In Zoraxy einen neuen Proxy-Eintrag anlegen:"
echo "   ┌─────────────────────────────────────────────────────────────┐"
echo "   │  Incoming:  <your-domain.example.com>                      │"
echo "   │  Target:    http://${LOCAL_IP}:80                          │"
echo "   │  Option:    'Pass Host Header' / 'Preserve Host' ✅        │"
echo "   └─────────────────────────────────────────────────────────────┘"
echo
echo "   Nginx server_name auf diesem Server:"
echo "   /etc/nginx/sites-available/${PROJECTNAME}"
echo "   → server_name: ${NGINX_SERVER_NAMES}"
echo
echo "   Beim nächsten Login wird die vollständige Serverübersicht"
echo "   mit allen Django-Projekten angezeigt (/etc/profile.d/00_django_motd.sh)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "✅ FERTIG! Viel Erfolg mit deinem Django-Projekt! 🚀"
echo "════════════════════════════════════════════════════════════════════"

# State-Datei nach erfolgreicher Installation entfernen
if [ -f "${STATE_FILE:-}" ]; then
  rm -f "$STATE_FILE"
  echo "🧹 Installations-State-Datei bereinigt"
fi

fi  # end INSTALL_PROJECT

# ===================================================================
# MANAGER-INSTALLATION (PLATZHALTER — wird in nächstem Abschnitt befüllt)
# ===================================================================
if [ "${INSTALL_MANAGER:-false}" = "true" ]; then
  echo
  echo "╔════════════════════════════════════════════════════════════════════╗"
  echo "║           DjangoMultiDeploy Manager — Installation                ║"
  echo "╚════════════════════════════════════════════════════════════════════╝"
  _MANAGER_DIR="/srv/djmanager"
  _MANAGER_PORT="${MANAGER_PORT:-8888}"
  _MANAGER_USER="${MANAGER_USER:-djmanager}"

  echo "📁 Manager-Verzeichnis:  $_MANAGER_DIR"
  echo "🔌 Manager-Port:         $_MANAGER_PORT"
  echo "👤 Manager-User:         $_MANAGER_USER"
  echo
  # Manager-Dateien werden vom Installer aus dem Repo geladen
  # (script liegt neben /manager/ Verzeichnis im Repo)
  _SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  _MANAGER_SRC="${_SCRIPT_DIR}/manager"

  if [ ! -d "$_MANAGER_SRC" ]; then
    echo "❌ FEHLER: Manager-Quellverzeichnis nicht gefunden: $_MANAGER_SRC"
    echo "   Stelle sicher, dass das komplette DjangoMultiDeploy-Repo geklont ist."
    exit 1
  fi

  # Linux-User für Manager erstellen
  if ! id "$_MANAGER_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$_MANAGER_USER"
    echo "✅ System-User '$_MANAGER_USER' erstellt"
  fi

  # Zielverzeichnis vorbereiten
  mkdir -p "$_MANAGER_DIR"
  cp -r "$_MANAGER_SRC/." "$_MANAGER_DIR/"

  # Python venv + Abhängigkeiten
  echo "🐍 Installiere Python-Abhängigkeiten für Manager..."
  apt-get install -y -q python3 python3-venv python3-pip build-essential 2>/dev/null || \
    apt install -y python3 python3-venv python3-pip build-essential
  echo "🐍 Erstelle Python venv für Manager..."
  python3 -m venv "$_MANAGER_DIR/venv"
  "$_MANAGER_DIR/venv/bin/pip" install --upgrade pip -q
  "$_MANAGER_DIR/venv/bin/pip" install -r "$_MANAGER_DIR/requirements.txt" -q
  echo "✅ Python-Abhängigkeiten installiert"

  # .env für Manager
  _MANAGER_SECRET="$(openssl rand -hex 32)"
  cat > "$_MANAGER_DIR/.env" <<MANAGERENV
SECRET_KEY=${_MANAGER_SECRET}
DEBUG=False
ALLOWED_HOSTS=*
MANAGER_PORT=${_MANAGER_PORT}
INSTALL_SCRIPT=${_SCRIPT_DIR}/Installv2.sh
REGISTRY_DIR=/etc/django-servers.d
MANAGERENV
  chmod 600 "$_MANAGER_DIR/.env"

  # Datenbankmigrationen
  "$_MANAGER_DIR/venv/bin/python" "$_MANAGER_DIR/manage.py" migrate --run-syncdb 2>/dev/null || true

  # Statische Dateien
  "$_MANAGER_DIR/venv/bin/python" "$_MANAGER_DIR/manage.py" collectstatic --noinput -v 0

  # Berechtigungen
  chown -R "$_MANAGER_USER:$_MANAGER_USER" "$_MANAGER_DIR"
  chmod 750 "$_MANAGER_DIR"

  # systemd Service
  cat > /etc/systemd/system/djmanager.service <<MANSERVEOF
[Unit]
Description=DjangoMultiDeploy Manager
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${_MANAGER_DIR}
ExecStart=${_MANAGER_DIR}/venv/bin/python ${_MANAGER_DIR}/manage.py runserver 0.0.0.0:${_MANAGER_PORT}
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
MANSERVEOF

  systemctl daemon-reload
  systemctl enable --now djmanager
  echo "✅ Manager-Service gestartet"

  # nginx Reverse Proxy für Manager (optional, eigener Port reicht auch)
  # Manager läuft direkt auf Port 8888, kein nginx nötig

  _MGR_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {print $7; exit}')"
  [ -z "${_MGR_IP:-}" ] && _MGR_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

  echo
  echo "╔════════════════════════════════════════════════════════════════════╗"
  echo "║              Manager erfolgreich installiert ✅                    ║"
  echo "╚════════════════════════════════════════════════════════════════════╝"
  echo "🌐 Manager-URL:    http://${_MGR_IP}:${_MANAGER_PORT}/"
  echo "📊 Status:         systemctl status djmanager"
  echo "📋 Logs:           journalctl -u djmanager -f"
  echo
  echo "⚠️  Der Manager läuft als root — nur im internen Netz verwenden!"
  echo "   Für externen Zugriff: Zoraxy/nginx mit Authentifizierung vorschalten."
  echo "════════════════════════════════════════════════════════════════════"
fi  # end INSTALL_MANAGER

echo
echo "✅ DjangoMultiDeploy — Alle Installationen abgeschlossen."
