#!/bin/bash
# harden_server.sh — bringt BESTEHENDE Installationen auf den Sicherheitsstand
# des aktuellen Installers. Idempotent; wird von fix_security_headers.sh (und
# damit bei jedem Manager-Update) aufgerufen, kann aber auch direkt laufen:
#   sudo bash harden_server.sh
#
# Ändert nur, was der Installer selbst angelegt hat. nginx wird hier NICHT neu
# geladen — das macht der Aufrufer nach `nginx -t` (mit Rollback bei Fehler).
set -uo pipefail

NGINX_SITES="${NGINX_SITES:-/etc/nginx/sites-available}"
NGINX_CONFD="${NGINX_CONFD:-/etc/nginx/conf.d}"
NGINX_MAIN="${NGINX_MAIN:-/etc/nginx/nginx.conf}"
SUDOERS_DIR="${SUDOERS_DIR:-/etc/sudoers.d}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ Dieses Skript muss als root ausgeführt werden."
  exit 1
fi

# ── 1. sudoers: keine Root-Shell über den Pager ──────────────────────────────
# Alte Einträge erlaubten `sudo journalctl -u <projekt>*` und
# `sudo systemctl status <projekt>`. Beide starten als root den Pager `less`,
# aus dem `!sh` eine Root-Shell öffnet → jeder App-Benutzer (und damit jede
# Lücke in der Web-App) war root. Logs gibt es stattdessen per Gruppe.
for f in "$SUDOERS_DIR"/*-service; do
  [ -f "$f" ] || continue
  grep -q 'journalctl\|systemctl status' "$f" 2>/dev/null || continue
  _user="$(awk 'NF && $1 !~ /^#/ {print $1; exit}' "$f")"
  _proj="$(basename "$f")"; _proj="${_proj%-service}"
  if ! [[ "$_user" =~ ^[a-z_][a-z0-9_-]*$ ]] || ! [[ "$_proj" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "  ⚠  sudoers $(basename "$f"): unerwartetes Format — bitte manuell prüfen"
    continue
  fi
  _tmp="$(mktemp "$SUDOERS_DIR/.dmd_XXXXXX")"
  echo "$_user ALL=(root) NOPASSWD: /bin/systemctl restart $_proj, /bin/systemctl reload $_proj" > "$_tmp"
  chmod 440 "$_tmp"
  if visudo -cf "$_tmp" >/dev/null 2>&1; then
    mv -f "$_tmp" "$f"
    getent group systemd-journal >/dev/null 2>&1 && usermod -aG systemd-journal "$_user" 2>/dev/null
    echo "     🔐 sudoers $_proj: journalctl/status entfernt (Root-Shell über Pager)"
  else
    rm -f "$_tmp"
    echo "  ⚠  sudoers $(basename "$f"): neue Regel ungültig — unverändert gelassen"
  fi
done

# ── 2. nginx: doppeltes real_ip_header macht die gesamte Config ungültig ─────
if [ -f "$NGINX_CONFD/realip.conf" ] && [ -f "$NGINX_CONFD/cloudflare_realip.conf" ]; then
  rm -f "$NGINX_CONFD/cloudflare_realip.conf"
  echo "     🔐 nginx: cloudflare_realip.conf entfernt (doppeltes real_ip_header, Netze stehen in realip.conf)"
fi

# ── 3. nginx: Versions-Banner abschalten ─────────────────────────────────────
if [ -d "$NGINX_CONFD" ] && [ ! -f "$NGINX_CONFD/dmd_hardening.conf" ] \
   && ! grep -rqsE '^[[:space:]]*server_tokens[[:space:]]' "$NGINX_MAIN" "$NGINX_CONFD/"; then
  echo 'server_tokens off;' > "$NGINX_CONFD/dmd_hardening.conf"
  echo "     🔐 nginx: server_tokens off"
fi

# ── 4. nginx-Sites: TLS, verlorene Header, static/media-Schutz ───────────────
if command -v python3 >/dev/null 2>&1 && [ -f "$SCRIPT_DIR/harden_nginx_sites.py" ] && [ -d "$NGINX_SITES" ]; then
  python3 "$SCRIPT_DIR/harden_nginx_sites.py" "$NGINX_SITES"/* || echo "  ⚠  nginx-Site-Härtung fehlgeschlagen"
fi

# ── 5. Backups: Geheimnisse nur für root lesbar ──────────────────────────────
for d in /var/backups/*/; do
  ls "$d"env_*.backup "$d"db_* >/dev/null 2>&1 || continue
  chmod 600 "$d"env_*.backup "$d"db_* 2>/dev/null
done

exit 0
