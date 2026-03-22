#!/bin/bash
# patch_update_script.sh
# Aktualisiert das installierte /usr/local/bin/djmanager_update.sh
# um Security-Header nach jedem Update automatisch zu patchen.
#
# Ausführen als root: sudo bash patch_update_script.sh

set -uo pipefail

UPDATE_SCRIPT="/usr/local/bin/djmanager_update.sh"
BACKUP="${UPDATE_SCRIPT}.bak.$(date +%Y%m%d_%H%M%S)"

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ Bitte als root ausführen"
  exit 1
fi

if [ ! -f "$UPDATE_SCRIPT" ]; then
  echo "❌ $UPDATE_SCRIPT nicht gefunden"
  exit 1
fi

# Bereits gepatcht?
if grep -q "fix_security_headers" "$UPDATE_SCRIPT" 2>/dev/null; then
  echo "✅ $UPDATE_SCRIPT ist bereits gepatcht"
  exit 0
fi

# Backup
cp "$UPDATE_SCRIPT" "$BACKUP"
echo "📦 Backup: $BACKUP"

# Bestimme SCRIPT_DIR aus dem installierten Script
SCRIPT_DIR=$(grep "^SCRIPT_DIR=" "$UPDATE_SCRIPT" | head -1 | cut -d= -f2- | tr -d '"')
if [ -z "$SCRIPT_DIR" ]; then
  SCRIPT_DIR="/opt/DjangoMultiDeploy"
fi

# Patch: Füge Security-Header-Fix vor dem abschließenden Banner ein
PATCH=$(cat <<'PATCHEOF'

# Security-Header sicherstellen (nginx-Config regenerierung kann Headers zurücksetzen)
_FIX_HEADERS="$SCRIPT_DIR/fix_security_headers.sh"
if [ -f "$_FIX_HEADERS" ]; then
  echo "🔒 Überprüfe Security-Header..."
  NGINX_SITES=/etc/nginx/sites-available bash "$_FIX_HEADERS" 2>&1 \
    | grep -v "^📦\|^📋\|^🔒\|Verzeichnis\|Backup\|Gefundene\|•\|Fertig" || true
fi
PATCHEOF
)

# Einfügen vor der abschließenden Banner-Zeile
BANNER_LINE=$(grep -n "MANAGER UPDATE DONE" "$UPDATE_SCRIPT" | head -1 | cut -d: -f1)
if [ -z "$BANNER_LINE" ]; then
  echo "❌ Banner-Zeile nicht gefunden in $UPDATE_SCRIPT"
  exit 1
fi

# Referenzzeile: die nginx -t Zeile davor finden (2 Zeilen vor Banner)
REF_LINE=$((BANNER_LINE - 2))

# Patch per python3 (zuverlässiger als sed bei mehrzeiligen Inserts)
python3 - <<PYEOF
lines = open("$UPDATE_SCRIPT").readlines()
insert_at = $REF_LINE  # nach dieser Zeilennummer einfügen
patch = """
# Security-Header sicherstellen (nginx-Config regenerierung kann Headers zurücksetzen)
_FIX_HEADERS="\$SCRIPT_DIR/fix_security_headers.sh"
if [ -f "\$_FIX_HEADERS" ]; then
  echo "🔒 Überprüfe Security-Header..."
  NGINX_SITES=/etc/nginx/sites-available bash "\$_FIX_HEADERS" 2>&1 \\\\
    | grep -v "^📦\\\\|^📋\\\\|^🔒\\\\|Verzeichnis\\\\|Backup\\\\|Gefundene\\\\|•\\\\|Fertig" || true
fi
"""
lines.insert(insert_at, patch)
open("$UPDATE_SCRIPT", 'w').writelines(lines)
print("✅ Patch eingefügt")
PYEOF

chmod 755 "$UPDATE_SCRIPT"
echo "✅ $UPDATE_SCRIPT aktualisiert"
echo ""
echo "Ab dem nächsten djmanager_update.sh werden Security-Header automatisch geprüft."
