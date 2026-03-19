#!/bin/bash
set -euo pipefail

# ===================================================================
# DjangoMultiDeploy - Multi-Server Django Installer
# Mehrere Django-Projekte auf einem Server, jedes mit eigenem Port,
# Gunicorn, nginx, systemd, PostgreSQL/MySQL/SQLite, Backup & MOTD
# Reverse Proxy ready | LXC/Container ready
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

# DEBIAN_FRONTEND=noninteractive: verhindert interaktive dpkg-Dialoge
# und stoppt ldconfig/systemd-reload von unnötigen Prozess-Forks beim apt install
export DEBIAN_FRONTEND=noninteractive

# ionice + nice für apt (reduziert I/O-Last auf LXC overlayfs)
# Wird hier initialisiert damit Manager-only-Installation ebenfalls davon profitiert
if command -v ionice >/dev/null 2>&1; then
  _APT="ionice -c3 nice -n 19 apt-get"
else
  _APT="apt-get"
fi

# _apt_install: Installiert Pakete einzeln mit sync+sleep zwischen jedem Block
# Reduziert Peak-Speicher (verhindert OOM im LXC-Container bei cgroup-Limit)
_apt_install() {
  # Alle Argumente als eine Gruppe installieren, dann sync
  $_APT install -y --no-install-recommends "$@"
  sync
  sleep 1
}

# _generate_selfsigned_cert: Erstellt ein Self-Signed SSL-Zertifikat (einmalig, geteilt).
# Das Cert liegt in /etc/ssl/private/djmanager-selfsigned.{key,crt} und wird von allen
# nginx-Sites auf diesem Server verwendet. Echter Cert kommt vom externen Reverse Proxy.
_SSL_KEY="/etc/ssl/private/djmanager-selfsigned.key"
_SSL_CERT="/etc/ssl/certs/djmanager-selfsigned.crt"
_generate_selfsigned_cert() {
  if [ ! -f "$_SSL_CERT" ] || [ ! -f "$_SSL_KEY" ]; then
    echo "🔒 Generiere Self-Signed SSL-Zertifikat (3650 Tage)..."
    openssl req -x509 -newkey rsa:2048 \
      -keyout "$_SSL_KEY" \
      -out "$_SSL_CERT" \
      -days 3650 -nodes \
      -subj "/C=DE/O=DjangoMultiDeploy/CN=$(hostname -f 2>/dev/null || hostname)"
    chmod 600 "$_SSL_KEY"
    echo "✅ SSL-Zertifikat: $_SSL_CERT"
  else
    echo "✅ SSL-Zertifikat bereits vorhanden: $_SSL_CERT"
  fi
}

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

# --- Freier Speicher auf / (mind. 3 GB für LXC overlayfs) ---
_FREE_ROOT_MB=$(df -m / 2>/dev/null | awk 'NR==2{print $4}')
if [ -n "$_FREE_ROOT_MB" ]; then
  if [ "$_FREE_ROOT_MB" -lt 3072 ]; then
    echo "  ❌ Zu wenig Speicher auf /: ${_FREE_ROOT_MB} MB frei (Minimum: 3072 MB)"
    echo "     → df -h /  →  ggf. alte Pakete mit: apt autoremove && apt clean"
    echo "     → LXC: Container-Disk in Proxmox vergrößern (Minimum: 8 GB empfohlen)"
    _PRE_OK=false
  elif [ "$_FREE_ROOT_MB" -lt 5120 ]; then
    echo "  ⚠️  Wenig Speicher auf /: ${_FREE_ROOT_MB} MB frei (Empfehlung: ≥ 5120 MB / 8 GB Disk)"
  else
    echo "  ✅ Freier Speicher auf /: ${_FREE_ROOT_MB} MB"
  fi
fi

# --- Freier RAM (mind. 512 MB für apt + pip) ---
_FREE_RAM_MB=$(awk '/MemAvailable/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo "")
if [ -n "$_FREE_RAM_MB" ]; then
  if [ "$_FREE_RAM_MB" -lt 512 ]; then
    echo "  ❌ Zu wenig RAM verfügbar: ${_FREE_RAM_MB} MB (Minimum: 512 MB)"
    echo "     → LXC: Container-RAM in Proxmox erhöhen (Minimum: 1 GB empfohlen)"
    _PRE_OK=false
  elif [ "$_FREE_RAM_MB" -lt 1024 ]; then
    echo "  ⚠️  Wenig RAM: ${_FREE_RAM_MB} MB frei (Empfehlung: ≥ 1024 MB)"
  else
    echo "  ✅ Verfügbarer RAM: ${_FREE_RAM_MB} MB"
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

# --- LXC / Proxmox Erkennung und Diagnose ---
if [ -f /proc/1/environ ] && grep -qa 'container=lxc' /proc/1/environ 2>/dev/null; then
  echo "  ℹ️  LXC-Container erkannt"
  # Overlayfs-Erkennung
  if grep -q ' overlay ' /proc/mounts 2>/dev/null || \
     grep -q 'upperdir' /proc/mounts 2>/dev/null; then
    echo "  ⚠️  overlayfs Dateisystem erkannt!"
    echo "     overlayfs kann unter Last EIO-Fehler erzeugen und systemd zum Absturz bringen."
    echo "     → Empfehlung: In Proxmox 'local-lvm' statt 'local' (dir) Speicher verwenden."
    echo "     → Oder: pct-Konfiguration prüfen: grep storage /etc/pve/lxc/<ID>.conf"
  fi
  # cgroup Speicherlimit prüfen (Proxmox setzt dieses für den Container)
  _CGROUP_MEM_FILE=""
  for _f in /sys/fs/cgroup/memory/memory.limit_in_bytes \
             /sys/fs/cgroup/memory.max; do
    [ -f "$_f" ] && _CGROUP_MEM_FILE="$_f" && break
  done
  if [ -n "$_CGROUP_MEM_FILE" ]; then
    _CGROUP_MEM=$(cat "$_CGROUP_MEM_FILE" 2>/dev/null || echo "")
    # max bedeutet kein Limit gesetzt
    if [ "$_CGROUP_MEM" != "max" ] && [ -n "$_CGROUP_MEM" ] && [ "$_CGROUP_MEM" -lt 9223372036854771712 ] 2>/dev/null; then
      _CGROUP_MB=$(( _CGROUP_MEM / 1024 / 1024 ))
      if [ "$_CGROUP_MB" -lt 1500 ]; then
        echo "  ❌ Proxmox RAM-Limit für diesen Container: ${_CGROUP_MB} MB — zu wenig!"
        echo "     → In Proxmox: Container → Ressourcen → Arbeitsspeicher auf ≥ 2048 MB erhöhen"
        _PRE_OK=false
      else
        echo "  ✅ Proxmox RAM-Limit: ${_CGROUP_MB} MB"
      fi
    fi
  fi
  # OOM-Score von Prozess 1 (systemd) prüfen
  _OOM1=$(cat /proc/1/oom_score_adj 2>/dev/null || echo "")
  if [ "$_OOM1" = "-1000" ]; then
    echo "  ✅ systemd ist gegen OOM-Killer geschützt (oom_score_adj=-1000)"
  else
    echo "  ⚠️  systemd OOM-Score: ${_OOM1:-?} — systemd könnte bei Speicherdruck gekillt werden"
    echo "     Dies kann zum kompletten Container-Absturz führen während apt/pip läuft."
    echo "     → Proxmox RAM erhöhen ODER Swap aktivieren (lxc.cgroup2.memory.swap.max=2G)"
  fi
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
# Source-Typ (github / zip / new)
# -------------------------------------------------------------------
SOURCE_TYPE="${SOURCE_TYPE:-}"
UPLOAD_ZIP_PATH="${UPLOAD_ZIP_PATH:-}"

if [[ "$SOURCE_TYPE" == "zip" ]]; then
  # ZIP-Modus — vom Manager übergeben, kein interaktives Prompt
  USE_GITHUB=""
  [ -f "$UPLOAD_ZIP_PATH" ] || { echo "❌ FEHLER: UPLOAD_ZIP_PATH='$UPLOAD_ZIP_PATH' nicht gefunden."; exit 1; }
  echo "✅ ZIP-Modus: $(basename "$UPLOAD_ZIP_PATH") wird als Quellcode verwendet"
elif [[ -n "$GITHUB_REPO_URL" ]]; then
  # NONINTERACTIVE mit gesetzter URL
  USE_GITHUB="true"
  SOURCE_TYPE="github"
  echo "✅ GitHub-Modus: $GITHUB_REPO_URL"
else
  echo
  echo "Quellcode-Option:"
  echo "  1) GitHub Repository klonen"
  echo "  2) Leeres Django-Projekt erstellen"
  _read -p "Auswahl (1/2) [2]: " _SRC_SEL
  _SRC_SEL="${_SRC_SEL:-2}"
  if [[ "$_SRC_SEL" == "1" ]]; then
    echo "  • Öffentlich:  https://github.com/user/repo.git"
    echo "  • Privat (SSH): git@github.com:user/repo.git"
    _read -p "GitHub URL: " GITHUB_REPO_URL
    USE_GITHUB="${GITHUB_REPO_URL:+true}"
    SOURCE_TYPE="github"
    echo "✅ GitHub-Modus aktiviert: $GITHUB_REPO_URL"
  else
    USE_GITHUB=""
    SOURCE_TYPE="new"
    echo "✅ Leeres Django-Projekt wird erstellt"
  fi
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
# Hosts / nginx-Port
# -------------------------------------------------------------------
# Alle lokalen IPs für ALLOWED_HOSTS (kein Hostname-Prompt nötig)
# Zugriff läuft ausschließlich über nginx (port-basiert) — kein server_name
NGINX_PORT="${NGINX_PORT:-$((GUNICORN_PORT + 1000))}"
echo "🔌 nginx-HTTPS-Port: ${NGINX_PORT}  (Gunicorn intern: 127.0.0.1:${GUNICORN_PORT})"

# ALLOWED_HOSTS: alle lokalen IPs + Loopback — kein Hostname (Reverse Proxy sendet ggf. Domain)
ALLOWED_HOSTS="${ALL_LOCAL_IPS},127.0.0.1,localhost"
ALLOWED_HOSTS="$(echo "$ALLOWED_HOSTS" | tr ',' '\n' | grep -v '^$' | sort -u | paste -sd, -)"

# -------------------------------------------------------------------
# CSRF_TRUSTED_ORIGINS automatisch bauen
# -------------------------------------------------------------------
# HTTP + HTTPS für alle IPs, jeweils mit und ohne Port
# IPv6-Adressen (enthalten ':') in eckige Klammern einschließen
CSRF_TRUSTED_ORIGINS_VALUE="$(echo "$ALL_LOCAL_IPS" | tr ',' '\n' | grep -v '^$' | awk \
  -v port="$NGINX_PORT" '
  NF {
    ip = $0
    if (index(ip, ":") > 0) ip = "[" ip "]"
    print "http://" ip
    print "https://" ip
    print "http://" ip ":" port
    print "https://" ip ":" port
  }' | awk '!seen[$0]++' | paste -sd, -)"
# Loopback ergänzen
CSRF_TRUSTED_ORIGINS_VALUE="${CSRF_TRUSTED_ORIGINS_VALUE},http://127.0.0.1:${NGINX_PORT},https://127.0.0.1:${NGINX_PORT}"

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
SSH_KEY_PASSPHRASE="${SSH_KEY_PASSPHRASE:-}"

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
SOURCE_TYPE="${SOURCE_TYPE}"
UPLOAD_ZIP_PATH="${UPLOAD_ZIP_PATH}"
LOCAL_IP="${LOCAL_IP}"
ALL_LOCAL_IPS="${ALL_LOCAL_IPS}"
HOSTNAME_FQDN="${HOSTNAME_FQDN}"
MODE="${MODE}"
MODESEL="${MODESEL}"
DEBUG_VALUE="${DEBUG_VALUE}"
ALLOWED_HOSTS="${ALLOWED_HOSTS}"
NGINX_PORT="${NGINX_PORT}"
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
APPUSER_PASS="${APPUSER_PASS:-}"
DJKEY="${DJKEY}"
SSH_KEY_PASSPHRASE="${SSH_KEY_PASSPHRASE:-}"
SSH_KEY_PATH="${SSH_KEY_PATH:-}"
GITHUB_DEPLOY_KEY="${GITHUB_DEPLOY_KEY:-/root/.ssh/djmanager_keys/${PROJECTNAME}_ed25519}"
GUNICORN_WORKERS="${GUNICORN_WORKERS}"
DJANGO_ADMIN_USER="${DJANGO_ADMIN_USER:-admin}"
DJANGO_ADMIN_EMAIL="${DJANGO_ADMIN_EMAIL:-admin@localhost}"
DJANGO_ADMIN_PASS="${DJANGO_ADMIN_PASS:-}"
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

$_APT update -qq

_read -p "System-Pakete updaten? (empfohlen) [J/n]: " UPGRADE
[[ "${UPGRADE:-J}" =~ ^[Jj]$ ]] && $_APT upgrade -y --no-install-recommends -qq

# Disk-Check vor dem Installieren (Warnung bei < 2 GB freier Platz)
_FREE_NOW=$(df -m / 2>/dev/null | awk 'NR==2{print $4}')
if [ -n "$_FREE_NOW" ] && [ "$_FREE_NOW" -lt 2048 ]; then
  echo "⚠️  WARNUNG: Nur ${_FREE_NOW} MB frei auf / — apt install könnte Disk füllen!"
  echo "   LXC: Container-Disk in Proxmox vergrößern empfohlen (Minimum 8 GB)"
fi

# Basis-Pakete — in kleinen Gruppen installieren, mit sync+sleep zwischen jeder Gruppe.
# Verhindert OOM-Peak im LXC-Container (2 GB cgroup-Limit wird sonst beim parallelen
# dpkg-Entpacken überschritten → OOM-Killer killt systemd → Container startet nicht mehr)
echo "📦 Installiere Netzwerk + Basis..."
_apt_install curl ca-certificates openssl iproute2 net-tools

echo "📦 Installiere git, nano..."
_apt_install git nano

echo "📦 Installiere nginx..."
_apt_install nginx

echo "📦 Installiere Python..."
_apt_install python3 python3-venv python3-pip

echo "📦 Installiere build-essential..."
_apt_install build-essential

# sudo ist in LXC-Containern oft nicht verfügbar — optional installieren
$_APT install -y --no-install-recommends sudo 2>/dev/null \
  || echo "ℹ️  sudo nicht installierbar (LXC?) — wird nicht benötigt"
sync

# Versionsspezifisches venv-Paket installieren (z.B. python3.11-venv auf Debian/Ubuntu)
_PY_VER_MAIN=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
[ -n "$_PY_VER_MAIN" ] && _apt_install "python${_PY_VER_MAIN}-venv" 2>/dev/null || sync

# Bildverarbeitung (Pillow) — in einem Block, das sind nur Header-Files
echo "🖼️  Installiere Pillow-Abhängigkeiten..."
_apt_install libjpeg-dev zlib1g-dev libpng-dev libwebp-dev

# DB-spezifische Pakete
if [ "$DBTYPE" = "postgresql" ]; then
  echo "📦 Installiere PostgreSQL-Entwicklungspakete..."
  _apt_install libpq-dev
elif [ "$DBTYPE" = "mysql" ]; then
  echo "📦 Installiere MySQL-Entwicklungspakete..."
  _apt_install libmysqlclient-dev python3-dev default-libmysqlclient-dev
fi

# Paket-Cache leeren — frees ~200-500 MB apt cache
echo "🧹 Leere apt-Cache..."
$_APT autoremove -y -qq
$_APT clean
sync
sleep 2

# -------------------------------------------------------------------
# fail2ban installieren
# -------------------------------------------------------------------
_read -p "fail2ban installieren (schützt SSH)? [J/n]: " INSTALL_FAIL2BAN
INSTALL_FAIL2BAN="${INSTALL_FAIL2BAN:-J}"
if [[ "$INSTALL_FAIL2BAN" =~ ^[Jj]$ ]]; then
  echo "🛡️  Installiere fail2ban..."
  _apt_install fail2ban

  # SSH + nginx HTTP Schutz
  cat > /etc/fail2ban/jail.local <<EOF
[sshd]
enabled  = true
port     = ssh
filter   = sshd
logpath  = /var/log/auth.log
maxretry = 3
bantime  = 3600

[nginx-4xx]
enabled  = true
port     = http,https
filter   = nginx-4xx
logpath  = /var/log/nginx/*.access.log
maxretry = 30
bantime  = 3600
findtime = 600

[nginx-scan]
enabled  = true
port     = http,https
filter   = nginx-scan
logpath  = /var/log/nginx/*.access.log
maxretry = 3
bantime  = 86400
findtime = 3600
EOF

  # fail2ban Filter: zu viele 4xx-Fehler
  mkdir -p /etc/fail2ban/filter.d
  cat > /etc/fail2ban/filter.d/nginx-4xx.conf <<'EOF'
[Definition]
failregex = ^<HOST> .+ "(GET|POST|HEAD|PUT|DELETE|OPTIONS|PATCH) .+ HTTP/\d\.\d" 4[0-9]{2} .+$
ignoreregex = ^<HOST> .+ "GET /static/ .+ 404 .+$
EOF

  # fail2ban Filter: Scanner/Bots die typische WordPress/PHP-Pfade anfragen
  cat > /etc/fail2ban/filter.d/nginx-scan.conf <<'EOF'
[Definition]
failregex = ^<HOST> .+ "(GET|POST) /(?:wp-admin|wp-login\.php|xmlrpc\.php|phpMyAdmin|phpmyadmin|\.env|\.git/|shell\.php|eval\.php|upload\.php|backdoor|webshell|config\.php|setup\.php|install\.php|admin\.php|cms/|joomla|drupal)
ignoreregex =
EOF

  systemctl enable --now fail2ban
  echo "✅ fail2ban aktiviert (SSH + nginx-4xx + nginx-scan)"
else
  echo "⏭️  fail2ban übersprungen"
fi

mark_done "pkgs_installed"
else
  echo "⏭️  Pakete bereits installiert - überspringe"
fi  # end pkgs_installed

# -------------------------------------------------------------------
# Firewall Grundkonfiguration (ufw) — einmalig für den gesamten Server
# -------------------------------------------------------------------
# Bedingung: noch nicht erledigt ODER ufw fehlt trotz gesetztem Flag (z.B.
# wegen eines Fehlers beim letzten Lauf oder gesetztem State ohne Installation)
if ! is_done "ufw_base_done" || ! command -v ufw >/dev/null 2>&1; then
echo "🔒 Konfiguriere Firewall (ufw)..."

# ufw installieren falls nicht vorhanden
if ! command -v ufw >/dev/null 2>&1; then
  echo "  📦 Installiere ufw..."
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y ufw
  if ! command -v ufw >/dev/null 2>&1; then
    echo "  ❌ ufw konnte nicht installiert werden — Firewall wird übersprungen"
    mark_done "ufw_base_done"
    # shellcheck disable=SC2209
    _UFW_SKIP=1
  fi
fi

if [ "${_UFW_SKIP:-0}" != "1" ]; then

# iptables-Module laden (LXC-Container benötigen dies manchmal)
modprobe iptable_filter   2>/dev/null || true
modprobe ip6table_filter  2>/dev/null || true
modprobe iptable_nat      2>/dev/null || true

# Prüfen ob ufw in diesem Container wirklich funktioniert
_UFW_STATUS_OUT=$(ufw status 2>&1)
_UFW_STATUS_RC=$?
if [ $_UFW_STATUS_RC -ne 0 ] && echo "$_UFW_STATUS_OUT" | grep -qi "error\|iptables\|failed"; then
  echo "  ⚠️  ufw nicht verfügbar in diesem Container (LXC ohne iptables-Unterstützung)"
  echo "     Ausgabe: $_UFW_STATUS_OUT"
  echo "     Firewall-Konfiguration wird übersprungen."
  mark_done "ufw_base_done"
else

# Regeln setzen wenn ufw noch nicht aktiv ist (verhindert Überschreiben bei Re-Installation)
if ! ufw status | grep -q "Status: active"; then
  echo "  🔧 Setze Basis-Firewall-Regeln..."

  ufw --force reset 2>/dev/null || true

  # Standard: eingehend verweigern, ausgehend erlauben
  ufw default deny incoming
  ufw default allow outgoing

  # SSH — unbedingt zuerst, sonst wird man ausgesperrt!
  ufw allow 22/tcp comment 'SSH'
  echo "  ✅ Port 22 (SSH) erlaubt"

  # HTTP + HTTPS für nginx
  ufw allow 80/tcp  comment 'HTTP nginx'
  ufw allow 443/tcp comment 'HTTPS nginx'
  echo "  ✅ Port 80/443 (HTTP/HTTPS) erlaubt"

  # Loopback immer erlauben (Gunicorn kommuniziert über 127.0.0.1)
  ufw allow in on lo comment 'Loopback intern'

  # Alle Gunicorn/Internalports 8000-8999 extern sperren.
  # Der Manager (Gunicorn auf 8888) ist nur über nginx Port 443 erreichbar —
  # direkter Zugriff auf 8888 ist nicht notwendig und sicherheitsrelevant.
  ufw deny 8000:8999/tcp comment 'Gunicorn-Ports (intern only)'
  echo "  ✅ Ports 8000-8999 (inkl. 8888 Gunicorn/Manager) extern gesperrt"

  # Firewall aktivieren
  ufw --force enable
  echo "  ✅ Firewall aktiviert"
  echo
  ufw status verbose 2>/dev/null || ufw status
else
  echo "  ✅ Firewall bereits aktiv — Basis-Regeln werden nicht überschrieben"
  ufw status numbered 2>/dev/null | head -20
fi

fi  # end ufw available check

mark_done "ufw_base_done"
fi  # end _UFW_SKIP

else
  echo "⏭️  Firewall-Grundkonfiguration bereits erledigt - überspringe"
fi  # end ufw_base_done

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
if [ -n "${APPUSER_PASS:-}" ]; then
  echo "$APPUSER:$APPUSER_PASS" | chpasswd
  echo "✅ Passwort für $APPUSER gesetzt"
else
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
fi

# SSH-Verzeichnis erstellen
echo "🔑 Erstelle SSH-Key für Benutzer $APPUSER..."
mkdir -p "/home/$APPUSER/.ssh"
chown "$APPUSER:$APPUSER" "/home/$APPUSER/.ssh"
chmod 700 "/home/$APPUSER/.ssh"

# SSH-Key erstellen (immer ohne Passphrase — Passphrase ist für WinSCP/PuTTY nutzlos bei Diensten)
ssh-keygen -t ed25519 -C "${APPUSER}@$(hostname -f 2>/dev/null || echo 'server')" \
  -f "$SSH_KEY_PATH" -N "" -q

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

# -------------------------------------------------------------------
# Pro-Projekt GitHub Deploy-Key
# -------------------------------------------------------------------
GITHUB_DEPLOY_KEY="${GITHUB_DEPLOY_KEY:-/root/.ssh/djmanager_keys/${PROJECTNAME}_ed25519}"
GITHUB_SSH_OPTS="-o ConnectTimeout=30"

if [[ "$USE_GITHUB" == "true" ]]; then
  echo "📦 GitHub Repository erkannt: $GITHUB_REPO_URL"

  # Pro-Projekt Deploy-Key – wird normalerweise vom Manager vorab erstellt
  # (GITHUB_DEPLOY_KEY und DEPLOY_KEY_ID kommen als Env-Var)
  if [ ! -f "$GITHUB_DEPLOY_KEY" ]; then
    echo "🔑 Erstelle GitHub Deploy-Key für Projekt '${PROJECTNAME}'..."
    mkdir -p /root/.ssh/djmanager_keys
    chmod 700 /root/.ssh/djmanager_keys
    ssh-keygen -t ed25519 -C "deploy-${PROJECTNAME}@$(hostname -f 2>/dev/null || echo 'server')" \
      -f "$GITHUB_DEPLOY_KEY" -N "" -q
    chmod 600 "$GITHUB_DEPLOY_KEY"
    chmod 644 "${GITHUB_DEPLOY_KEY}.pub"
    echo "✅ Deploy-Key erstellt: $GITHUB_DEPLOY_KEY"
  else
    echo "✅ Deploy-Key bereits vorhanden: $GITHUB_DEPLOY_KEY"
  fi

  echo
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "🔐 ÖFFENTLICHER GITHUB DEPLOY-KEY für Projekt '${PROJECTNAME}':"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  cat "${GITHUB_DEPLOY_KEY}.pub"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "➡️  Manager → Projekt '${PROJECTNAME}' → 'GitHub Deploy-Key' → Key kopieren"
  echo "   Dann: GitHub → Repo → Settings → Deploy keys → Add deploy key"
  echo

  # Retry-Schleife: Key testen, bei Fehler nochmal warten
  # Im NONINTERACTIVE-Modus: auf Confirm-File warten statt Terminal-Eingabe
  _CONFIRM_FILE="/tmp/djmanager_installs/${PROJECTNAME}_github_confirm"
  mkdir -p /tmp/djmanager_installs

  _wait_for_github_confirm() {
    if [ "$NONINTERACTIVE" = "true" ]; then
      echo "##WAIT_GITHUB_CONFIRM##"
      echo "⏳ Warte auf Bestätigung im Web-Interface..."
      echo "   → Seite oben: Deploy-Key kopieren → GitHub hinterlegen → Button klicken"
      # Altes Confirm-File löschen falls vorhanden
      rm -f "$_CONFIRM_FILE"
      local _waited=0
      while [ ! -f "$_CONFIRM_FILE" ]; do
        sleep 3
        _waited=$((_waited + 3))
        if [ $((_waited % 60)) -eq 0 ]; then
          echo "   ⏳ Warte seit ${_waited}s auf Bestätigung..."
        fi
      done
      rm -f "$_CONFIRM_FILE"
    else
      _read -p "Key zu GitHub hinzugefügt? Verbindung testen [J] / abbrechen [n]: " CONFIRM
      [[ ! "${CONFIRM:-J}" =~ ^[Jj]$ ]] && echo "❌ Abbruch." && exit 1
    fi
  }

  while true; do
    _wait_for_github_confirm

    # known_hosts für github.com (als root)
    ssh-keyscan -H github.com >> /root/.ssh/known_hosts 2>/dev/null || true
    chmod 644 /root/.ssh/known_hosts

    # SSH-Verbindung zu GitHub testen
    echo "🔍 Teste SSH-Verbindung zu GitHub (Port 22)..."
    SSH_TEST_RESULT=$(timeout 15 ssh -i "$GITHUB_DEPLOY_KEY" -o IdentitiesOnly=yes \
      -o StrictHostKeyChecking=no -o ConnectTimeout=10 -T git@github.com 2>&1 || true)

    if echo "$SSH_TEST_RESULT" | grep -q "successfully authenticated"; then
      echo "✅ GitHub SSH Port 22 erfolgreich verbunden"
      break
    fi

    echo "⚠️  SSH Port 22 nicht erreichbar — teste Port 443 (ssh.github.com)..."
    ssh-keyscan -H -p 443 ssh.github.com >> /root/.ssh/known_hosts 2>/dev/null || true
    SSH_TEST_443=$(timeout 15 ssh -i "$GITHUB_DEPLOY_KEY" -o IdentitiesOnly=yes \
      -o StrictHostKeyChecking=no -o ConnectTimeout=10 -p 443 -T git@ssh.github.com 2>&1 || true)

    if echo "$SSH_TEST_443" | grep -q "successfully authenticated"; then
      echo "✅ GitHub SSH über Port 443 erreichbar - erstelle SSH-Config..."
      cat >> /root/.ssh/config <<SSHCONFIGEOF

Host github.com
    Hostname ssh.github.com
    Port 443
    User git
    IdentityFile ${GITHUB_DEPLOY_KEY}
    IdentitiesOnly yes
SSHCONFIGEOF
      chmod 600 /root/.ssh/config
      echo "✅ SSH-Config für GitHub Port 443 erstellt"
      GITHUB_SSH_OPTS="-o ConnectTimeout=30 -p 443"
      break
    fi

    echo "❌ SSH zu GitHub fehlgeschlagen (Port 22 + 443)"
    echo "   → Deploy-Key in GitHub hinterlegen und nochmal bestätigen"
    echo "##WAIT_GITHUB_CONFIRM##"
  done
else
  GITHUB_DEPLOY_KEY="${GITHUB_DEPLOY_KEY:-/root/.ssh/djmanager_keys/${PROJECTNAME}_ed25519}"
  echo "⏭️  GitHub nicht genutzt — überspringe GitHub-Setup"
fi

# -------------------------------------------------------------------
# PostgreSQL / MySQL Installation (lokal)
# -------------------------------------------------------------------
if ! is_done "db_setup"; then
cd /tmp

if [ "${DBTYPE}" != "sqlite" ] && [ "${DBMODE:-}" = "1" ]; then
  echo "🗄️  Installiere lokale ${DBTYPE^^} Datenbank..."

  if [ "$DBTYPE" = "postgresql" ]; then
    echo "📦 Installiere PostgreSQL..."
    _apt_install $DB_PACKAGE_LOCAL
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
    echo "📦 Installiere MariaDB..."
    _apt_install $DB_PACKAGE_LOCAL
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
  _apt_install $DB_PACKAGE_CLIENT
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
# Django Setup (ZIP / GitHub / Neues Projekt)
# -------------------------------------------------------------------
if [[ "$SOURCE_TYPE" == "zip" ]]; then
  echo "📦 Entpacke ZIP nach $APPDIR..."
  _ZIP_TMP="/tmp/_dmd_zip_${PROJECTNAME}_$$"
  mkdir -p "$_ZIP_TMP"
  unzip -q "$UPLOAD_ZIP_PATH" -d "$_ZIP_TMP" 2>/dev/null \
    || { echo "❌ FEHLER: ZIP konnte nicht entpackt werden"; exit 1; }

  # Erkennen ob GitHub-Style (einzelnes Top-Level-Verzeichnis) oder flache Struktur
  _TOP_COUNT=$(ls -1 "$_ZIP_TMP" | wc -l)
  if [ "$_TOP_COUNT" -eq 1 ]; then
    _TOP_ITEM="$_ZIP_TMP/$(ls -1 "$_ZIP_TMP")"
    if [ -d "$_TOP_ITEM" ]; then
      cp -r "$_TOP_ITEM/." "$APPDIR/"
    else
      cp -r "$_ZIP_TMP/." "$APPDIR/"
    fi
  else
    cp -r "$_ZIP_TMP/." "$APPDIR/"
  fi
  rm -rf "$_ZIP_TMP"
  chown -R "$APPUSER:$APPUSER" "$APPDIR"
  echo "✅ ZIP entpackt nach $APPDIR"

  # Django-Modul erkennen
  DJANGO_MODULE=$(find "$APPDIR" -maxdepth 2 -name "wsgi.py" ! -path "*/.venv/*" 2>/dev/null | head -1 | xargs -I{} dirname {} | xargs -I{} basename {} 2>/dev/null)
  [ -z "$DJANGO_MODULE" ] && DJANGO_MODULE="core"
  echo "📌 Django-Modul erkannt: $DJANGO_MODULE"

  # Admin-URL auf /djadmin/ setzen
  URLS_FILE="$APPDIR/$DJANGO_MODULE/urls.py"
  if [ -f "$URLS_FILE" ]; then
    sed -i "s|path('admin/', admin.site.urls)|path('djadmin/', admin.site.urls)|g" "$URLS_FILE"
    sed -i 's|path("admin/", admin.site.urls)|path("djadmin/", admin.site.urls)|g' "$URLS_FILE"
    echo "✅ Admin-URL auf /djadmin/ gesetzt in $URLS_FILE"
  fi

  # Virtual Environment + Dependencies
  echo "🐍 Erstelle Python Virtual Environment..."
  su - "$APPUSER" -s /bin/bash <<EOF
set -e
cd "$APPDIR"
python3 -m venv .venv
source .venv/bin/activate
pip install --no-cache-dir --upgrade pip
pip install --no-cache-dir --prefer-binary django gunicorn python-dotenv pillow

if [ "$DBTYPE" = "postgresql" ]; then
  pip install --no-cache-dir --prefer-binary "psycopg[binary]"
elif [ "$DBTYPE" = "mysql" ]; then
  pip install --no-cache-dir --prefer-binary mysqlclient
fi

if [ -f "$APPDIR/requirements.txt" ]; then
  echo "📦 Installiere requirements.txt..."
  pip install --no-cache-dir --prefer-binary -r "$APPDIR/requirements.txt"
fi
EOF

elif [[ "$USE_GITHUB" == "true" ]]; then
  echo "📥 Klonen GitHub Repository: $GITHUB_REPO_URL"
  
  # Git clone als root mit globalem Deploy-Key
  GIT_SSH_COMMAND="ssh -i ${GITHUB_DEPLOY_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new ${GITHUB_SSH_OPTS}" \
    git clone "$GITHUB_REPO_URL" "$APPDIR"
  chown -R "$APPUSER:$APPUSER" "$APPDIR"
  
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
pip install --no-cache-dir --upgrade pip
# --prefer-binary: verwendet fertige Binary-Wheels statt Kompilierung (spart ~300 MB RAM-Peak)
pip install --no-cache-dir --prefer-binary django gunicorn python-dotenv pillow

# DB-spezifische Pakete
if [ "$DBTYPE" = "postgresql" ]; then
  pip install --no-cache-dir --prefer-binary "psycopg[binary]"
elif [ "$DBTYPE" = "mysql" ]; then
  pip install --no-cache-dir --prefer-binary mysqlclient
fi

# Requirements installieren (falls vorhanden)
if [ -f "$APPDIR/requirements.txt" ]; then
  echo "📦 Installiere requirements.txt..."
  pip install --no-cache-dir --prefer-binary -r "$APPDIR/requirements.txt"
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
pip install --no-cache-dir --upgrade pip
# --prefer-binary: verwendet fertige Binary-Wheels statt Kompilierung (spart ~300 MB RAM-Peak)
pip install --no-cache-dir --prefer-binary django gunicorn python-dotenv pillow

# DB-spezifische Pakete
if [ "$DBTYPE" = "postgresql" ]; then
  pip install --no-cache-dir --prefer-binary "psycopg[binary]"
elif [ "$DBTYPE" = "mysql" ]; then
  pip install --no-cache-dir --prefer-binary mysqlclient
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
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
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

if [ -n "${DJANGO_ADMIN_PASS:-}" ]; then
  # Noninteractive (Web-UI)
  DJANGO_ADMIN_USER="${DJANGO_ADMIN_USER:-admin}"
  DJANGO_ADMIN_EMAIL="${DJANGO_ADMIN_EMAIL:-admin@localhost}"
  echo "✅ Django Admin '${DJANGO_ADMIN_USER}' wird angelegt..."
else
  _read -p "Admin-Username [admin]: " DJANGO_ADMIN_USER
  DJANGO_ADMIN_USER="${DJANGO_ADMIN_USER:-admin}"
  _read -p "Admin-Email [admin@localhost]: " DJANGO_ADMIN_EMAIL
  DJANGO_ADMIN_EMAIL="${DJANGO_ADMIN_EMAIL:-admin@localhost}"
  while true; do
    _read -s -p "Admin-Passwort: " DJANGO_ADMIN_PASS; echo
    [ -z "$DJANGO_ADMIN_PASS" ] && echo "❌ Passwort darf nicht leer sein." && continue
    _read -s -p "Admin-Passwort bestätigen: " DJANGO_ADMIN_PASS2; echo
    [ "$DJANGO_ADMIN_PASS" = "$DJANGO_ADMIN_PASS2" ] && break
    echo "❌ Passwörter stimmen nicht überein. Erneut versuchen."
  done
fi

su - "$APPUSER" -s /bin/bash -c "cd $APPDIR && source .venv/bin/activate && \
  DJANGO_SUPERUSER_PASSWORD='$DJANGO_ADMIN_PASS' \
  python manage.py createsuperuser --noinput \
    --username '$DJANGO_ADMIN_USER' \
    --email '$DJANGO_ADMIN_EMAIL'" 2>/dev/null || \
su - "$APPUSER" -s /bin/bash -c "cd $APPDIR && source .venv/bin/activate && \
  python manage.py shell -c \
  \"from django.contrib.auth.models import User; \
  User.objects.filter(username='$DJANGO_ADMIN_USER').delete(); \
  User.objects.create_superuser('$DJANGO_ADMIN_USER','$DJANGO_ADMIN_EMAIL','$DJANGO_ADMIN_PASS')\""

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

# Globales reqtime-Logformat + Rate-Limit-Zones einmalig anlegen (idempotent)
if [ ! -f /etc/nginx/conf.d/reqtime_log.conf ]; then
  cat > /etc/nginx/conf.d/reqtime_log.conf <<'LOGFMT'
log_format reqtime '$remote_addr - $remote_user [$time_local] "$request" '
                   '$status $body_bytes_sent "$http_referer" '
                   '"$http_user_agent" $request_time';
LOGFMT
  echo "✅ Nginx reqtime-Logformat angelegt"
fi

if [ ! -f /etc/nginx/conf.d/ratelimit.conf ]; then
  cat > /etc/nginx/conf.d/ratelimit.conf <<'RATELIMIT'
# Rate Limiting Zonen (global, für alle Django-Projekte)
limit_req_zone $binary_remote_addr zone=django_login:10m rate=5r/m;
limit_req_zone $binary_remote_addr zone=django_api:10m   rate=30r/s;
limit_req_zone $binary_remote_addr zone=django_general:20m rate=120r/m;
limit_req_status 429;
RATELIMIT
  echo "✅ Nginx Rate-Limit-Zonen angelegt"
fi

# Self-Signed SSL-Zertifikat bereitstellen (einmalig, geteilt für alle Sites)
_generate_selfsigned_cert

# NGINX_PORT: HTTPS-Port für diese App (Gunicorn intern auf 127.0.0.1:GUNICORN_PORT)
_NGINX_SERVER_NAMES="$(echo "$ALLOWED_HOSTS" | tr ',' ' ')"
cat > /etc/nginx/sites-available/$PROJECTNAME <<EOF
server {
    listen ${NGINX_PORT} ssl;
    server_name ${_NGINX_SERVER_NAMES};
    client_max_body_size 50M;

    ssl_certificate     ${_SSL_CERT};
    ssl_certificate_key ${_SSL_KEY};
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;

    # Per-Projekt Access- und Error-Log
    access_log /var/log/nginx/${PROJECTNAME}.access.log reqtime;
    error_log  /var/log/nginx/${PROJECTNAME}.error.log;

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
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy "geolocation=(), microphone=(), camera=(), payment=(), usb=()" always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; frame-ancestors 'none';" always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # Bekannte Angriffspfade blockieren
    location ~* ^/(wp-admin|wp-login\.php|xmlrpc\.php|phpmyadmin|\.env|\.git|shell\.php|eval\.php) {
        return 404;
    }

    # Rate Limiting für Login/Admin-Bereich
    location ~* ^/(login|djadmin|accounts/login|api/auth) {
        limit_req zone=django_login burst=5 nodelay;
        add_header Retry-After 60 always;
        proxy_pass http://127.0.0.1:${GUNICORN_PORT};
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

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
        limit_req zone=django_general burst=30 nodelay;
        proxy_pass http://127.0.0.1:${GUNICORN_PORT};
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
EOF

ln -sf /etc/nginx/sites-available/$PROJECTNAME /etc/nginx/sites-enabled/$PROJECTNAME
# Default-Site nur entfernen wenn es die einzige aktive Site ist
# (mehrere Projekte auf einem Server — andere Sites nicht anfassen)
_ACTIVE_SITES=$(ls /etc/nginx/sites-enabled/ 2>/dev/null | grep -v "^default$" | wc -l)
[ "$_ACTIVE_SITES" -eq 0 ] && rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

if ! nginx -t; then
  echo "❌ FEHLER: Nginx Konfiguration ungültig!"
  exit 1
fi

systemctl restart nginx

# UFW: NGINX_PORT für diese App freigeben (ist nicht 80/443 — jede App hat eigenen Port)
if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "${NGINX_PORT}/tcp" comment "nginx ${PROJECTNAME}"
  echo "  ✅ UFW: Port ${NGINX_PORT} für ${PROJECTNAME} geöffnet"
fi

mark_done "nginx_done"

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔒 Direkter Zugriff (Self-Signed SSL):"
echo "   https://${LOCAL_IP}:${NGINX_PORT}/"
echo
echo "🔀 Externer Reverse Proxy → Ziel:"
echo "   https://${LOCAL_IP}:${NGINX_PORT}  (SSL verify off / insecure)"
echo "   Gunicorn intern: 127.0.0.1:${GUNICORN_PORT}"
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
  # Ersten ALLOWED_HOST als Primary-Domain ermitteln
  _PRIMARY_HOST=$(echo "${ALLOWED_HOSTS:-$LOCAL_IP}" | cut -d, -f1 | tr -d ' ')
  cat > /etc/django-servers.d/${PROJECTNAME}.conf <<REGEOF
PROJECTNAME="${PROJECTNAME}"
APPDIR="${APPDIR}"
APPUSER="${APPUSER}"
MODE="${MODE}"
DEBUG="${DEBUG_VALUE}"
GUNICORN_PORT="${GUNICORN_PORT}"
NGINX_PORT="${NGINX_PORT}"
DBTYPE="${DBTYPE}"
DBNAME="${DBNAME:-}"
DBHOST="${DBHOST:-}"
DBPORT="${DBPORT:-}"
LOCAL_IP="${LOCAL_IP}"
HOSTNAME_FQDN="${HOSTNAME_FQDN}"
PRIMARY_HOST="${LOCAL_IP}"
GITHUB_REPO_URL="${GITHUB_REPO_URL:-}"
DEPLOY_KEY_ID="${DEPLOY_KEY_ID:-}"
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
# Firewall (ufw) — Gunicorn-Port nach außen sperren
# -------------------------------------------------------------------
if ! is_done "firewall_done"; then
if command -v ufw >/dev/null 2>&1; then
  echo "🔒 Konfiguriere Firewall für Gunicorn-Port $GUNICORN_PORT..."
  # Gunicorn-Port von außen sperren (läuft ohnehin auf 127.0.0.1, Doppelabsicherung)
  ufw deny "${GUNICORN_PORT}/tcp" comment "Gunicorn ${PROJECTNAME} (intern only)" 2>/dev/null || true
  ufw reload 2>/dev/null || true
  echo "  ✅ Port $GUNICORN_PORT extern gesperrt (nginx → 127.0.0.1:${GUNICORN_PORT} intern OK)"
else
  echo "  ℹ️  ufw nicht gefunden — Firewall muss manuell konfiguriert werden"
  echo "     Empfehlung: apt install ufw && ufw deny ${GUNICORN_PORT}/tcp"
fi
mark_done "firewall_done"
else
  echo "⏭️  Firewall bereits konfiguriert - überspringe"
fi  # end firewall_done

# -------------------------------------------------------------------
# Sudoers + Update-Skript
# -------------------------------------------------------------------
if ! is_done "scripts_done"; then
# sudoers: App-User darf Service-Befehle ohne Passwort — nur wenn sudo verfügbar
if command -v sudo >/dev/null 2>&1 && [ -d /etc/sudoers.d ]; then
  echo "🔐 Konfiguriere sudoers für $APPUSER..."
  cat > /etc/sudoers.d/${PROJECTNAME}-service <<EOF
$APPUSER ALL=NOPASSWD: /bin/systemctl restart $PROJECTNAME, /bin/systemctl status $PROJECTNAME, /bin/systemctl reload $PROJECTNAME, /bin/journalctl -u $PROJECTNAME*
EOF
  chmod 440 /etc/sudoers.d/${PROJECTNAME}-service
else
  echo "ℹ️  sudo nicht verfügbar (LXC?) — sudoers-Eintrag übersprungen"
fi

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
  GITHUB_DEPLOY_KEY="${GITHUB_DEPLOY_KEY}"
  # safe.directory: verhindert "dubious ownership" Fehler wenn root
  # ein Repo eines anderen Benutzers (\$APPUSER) pullt
  git config --global --add safe.directory "\$APPDIR" 2>/dev/null || true
  git config --global pull.rebase false 2>/dev/null || true
  git -C "\$APPDIR" stash --quiet 2>/dev/null || true
  if [ -f "\$GITHUB_DEPLOY_KEY" ]; then
    GIT_SSH_COMMAND="ssh -i \$GITHUB_DEPLOY_KEY -o IdentitiesOnly=yes -o ConnectTimeout=30" \
      git -C "\$APPDIR" pull --ff-only 2>/dev/null \
      || GIT_SSH_COMMAND="ssh -i \$GITHUB_DEPLOY_KEY -o IdentitiesOnly=yes -o ConnectTimeout=30" \
         git -C "\$APPDIR" pull --no-rebase
  else
    git -C "\$APPDIR" pull --ff-only 2>/dev/null \
      || git -C "\$APPDIR" pull --no-rebase
  fi
else
  echo "⏭️  Kein Git-Repository gefunden (überspringe git pull)"
fi

# Requirements installieren
if [ -f "\$APPDIR/requirements.txt" ]; then
  echo "📦 Installiere Requirements..."
  su - "\$APPUSER" -s /bin/bash -c "cd \$APPDIR && source .venv/bin/activate && pip install --no-cache-dir -r requirements.txt"
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

# Service neustarten (direkt als root — kein sudo nötig)
echo "🔄 Neustart Service..."
systemctl restart "\$SERVICE"

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

# Maximal 5 Backups behalten (älteste löschen)
ls -t "\$BACKUP_DIR"/*.tar.gz 2>/dev/null | tail -n +6 | xargs -r rm -f

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

# _read: überspringt Prompts wenn NONINTERACTIVE=true
_read() { [ "\${NONINTERACTIVE:-false}" = "true" ] && return 0; read "\$@"; }

PROJECT="${PROJECTNAME}"
APPDIR="${APPDIR}"
APPUSER="${APPUSER}"
DBTYPE="${DBTYPE}"
DBNAME="${DBNAME:-}"
DBUSER_DB="${DBUSER:-}"
NGINX_PORT="${NGINX_PORT}"
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

# UFW-Regel für NGINX_PORT entfernen
if [ -n "\${NGINX_PORT:-}" ] && command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
  ufw delete allow "\${NGINX_PORT}/tcp" 2>/dev/null || true
  echo "  ✅ UFW: Port \${NGINX_PORT} für \${PROJECT} geschlossen"
fi

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

echo "  🔀 Reverse Proxy Konfiguration (optional):"
echo "     nginx auf diesem Server hört auf Port 80, leitet per server_name."
echo "     Im Reverse Proxy je Domain einen Eintrag anlegen:"
for _conf in "$CONF_DIR"/*.conf; do
  [ -f "$_conf" ] || continue
  _LIP=$(grep '^LOCAL_IP=' "$_conf" | cut -d= -f2 | tr -d '"')
  _PHOST=$(grep '^PRIMARY_HOST=' "$_conf" | cut -d= -f2 | tr -d '"')
  _PROJ=$(grep '^PROJECTNAME=' "$_conf" | cut -d= -f2 | tr -d '"')
  printf "     %-32s →  http://%s:80\n" "${_PHOST:-$_PROJ}" "${_LIP:-$_IP}"
done
echo "     ⚠️  'Pass Host Header' / 'Preserve Host' im Reverse Proxy aktivieren!"
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
echo "🔒 Django Admin: https://${LOCAL_IP}:${NGINX_PORT}/djadmin/"
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔒 Direkter Zugriff (Self-Signed SSL):"
echo "   https://${LOCAL_IP}:${NGINX_PORT}/"
echo
echo "🔀 Externer Reverse Proxy → Ziel:"
echo "   https://${LOCAL_IP}:${NGINX_PORT}  (SSL verify off / insecure erlaubt)"
echo "   nginx-Config: /etc/nginx/sites-available/${PROJECTNAME}"
echo "   Gunicorn intern: 127.0.0.1:${GUNICORN_PORT}"
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

  # nginx sicherstellen (falls Manager standalone installiert wird)
  if ! command -v nginx >/dev/null 2>&1; then
    echo "📦 nginx nicht gefunden — wird installiert..."
    _apt_install nginx
  fi
  systemctl enable nginx 2>/dev/null || true
  systemctl start  nginx 2>/dev/null || true

  # Server-IP automatisch ermitteln (kein Hostname-Prompt)
  _MGR_DEFAULT_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/{print $7;exit}')"
  [ -z "${_MGR_DEFAULT_IP:-}" ] && _MGR_DEFAULT_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  # Alle Interface-IPs (ohne IPv6-only)
  _ALL_MGR_IPS="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -Ev '^$|^::' | paste -sd, -)"

  echo "📁 Manager-Verzeichnis:  $_MANAGER_DIR"
  echo "🔌 Manager-Gunicorn:     127.0.0.1:$_MANAGER_PORT (intern)"
  echo "🔒 nginx HTTPS:          https://${_MGR_DEFAULT_IP}:443/"
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

  # Python venv + Abhängigkeiten — in kleinen Gruppen (OOM-Schutz)
  echo "🐍 Installiere Python-Abhängigkeiten für Manager..."
  _apt_install python3 python3-venv python3-pip
  _apt_install build-essential

  # Versionsspezifisches venv-Paket installieren (z.B. python3.11-venv auf Debian/Ubuntu)
  _PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
  if [ -n "$_PY_VER" ]; then
    _apt_install "python${_PY_VER}-venv" 2>/dev/null || sync
  fi

  # Sicherstellen dass venv wirklich verfügbar ist
  if ! python3 -m venv --help >/dev/null 2>&1; then
    echo "❌ python3-venv ist nicht verfügbar. Bitte manuell installieren:"
    echo "   apt install python3-venv python${_PY_VER:+${_PY_VER}-}venv"
    exit 1
  fi

  echo "🐍 Erstelle Python venv für Manager..."
  python3 -m venv "$_MANAGER_DIR/venv"
  "$_MANAGER_DIR/venv/bin/pip" install --no-cache-dir --upgrade pip -q
  "$_MANAGER_DIR/venv/bin/pip" install --no-cache-dir --prefer-binary -r "$_MANAGER_DIR/requirements.txt" -q
  echo "✅ Python-Abhängigkeiten installiert"

  # .env für Manager
  _MANAGER_SECRET="$(openssl rand -hex 32)"

  # ALLOWED_HOSTS: alle lokalen IPs + Loopback (kein Hostname — port-basierter Zugriff via nginx)
  _MGR_ALLOWED_HOSTS="${_ALL_MGR_IPS},${_MGR_DEFAULT_IP},127.0.0.1,localhost"
  _MGR_ALLOWED_HOSTS="$(echo "$_MGR_ALLOWED_HOSTS" | tr ',' '\n' | grep -v '^$' | sort -u | paste -sd, -)"

  # CSRF_TRUSTED_ORIGINS: http + https für alle IPs, Standard-Ports (80/443)
  # IPv6-Adressen (enthalten ':') müssen in eckigen Klammern stehen
  _MGR_CSRF_ORIGINS=""
  for _ip in $(echo "${_ALL_MGR_IPS},${_MGR_DEFAULT_IP},127.0.0.1" | tr ',' '\n' | grep -v '^$' | sort -u); do
    if echo "$_ip" | grep -q ':'; then _ip_h="[${_ip}]"; else _ip_h="$_ip"; fi
    _MGR_CSRF_ORIGINS="${_MGR_CSRF_ORIGINS},http://${_ip_h},https://${_ip_h}"
    _MGR_CSRF_ORIGINS="${_MGR_CSRF_ORIGINS},http://${_ip_h}:${_MANAGER_PORT},https://${_ip_h}:${_MANAGER_PORT}"
    _MGR_CSRF_ORIGINS="${_MGR_CSRF_ORIGINS},http://${_ip_h}:80,https://${_ip_h}:443"
  done
  _MGR_CSRF_ORIGINS="$(echo "$_MGR_CSRF_ORIGINS" | tr ',' '\n' | grep -v '^$' | sort -u | paste -sd, -)"

  # .env zeilenweise mit printf schreiben
  {
    printf 'SECRET_KEY=%s\n'            "${_MANAGER_SECRET}"
    printf 'DEBUG=False\n'
    printf 'ALLOWED_HOSTS=%s\n'         "${_MGR_ALLOWED_HOSTS}"
    printf 'CSRF_TRUSTED_ORIGINS=%s\n'  "${_MGR_CSRF_ORIGINS}"
    printf 'USE_X_FORWARDED_HOST=False\n'
    printf 'MANAGER_PORT=%s\n'          "${_MANAGER_PORT}"
    printf 'INSTALL_SCRIPT=%s\n'        "${_SCRIPT_DIR}/Installv2.sh"
    printf 'REGISTRY_DIR=/etc/django-servers.d\n'
  } > "$_MANAGER_DIR/.env"
  chmod 600 "$_MANAGER_DIR/.env"

  # Datenbankmigrationen (inkl. auth-Tabellen)
  "$_MANAGER_DIR/venv/bin/python" "$_MANAGER_DIR/manage.py" migrate 2>/dev/null || \
    "$_MANAGER_DIR/venv/bin/python" "$_MANAGER_DIR/manage.py" migrate --run-syncdb

  # Admin-Benutzer anlegen
  echo
  echo "👤 Manager Admin-Benutzer anlegen"
  _read -p "Admin-Benutzername [admin]: " _ADMIN_USER
  _ADMIN_USER="${_ADMIN_USER:-admin}"
  while true; do
    _read -s -p "Admin-Passwort: " _ADMIN_PASS; echo
    [ -z "$_ADMIN_PASS" ] && echo "❌ Passwort darf nicht leer sein." && continue
    _read -s -p "Admin-Passwort bestätigen: " _ADMIN_PASS2; echo
    [ "$_ADMIN_PASS" = "$_ADMIN_PASS2" ] && break
    echo "❌ Passwörter stimmen nicht überein."
  done
  "$_MANAGER_DIR/venv/bin/python" "$_MANAGER_DIR/manage.py" shell -c \
    "from django.contrib.auth.models import User; User.objects.filter(username='${_ADMIN_USER}').delete(); User.objects.create_superuser('${_ADMIN_USER}', '', '${_ADMIN_PASS}')"
  echo "✅ Admin-Benutzer '${_ADMIN_USER}' angelegt (is_staff=True)"

  # Statische Dateien
  "$_MANAGER_DIR/venv/bin/python" "$_MANAGER_DIR/manage.py" collectstatic --noinput -v 0

  # Manager Self-Update Script
  cat > /usr/local/bin/djmanager_update.sh <<MGRUPDEOF
#!/bin/bash
set -euo pipefail
MANAGER_DIR="${_MANAGER_DIR}"
SCRIPT_DIR="${_SCRIPT_DIR}"
SERVICE="djmanager"

echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║              DjangoMultiDeploy Manager — UPDATE               ║"
echo "╚═══════════════════════════════════════════════════════════════╝"

# Git Pull des gesamten Repos
if [ -d "\$SCRIPT_DIR/.git" ]; then
  echo "📥 Git Pull..."
  GITHUB_DEPLOY_KEY="/root/.ssh/djmanager_github_ed25519"
  git config --global --add safe.directory "\$SCRIPT_DIR" 2>/dev/null || true
  git config --global pull.rebase false 2>/dev/null || true
  # Lokale Änderungen stashen damit git pull nicht abbricht
  git -C "\$SCRIPT_DIR" stash --quiet 2>/dev/null || true
  if [ -f "\$GITHUB_DEPLOY_KEY" ]; then
    GIT_SSH_COMMAND="ssh -i \$GITHUB_DEPLOY_KEY -o IdentitiesOnly=yes -o ConnectTimeout=30" \
      git -C "\$SCRIPT_DIR" pull --ff-only 2>/dev/null \
      || GIT_SSH_COMMAND="ssh -i \$GITHUB_DEPLOY_KEY -o IdentitiesOnly=yes -o ConnectTimeout=30" \
         git -C "\$SCRIPT_DIR" pull --no-rebase
  else
    git -C "\$SCRIPT_DIR" pull --ff-only 2>/dev/null \
      || git -C "\$SCRIPT_DIR" pull --no-rebase
  fi
else
  echo "⏭️  Kein Git-Repository gefunden — überspringe git pull"
fi

# Neuen Manager-Code nach MANAGER_DIR synchronisieren
# .env, db.sqlite3 und venv werden NICHT überschrieben (cp, kein rsync nötig)
if [ -d "\$SCRIPT_DIR/manager" ]; then
  echo "📋 Synchronisiere Manager-Code nach \$MANAGER_DIR..."
  find "\$SCRIPT_DIR/manager" -mindepth 1 -maxdepth 1 | while read -r _item; do
    _base="\$(basename "\$_item")"
    case "\$_base" in
      .env|db.sqlite3|venv|staticfiles) continue ;;
    esac
    cp -a "\$_item" "\$MANAGER_DIR/"
  done
  echo "✅ Code synchronisiert"
else
  echo "⏭️  \$SCRIPT_DIR/manager nicht gefunden — überspringe Sync"
fi

# Python-Abhängigkeiten aktualisieren
if [ -f "\$MANAGER_DIR/requirements.txt" ]; then
  echo "📦 Installiere Requirements..."
  "\$MANAGER_DIR/venv/bin/pip" install --no-cache-dir --prefer-binary -r "\$MANAGER_DIR/requirements.txt" -q
fi

# .env: CSRF_TRUSTED_ORIGINS + ALLOWED_HOSTS aktualisieren
# (fügt Port-Varianten und .iot-Hostname hinzu falls noch fehlend)
_ENV_FILE="\$MANAGER_DIR/.env"
if [ -f "\$_ENV_FILE" ]; then
  _CUR_CSRF="\$(grep '^CSRF_TRUSTED_ORIGINS=' "\$_ENV_FILE" | cut -d= -f2-)"
  _MGR_PORT_UP="\$(grep '^MANAGER_PORT=' "\$_ENV_FILE" | cut -d= -f2- | tr -d '\"')"
  _MGR_PORT_UP="\${_MGR_PORT_UP:-8888}"
  # Hostnamen des Servers ermitteln
  _UPD_SHORT="\$(hostname -s 2>/dev/null || hostname | cut -d. -f1)"
  _UPD_FQDN="\$(hostname -f 2>/dev/null || echo '')"
  _ALL_IPS="\$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -Ev '^\$|^::' | paste -sd, -)"
  _NEEDS_FIX=0
  # Prüfen ob Port-Variante bereits vorhanden
  echo "\$_CUR_CSRF" | grep -q ":\${_MGR_PORT_UP}" || _NEEDS_FIX=1
  if [ "\$_NEEDS_FIX" = "1" ]; then
    echo "  🔧 Ergänze CSRF_TRUSTED_ORIGINS (Port :\${_MGR_PORT_UP})..."
    _NEW_CSRF="\$_CUR_CSRF"
    # Alle Hostnamen: IPs, kurz, FQDN, loopback
    for _h in \$(echo "\$_ALL_IPS" | tr ',' ' ') "\$_UPD_SHORT" "\$_UPD_FQDN" 127.0.0.1 localhost; do
      [ -z "\$_h" ] && continue
      echo "\$_NEW_CSRF" | grep -qF "http://\${_h}:\${_MGR_PORT_UP}" || \
        _NEW_CSRF="\${_NEW_CSRF},http://\${_h}:\${_MGR_PORT_UP}"
      echo "\$_NEW_CSRF" | grep -qF "http://\${_h}" || \
        _NEW_CSRF="\${_NEW_CSRF},http://\${_h}"
    done
    # ALLOWED_HOSTS ebenfalls vervollständigen
    _CUR_AH="\$(grep '^ALLOWED_HOSTS=' "\$_ENV_FILE" | cut -d= -f2-)"
    _NEW_AH="\$_CUR_AH"
    for _h in \$(echo "\$_ALL_IPS" | tr ',' ' ') "\$_UPD_SHORT" "\$_UPD_FQDN"; do
      [ -z "\$_h" ] && continue
      echo "\$_NEW_AH" | grep -qF "\$_h" || _NEW_AH="\${_NEW_AH},\${_h}"
    done
    sed -i "s|^CSRF_TRUSTED_ORIGINS=.*|CSRF_TRUSTED_ORIGINS=\${_NEW_CSRF}|" "\$_ENV_FILE"
    sed -i "s|^ALLOWED_HOSTS=.*|ALLOWED_HOSTS=\${_NEW_AH}|" "\$_ENV_FILE"
    echo "  ✅ CSRF_TRUSTED_ORIGINS und ALLOWED_HOSTS aktualisiert"
  else
    echo "  ✅ CSRF_TRUSTED_ORIGINS bereits vollständig"
  fi
fi

# Migrationen
echo "📊 Erstelle und führe Migrationen aus..."
"\$MANAGER_DIR/venv/bin/python" "\$MANAGER_DIR/manage.py" makemigrations control --no-input
"\$MANAGER_DIR/venv/bin/python" "\$MANAGER_DIR/manage.py" migrate --run-syncdb

# Statische Dateien
echo "📦 Sammle statische Dateien..."
"\$MANAGER_DIR/venv/bin/python" "\$MANAGER_DIR/manage.py" collectstatic --noinput -v 0

# Service neu starten
echo "🔄 Neustart Manager-Service..."
systemctl restart "\$SERVICE"
sleep 2
systemctl is-active --quiet "\$SERVICE" && echo "✅ Manager läuft" || echo "❌ Manager-Start fehlgeschlagen"

# nginx neu laden (statische Dateien könnten sich geändert haben)
nginx -t 2>/dev/null && systemctl reload nginx && echo "✅ nginx neu geladen" || true

echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║                    MANAGER UPDATE DONE ✅                     ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
MGRUPDEOF
  chmod 755 /usr/local/bin/djmanager_update.sh
  echo "✅ Manager-Update-Script: djmanager_update.sh"

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
ExecStart=${_MANAGER_DIR}/venv/bin/gunicorn djmanager.wsgi:application --bind 127.0.0.1:${_MANAGER_PORT} --workers 1 --timeout 120
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
MANSERVEOF

  systemctl daemon-reload
  systemctl enable --now djmanager
  echo "✅ Manager-Service gestartet (0.0.0.0:${_MANAGER_PORT} + nginx Port 80)"

  # nginx Reverse Proxy für Manager
  # Manager ist über nginx (Port 80) und direkt über Port 8888 erreichbar
  echo "🌐 Erstelle nginx-Konfiguration für Manager..."
  mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled

  # Self-Signed SSL-Zertifikat bereitstellen (geteilt mit Webapps)
  _generate_selfsigned_cert

  cat > /etc/nginx/sites-available/djmanager <<MGNGINXEOF
# HTTP → HTTPS Redirect (Port 80)
server {
    listen 80 default_server;
    server_name _;
    return 301 https://\$host\$request_uri;
}

# Manager HTTPS (Port 443, Self-Signed SSL)
server {
    listen 443 ssl default_server;
    server_name _;
    client_max_body_size 10M;

    ssl_certificate     ${_SSL_CERT};
    ssl_certificate_key ${_SSL_KEY};
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;

    # Security Headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Strict-Transport-Security "max-age=31536000" always;

    # Manager-Logs (sichtbar im Log-Viewer)
    access_log /var/log/nginx/djmanager.access.log;
    error_log  /var/log/nginx/djmanager.error.log;

    # Statische Dateien des Managers
    location /static/ {
        alias ${_MANAGER_DIR}/staticfiles/;
        expires 1h;
        access_log off;
    }

    # Manager-App (Gunicorn auf 127.0.0.1:PORT)
    location / {
        proxy_pass http://127.0.0.1:${_MANAGER_PORT};
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_connect_timeout 30s;
        proxy_read_timeout 300s;
    }
}
MGNGINXEOF

  ln -sf /etc/nginx/sites-available/djmanager /etc/nginx/sites-enabled/djmanager
  # Default-Site entfernen (djmanager übernimmt als default_server)
  rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

  if nginx -t 2>/dev/null; then
    systemctl enable nginx 2>/dev/null || true
    systemctl restart nginx
    echo "✅ nginx gestartet — Manager über https://${_MGR_DEFAULT_IP}/ erreichbar"
  else
    echo "⚠️  nginx-Konfiguration ungültig — bitte manuell prüfen: nginx -t"
    nginx -t
  fi

  _MGR_IP="${_MGR_DEFAULT_IP}"

  echo
  # -------------------------------------------------------------------
  # Firewall Grundkonfiguration (ufw) — auch beim Manager-Only-Install
  # (identisch mit der Firewall-Sektion im INSTALL_PROJECT-Block)
  # -------------------------------------------------------------------
  echo "🔒 Prüfe Firewall (ufw)..."
  if ! command -v ufw >/dev/null 2>&1; then
    echo "  📦 Installiere ufw..."
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y ufw
  fi
  if command -v ufw >/dev/null 2>&1; then
    _UFW_STATUS_OUT=$(ufw status 2>&1)
    _UFW_STATUS_RC=$?
    if [ $_UFW_STATUS_RC -ne 0 ] && echo "$_UFW_STATUS_OUT" | grep -qi "error\|iptables\|failed"; then
      echo "  ⚠️  ufw nicht verfügbar in diesem Container (LXC ohne iptables-Unterstützung) — überspringe"
    elif ! ufw status | grep -q "Status: active"; then
      echo "  🔧 Setze Basis-Firewall-Regeln..."
      modprobe iptable_filter 2>/dev/null || true
      modprobe ip6table_filter 2>/dev/null || true
      ufw --force reset 2>/dev/null || true
      ufw default deny incoming
      ufw default allow outgoing
      ufw allow 22/tcp    comment 'SSH'
      ufw allow 80/tcp    comment 'HTTP nginx'
      ufw allow 443/tcp   comment 'HTTPS nginx'
      ufw allow in on lo  comment 'Loopback intern'
      ufw deny 8000:8999/tcp comment 'Gunicorn-Ports (intern only)'
      ufw --force enable
      echo "  ✅ Firewall aktiviert (SSH 22, HTTP 80, HTTPS 443; Ports 8000-8999 gesperrt)"
    else
      echo "  ✅ Firewall bereits aktiv"
    fi
  else
    echo "  ❌ ufw konnte nicht installiert werden — Firewall muss manuell konfiguriert werden"
  fi

  echo "╔════════════════════════════════════════════════════════════════════╗"
  echo "║              Manager erfolgreich installiert ✅                    ║"
  echo "╚════════════════════════════════════════════════════════════════════╝"
  echo "🔒 Manager-URL:    https://${_MGR_IP}/"
  echo "   (nginx Port 443/SSL → 127.0.0.1:${_MANAGER_PORT})"
  echo "   HTTP Port 80 leitet automatisch auf HTTPS weiter."
  echo
  echo "🔀 Externer Reverse Proxy → Ziel:"
  echo "   https://${_MGR_IP}:443  (SSL verify off / insecure erlaubt)"
  echo
  echo "📊 Status:         systemctl status djmanager"
  echo "📋 Logs:           journalctl -u djmanager -f"
  echo "🔄 Update:         djmanager_update.sh"
  echo "🔑 SSL-Cert:       ${_SSL_CERT}"
  echo "════════════════════════════════════════════════════════════════════"
fi  # end INSTALL_MANAGER

echo
echo "✅ DjangoMultiDeploy — Alle Installationen abgeschlossen."
