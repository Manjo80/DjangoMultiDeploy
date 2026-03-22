#!/bin/bash
# fix_security_headers.sh
# Patcht bestehende nginx-Konfigurationen um fehlende Security-Header zu ergänzen.
# Betrifft: alle Webapps und den Manager (djmanager) in /etc/nginx/sites-available/
#
# Ausführen als root: sudo bash fix_security_headers.sh

set -euo pipefail

NGINX_SITES="${NGINX_SITES:-/etc/nginx/sites-available}"
BACKUP_DIR="/tmp/nginx_headers_backup_$(date +%Y%m%d_%H%M%S)"

# ─── Hilfsfunktionen ─────────────────────────────────────────────────────────

# Prüft ob ein Header auf Server-Block-Ebene vorhanden ist.
# Server-Block-Header haben 4 Leerzeichen Einrückung (nicht 8 wie location-Blöcke).
_has_header_in_server_block() {
  local file="$1" name="$2"
  grep -q "^    add_header[[:space:]]\+${name}" "$file" 2>/dev/null
}

# Findet die letzte add_header-Zeile auf Server-Block-Ebene (4 Leerzeichen).
# Location-Block-Header (8 Leerzeichen) werden dabei IGNORIERT.
_last_server_block_header_line() {
  local file="$1"
  grep -n "^    add_header" "$file" | tail -1 | cut -d: -f1
}

# Fügt einen Header nach dem letzten Server-Block-Level add_header ein.
_add_header_after_last() {
  local file="$1" header_line="$2"
  local ref_line
  ref_line=$(_last_server_block_header_line "$file")

  if [ -n "$ref_line" ] && [ "$ref_line" -gt 0 ]; then
    sed -i "${ref_line}a\\    ${header_line}" "$file"
  else
    # Kein add_header im Server-Block → nach ssl_session_cache einfügen
    ref_line=$(grep -n "^    ssl_session_cache\|^    ssl_ciphers\|^    ssl_protocols" "$file" | tail -1 | cut -d: -f1)
    if [ -n "$ref_line" ]; then
      sed -i "${ref_line}a\\    ${header_line}" "$file"
    else
      echo "  ⚠  Konnte Einfügeposition nicht bestimmen in: $file" >&2
    fi
  fi
}

# Ersetzt einen vorhandenen Header (nur Server-Block-Ebene).
_replace_header() {
  local file="$1" name="$2" new_line="$3"
  sed -i "s|^    add_header[[:space:]]\+${name}.*|    ${new_line}|" "$file"
}

# ─── Header-Definitionen ─────────────────────────────────────────────────────

HEADER_XFRAME='add_header X-Frame-Options "DENY" always;'
HEADER_XCTO='add_header X-Content-Type-Options "nosniff" always;'
HEADER_XSS='add_header X-XSS-Protection "1; mode=block" always;'
HEADER_RP='add_header Referrer-Policy "strict-origin-when-cross-origin" always;'
HEADER_PP='add_header Permissions-Policy "geolocation=(), microphone=(), camera=(), payment=(), usb=()" always;'
HEADER_CSP='add_header Content-Security-Policy "default-src '"'"'self'"'"'; script-src '"'"'self'"'"' '"'"'unsafe-inline'"'"' '"'"'unsafe-eval'"'"'; style-src '"'"'self'"'"' '"'"'unsafe-inline'"'"'; img-src '"'"'self'"'"' data: blob:; font-src '"'"'self'"'"' data:; frame-ancestors '"'"'none'"'"';" always;'
HEADER_HSTS='add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;'
HEADER_COOP='add_header Cross-Origin-Opener-Policy "same-origin" always;'
HEADER_COEP='add_header Cross-Origin-Embedder-Policy "require-corp" always;'
HEADER_CORP='add_header Cross-Origin-Resource-Policy "same-origin" always;'

# ─── Haupt-Patch-Funktion ────────────────────────────────────────────────────

patch_nginx_config() {
  local file="$1"
  local changed=0

  # Nur HTTPS-Server-Blöcke patchen
  if ! grep -q "listen.*ssl\|ssl_certificate" "$file" 2>/dev/null; then
    echo "  ⏭  Kein SSL-Block — überspringe: $(basename "$file")"
    return
  fi

  echo "  🔍 Prüfe: $(basename "$file")"

  # X-Frame-Options: SAMEORIGIN → DENY korrigieren
  if grep -q "^    add_header X-Frame-Options.*SAMEORIGIN" "$file"; then
    _replace_header "$file" "X-Frame-Options" "$HEADER_XFRAME"
    echo "     ✏  X-Frame-Options: SAMEORIGIN → DENY"
    changed=1
  fi

  # HSTS: ohne includeSubDomains ergänzen
  if grep -q "^    add_header Strict-Transport-Security" "$file" && \
     ! grep -q "includeSubDomains" "$file"; then
    _replace_header "$file" "Strict-Transport-Security" "$HEADER_HSTS"
    echo "     ✏  HSTS: includeSubDomains ergänzt"
    changed=1
  fi

  # Fehlende Header hinzufügen (Reihenfolge: erst alle prüfen, dann in richtiger Reihe einfügen)
  local missing_headers=()
  for entry in \
    "X-Content-Type-Options|$HEADER_XCTO" \
    "X-XSS-Protection|$HEADER_XSS" \
    "Referrer-Policy|$HEADER_RP" \
    "Permissions-Policy|$HEADER_PP" \
    "Content-Security-Policy|$HEADER_CSP" \
    "Strict-Transport-Security|$HEADER_HSTS" \
    "Cross-Origin-Opener-Policy|$HEADER_COOP" \
    "Cross-Origin-Embedder-Policy|$HEADER_COEP" \
    "Cross-Origin-Resource-Policy|$HEADER_CORP"
  do
    local hname="${entry%%|*}"
    local hline="${entry#*|}"
    if ! _has_header_in_server_block "$file" "$hname"; then
      # Nach dem aktuell letzten Server-Block-Header einfügen
      _add_header_after_last "$file" "$hline"
      echo "     ➕ $hname hinzugefügt"
      changed=1
    fi
  done

  if [ "$changed" -eq 0 ]; then
    echo "     ✅ Alle Header bereits vorhanden"
  fi
}

# ─── Hauptprogramm ───────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ Dieses Skript muss als root ausgeführt werden."
  exit 1
fi

if [ ! -d "$NGINX_SITES" ]; then
  echo "❌ nginx sites-Verzeichnis nicht gefunden: $NGINX_SITES"
  exit 1
fi

echo "🔒 Security-Header-Patch für nginx"
echo "   Verzeichnis : $NGINX_SITES"
echo "   Backup      : $BACKUP_DIR"
echo ""

# Backup erstellen
mkdir -p "$BACKUP_DIR"
cp -r "$NGINX_SITES"/. "$BACKUP_DIR/"
echo "📦 Backup erstellt: $BACKUP_DIR"
echo ""

# Gefundene Dateien auflisten (Transparenz)
echo "📋 Gefundene Konfigurationen:"
for f in "$NGINX_SITES"/*; do
  [ -f "$f" ] && echo "   • $(basename "$f")"
done
echo ""

# Alle Konfigurationsdateien patchen
for config_file in "$NGINX_SITES"/*; do
  [ -f "$config_file" ] || continue
  patch_nginx_config "$config_file"
done

echo ""
echo "🧪 Teste nginx-Konfiguration..."
if nginx -t 2>&1; then
  echo ""
  echo "🔄 Lade nginx neu..."
  systemctl reload nginx
  echo "✅ nginx neu geladen — alle Security-Header aktiv"
else
  echo ""
  echo "❌ nginx-Test fehlgeschlagen! Stelle Backup wieder her..."
  cp -r "$BACKUP_DIR"/. "$NGINX_SITES/"
  nginx -t
  echo "⚠  Originalkonfiguration wiederhergestellt aus: $BACKUP_DIR"
  exit 1
fi

echo ""
echo "Fertig. Backup der Originaldateien: $BACKUP_DIR"
