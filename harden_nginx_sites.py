#!/usr/bin/env python3
"""Idempotent hardening of nginx site configs written by DjangoMultiDeploy.

Called by harden_server.sh for every file in sites-available. The caller
backs the directory up, runs `nginx -t` afterwards and restores on failure.

What it changes (only where the old installer pattern is found):
  * weak cipher list  -> forward-secret AEAD suites, session tickets off
  * a lone `add_header` inside a location (Retry-After / Cache-Control) is
    removed: it silently discards every security header of the server block
  * dot files and backups/dumps below /static/ and /media/ return 404
  * HTML/SVG/JS uploads below /media/ are served as sandboxed downloads
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CIPHERS = (
    "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305"
)
WEAK_CIPHERS = re.compile(r"^(\s*)ssl_ciphers\s+HIGH:!aNULL:!MD5;\s*$")
LONE_HEADERS = ("add_header Retry-After 60 always;", 'add_header Cache-Control "public, immutable";')
GUARD_MARK = "# dmd-hardening: guards"
MEDIA_MARK = "# dmd-hardening: media"

GUARDS = f"""    {GUARD_MARK} (dot files, backups and dumps are never served)
    location ~ /\\.(?!well-known/) {{
        return 404;
    }}
    location ~* ^/(static|media)/(.*/)?(backups?/|[^/]+\\.(sql|sqlite3?|dump|bak|old|orig|swp|env|ini|log|pyc?|sh)$) {{
        return 404;
    }}

"""

MEDIA_BLOCK = f"""
        {MEDIA_MARK} (uploaded HTML/SVG/JS must never run in the app's origin)
        location ~* \\.(html?|xhtml|svgz?|xml|js|mjs|css)$ {{
            add_header Content-Disposition "attachment" always;
            add_header Content-Security-Policy "default-src 'none'; sandbox" always;
            add_header X-Content-Type-Options "nosniff" always;
            add_header X-Frame-Options "DENY" always;
            add_header Referrer-Policy "no-referrer" always;
            add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
            add_header Cross-Origin-Resource-Policy "same-origin" always;
        }}
"""


def _location_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """(start, end) line indexes of every top-level `location … { … }` block."""
    blocks, start, depth = [], None, 0
    for i, line in enumerate(lines):
        code = line.split("#", 1)[0]
        if start is None and re.match(r"\s*location\s", code) and "{" in code:
            start, depth = i, 0
        if start is not None:
            depth += code.count("{") - code.count("}")
            if depth <= 0:
                blocks.append((start, i))
                start = None
    return blocks


def harden(text: str) -> tuple[str, list[str]]:
    """Return (new_text, list of human-readable changes)."""
    changes: list[str] = []
    if "ssl_certificate" not in text or "proxy_pass" not in text:
        return text, changes
    lines = text.splitlines(keepends=True)

    # 1. TLS
    for i, line in enumerate(lines):
        m = WEAK_CIPHERS.match(line)
        if m:
            lines[i] = f"{m.group(1)}ssl_ciphers         {CIPHERS};\n"
            extra = ""
            if "ssl_prefer_server_ciphers" not in text:
                extra += f"{m.group(1)}ssl_prefer_server_ciphers off;\n"
            if "ssl_session_tickets" not in text:
                extra += f"{m.group(1)}ssl_session_tickets off;\n"
            lines[i] += extra
            changes.append("TLS: nur noch Forward-Secrecy-Cipher, Session-Tickets aus")

    # 2. lone add_header inside a location drops all inherited security headers
    drop: set[int] = set()
    for start, end in _location_blocks(lines):
        headers = [i for i in range(start, end + 1) if lines[i].strip().startswith("add_header ")]
        if len(headers) == 1 and lines[headers[0]].strip() in LONE_HEADERS:
            drop.add(headers[0])
    if drop:
        lines = [line for i, line in enumerate(lines) if i not in drop]
        changes.append(f"{len(drop)} add_header entfernt, der die Security-Header der location verwarf")

    # 3. nested download-only block for active content in /media/
    if MEDIA_MARK not in text:
        for start, end in reversed(_location_blocks(lines)):
            head = lines[start].split("#", 1)[0]
            body = "".join(lines[start:end + 1])
            if re.match(r"\s*location\s+(\^~\s+)?/media/\s*\{", head) and "alias" in body \
                    and "Content-Disposition" not in body and "return 404" not in body:
                lines.insert(end, MEDIA_BLOCK)
                changes.append("/media/: HTML/SVG/JS nur noch als Sandbox-Download")

    # 4. guard locations in front of the first static/media location
    if GUARD_MARK not in text:
        for start, _end in _location_blocks(lines):
            if re.match(r"\s*location\s+(\^~\s+)?/(static|media)/\s*\{", lines[start].split("#", 1)[0]):
                at = start
                while at > 0 and lines[at - 1].lstrip().startswith("#"):
                    at -= 1  # keep the block's own comment attached to it
                lines.insert(at, GUARDS)
                changes.append("Punkt-Dateien und Backups/Dumps unter static/media gesperrt")
                break

    return "".join(lines), changes


def main(paths: list[str]) -> int:
    for name in paths:
        path = Path(name)
        if not path.is_file() or path.name == "default":
            continue
        original = path.read_text()
        patched, changes = harden(original)
        if patched != original:
            path.write_text(patched)
            for change in changes:
                print(f"     🔐 {path.name}: {change}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
