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

_has_header() {
  local file="$1" name="$2"
  grep -q "add_header[[:space:]]\+${name}" "$file" 2>/dev/null
}

# Fügt einen Header ein, falls er noch nicht vorhanden ist.
# Einfügen *nach* der letzten vorhandenen add_header-Zeile im Server-Block.
_add_header_after_last() {
  local file="$1" header_line="$2"
  # Letzte add_header-Zeile finden und danach einfügen
  local last_line
  last_line=$(grep -n "add_header" "$file" | tail -1 | cut -d: -f1)
  if [ -n "$last_line" ]; then
    sed -i "${last_line}a\\    ${header_line}" "$file"
  else
    # Keine add_header-Zeilen → nach ssl_session_cache einfügen
    local ref_line
    ref_line=$(grep -n "ssl_session_cache\|ssl_ciphers\|ssl_protocols" "$file" | tail -1 | cut -d: -f1)
    if [ -n "$ref_line" ]; then
      sed -i "${ref_line}a\\    ${header_line}" "$file"
    else
      echo "  ⚠  Konnte Einfügeposition nicht bestimmen in: $file" >&2
    fi
  fi
}

# Ersetzt einen vorhandenen Header-Wert durch den neuen.
_replace_header() {
  local file="$1" name="$2" new_line="$3"
  sed -i "s|.*add_header[[:space:]]\+${name}.*|    ${new_line}|" "$file"
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

  # Nur HTTPS-Server-Blöcke patchen (die mit ssl in der listen-Zeile)
  if ! grep -q "listen.*ssl\|ssl_certificate" "$file" 2>/dev/null; then
    echo "  ⏭  Kein SSL-Block gefunden, überspringe: $(basename "$file")"
    return
  fi

  echo "  🔍 Prüfe: $(basename "$file")"

  # X-Frame-Options: SAMEORIGIN → DENY korrigieren falls nötig
  if grep -q 'add_header X-Frame-Options.*SAMEORIGIN' "$file"; then
    _replace_header "$file" "X-Frame-Options" "$HEADER_XFRAME"
    echo "     ✏  X-Frame-Options: SAMEORIGIN → DENY"
    changed=1
  fi

  # HSTS: ohne includeSubDomains → mit includeSubDomains
  if grep -q 'add_header Strict-Transport-Security' "$file" && \
     ! grep -q 'includeSubDomains' "$file"; then
    _replace_header "$file" "Strict-Transport-Security" "$HEADER_HSTS"
    echo "     ✏  HSTS: includeSubDomains ergänzt"
    changed=1
  fi

  # Fehlende Header hinzufügen
  for name_line in \
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
    local hname="${name_line%%|*}"
    local hline="${name_line#*|}"
    if ! _has_header "$file" "$hname"; then
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
echo "   Verzeichnis: $NGINX_SITES"
echo "   Backup:      $BACKUP_DIR"
echo ""

# Backup erstellen
mkdir -p "$BACKUP_DIR"
cp -r "$NGINX_SITES"/. "$BACKUP_DIR/"
echo "📦 Backup erstellt: $BACKUP_DIR"
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
