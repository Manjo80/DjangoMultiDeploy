#!/bin/bash
# fix_security_headers.sh
# Patcht bestehende nginx-Konfigurationen um fehlende Security-Header zu ergänzen.
# Ausführen als root: sudo bash fix_security_headers.sh

NGINX_SITES="${NGINX_SITES:-/etc/nginx/sites-available}"
BACKUP_DIR="/tmp/nginx_headers_backup_$(date +%Y%m%d_%H%M%S)"

# Globale Variablen für aktuell bearbeitete Datei und Änderungsstatus
CUR_FILE=""
CHANGED=0

# ─── Hilfsfunktionen ─────────────────────────────────────────────────────────

# Letzte add_header-Zeile im Server-Block (4 Leerzeichen Einrückung, nicht location-Blöcke)
_ref_line() {
  grep -n "^    add_header" "$CUR_FILE" 2>/dev/null | tail -1 | cut -d: -f1
  return 0
}

# Fallback-Referenzzeile (ssl-Direktiven im Server-Block)
_ssl_line() {
  grep -n "^    ssl_session_cache\|^    ssl_ciphers\|^    ssl_protocols" "$CUR_FILE" 2>/dev/null | tail -1 | cut -d: -f1
  return 0
}

# Prüft ob Header auf Server-Block-Ebene vorhanden ist
_has() {
  grep -q "^    add_header[[:space:]]\+$1" "$CUR_FILE" 2>/dev/null
}

# Fügt einen Header ein (nach letztem Server-Block-Header, oder nach SSL-Direktiven)
_insert() {
  local name="$1" line="$2"
  local ref
  ref=$(_ref_line)
  if [ -z "$ref" ]; then
    ref=$(_ssl_line)
  fi
  if [ -n "$ref" ]; then
    sed -i "${ref}a\\    ${line}" "$CUR_FILE"
    echo "     ➕ ${name} hinzugefügt"
    CHANGED=1
  else
    echo "  ⚠  Keine Einfügeposition für ${name} in: $(basename "$CUR_FILE")"
  fi
}

# Fügt Header ein wenn er noch nicht vorhanden ist
_ensure() {
  local name="$1" line="$2"
  if ! _has "$name"; then
    _insert "$name" "$line"
  fi
}

# ─── Patch-Funktion ──────────────────────────────────────────────────────────

patch_config() {
  CUR_FILE="$1"
  CHANGED=0

  # Standard nginx Default-Config immer überspringen
  if [ "$(basename "$CUR_FILE")" = "default" ]; then
    echo "  ⏭  Default-Config — überspringe: $(basename "$CUR_FILE")"
    return
  fi

  # Nur SSL-Configs
  if ! grep -q "listen.*ssl\|ssl_certificate" "$CUR_FILE" 2>/dev/null; then
    echo "  ⏭  Kein SSL — überspringe: $(basename "$CUR_FILE")"
    return
  fi

  # Catch-all und Redirect-Only-Blöcke überspringen (return 444, kein proxy_pass/alias)
  if grep -q "return 444\|return 301" "$CUR_FILE" 2>/dev/null \
     && ! grep -q "proxy_pass\|^[[:space:]]*alias[[:space:]]" "$CUR_FILE" 2>/dev/null; then
    echo "  ⏭  Catch-all — überspringe: $(basename "$CUR_FILE")"
    return
  fi

  echo "  🔍 Prüfe: $(basename "$CUR_FILE")"

  # djmanager: Django's SecurityHeadersMiddleware setzt CSP dynamisch mit Per-Request-Nonce.
  # Ein statischer nginx-CSP würde mit Djangos Nonce-CSP kollidieren (Browser wendet beide an).
  # → Vorhandenen statischen CSP entfernen, keinen neuen hinzufügen.
  local SKIP_CSP=0
  if [ "$(basename "$CUR_FILE")" = "djmanager" ]; then
    SKIP_CSP=1
    if grep -q "^    add_header Content-Security-Policy" "$CUR_FILE" 2>/dev/null; then
      sed -i '/^    add_header Content-Security-Policy/d' "$CUR_FILE"
      echo "     ✏  CSP entfernt (Django/SecurityHeadersMiddleware verwaltet CSP mit Nonce)"
      CHANGED=1
    fi
  fi

  # X-Frame-Options: SAMEORIGIN → DENY
  if grep -q "^    add_header X-Frame-Options.*SAMEORIGIN" "$CUR_FILE" 2>/dev/null; then
    sed -i 's|^    add_header[[:space:]]\+X-Frame-Options.*|    add_header X-Frame-Options "DENY" always;|' "$CUR_FILE"
    echo "     ✏  X-Frame-Options: SAMEORIGIN → DENY"
    CHANGED=1
  fi

  # HSTS: includeSubDomains ergänzen
  if grep -q "^    add_header Strict-Transport-Security" "$CUR_FILE" 2>/dev/null \
     && ! grep -q "includeSubDomains" "$CUR_FILE" 2>/dev/null; then
    sed -i 's|^    add_header[[:space:]]\+Strict-Transport-Security.*|    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;|' "$CUR_FILE"
    echo "     ✏  HSTS: includeSubDomains ergänzt"
    CHANGED=1
  fi

  # COEP: require-corp → unsafe-none (require-corp blockt CDN-Ressourcen ohne CORP-Header)
  if grep -q "^    add_header Cross-Origin-Embedder-Policy.*require-corp" "$CUR_FILE" 2>/dev/null; then
    sed -i 's|^    add_header[[:space:]]\+Cross-Origin-Embedder-Policy.*|    add_header Cross-Origin-Embedder-Policy "unsafe-none" always;|' "$CUR_FILE"
    echo "     ✏  COEP: require-corp → unsafe-none"
    CHANGED=1
  fi

  if [ "$SKIP_CSP" -eq 0 ]; then
    # CSP: 'unsafe-inline'/'unsafe-eval' aus script-src entfernen (kein JS-Inline-Execution)
    if grep -q "^    add_header Content-Security-Policy.*script-src.*unsafe-inline" "$CUR_FILE" 2>/dev/null; then
      sed -i \
        "s|script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net|script-src 'self' https://cdn.jsdelivr.net|g; \
         s|script-src 'self' 'unsafe-inline' 'unsafe-eval'|script-src 'self'|g; \
         s|script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net|script-src 'self' https://cdn.jsdelivr.net|g; \
         s|script-src 'self' 'unsafe-inline'|script-src 'self'|g" \
        "$CUR_FILE"
      echo "     ✏  CSP: 'unsafe-inline'/'unsafe-eval' aus script-src entfernt"
      CHANGED=1
    fi

    # CSP: cdn.jsdelivr.net ergänzen wenn nicht vorhanden (Bootstrap/Icons CDN)
    if grep -q "^    add_header Content-Security-Policy" "$CUR_FILE" 2>/dev/null \
       && ! grep -q "cdn.jsdelivr.net" "$CUR_FILE" 2>/dev/null; then
      sed -i \
        "s|script-src 'self';|script-src 'self' https://cdn.jsdelivr.net;|g" \
        "$CUR_FILE"
      sed -i \
        "s|style-src 'self' 'unsafe-inline';|style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;|g" \
        "$CUR_FILE"
      sed -i \
        "s|font-src 'self' data:;|font-src 'self' data: https://cdn.jsdelivr.net;|g" \
        "$CUR_FILE"
      echo "     ✏  CSP: cdn.jsdelivr.net ergänzt"
      CHANGED=1
    fi
  fi

  # Fehlende Header ergänzen (einzeln aufgerufen — kein Loop, kein pipefail-Problem)
  _ensure "X-Content-Type-Options"   'add_header X-Content-Type-Options "nosniff" always;'
  _ensure "X-XSS-Protection"        'add_header X-XSS-Protection "1; mode=block" always;'
  _ensure "Referrer-Policy"         'add_header Referrer-Policy "strict-origin-when-cross-origin" always;'
  _ensure "Permissions-Policy"      'add_header Permissions-Policy "geolocation=(), microphone=(), camera=(), payment=(), usb=()" always;'
  [ "$SKIP_CSP" -eq 0 ] && _ensure "Content-Security-Policy" "add_header Content-Security-Policy \"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none';\" always;"
  _ensure "Strict-Transport-Security"       'add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;'
  _ensure "Cross-Origin-Opener-Policy"      'add_header Cross-Origin-Opener-Policy "same-origin" always;'
  _ensure "Cross-Origin-Embedder-Policy"    'add_header Cross-Origin-Embedder-Policy "unsafe-none" always;'
  _ensure "Cross-Origin-Resource-Policy"    'add_header Cross-Origin-Resource-Policy "same-origin" always;'

  if [ "$CHANGED" -eq 0 ]; then
    echo "     ✅ Alle Header bereits vorhanden"
  fi
}

# ─── Hauptprogramm ───────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ Dieses Skript muss als root ausgeführt werden."
  exit 1
fi

if [ ! -d "$NGINX_SITES" ]; then
  echo "❌ Verzeichnis nicht gefunden: $NGINX_SITES"
  exit 1
fi

echo "🔒 Security-Header-Patch für nginx"
echo "   Verzeichnis : $NGINX_SITES"
echo "   Backup      : $BACKUP_DIR"
echo ""

mkdir -p "$BACKUP_DIR"
cp -r "$NGINX_SITES"/. "$BACKUP_DIR/"
echo "📦 Backup erstellt: $BACKUP_DIR"
echo ""

echo "📋 Gefundene Konfigurationen:"
for f in "$NGINX_SITES"/*; do
  [ -f "$f" ] && echo "   • $(basename "$f")"
done
echo ""

for config_file in "$NGINX_SITES"/*; do
  [ -f "$config_file" ] || continue
  patch_config "$config_file"
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
  systemctl reload nginx
  echo "⚠  Originalkonfiguration wiederhergestellt aus: $BACKUP_DIR"
  exit 1
fi

echo ""
echo "Fertig. Backup: $BACKUP_DIR"
