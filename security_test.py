#!/usr/bin/env python3
"""
External Security Test Suite
Testet die komplette Zugriffskette von außen:
  Cloudflare → Zoraxy → nginx → Django-Webapp

Verwendung:
  python3 security_test.py <hostname> [--port 443] [--no-verify] [--json]

Beispiel:
  python3 security_test.py meine-domain.de
  python3 security_test.py meine-domain.de --json > bericht.json
"""

import argparse
import json
import socket
import ssl
import sys
import time
import datetime
from urllib.parse import urlparse
import urllib.request
import urllib.error

# Optional: requests (bevorzugt), Fallback auf urllib
try:
    import requests
    from requests.exceptions import SSLError, ConnectionError, Timeout
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ─────────────────────────────────────────────────────────────────────────────
# FARBEN / OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
GRAY   = "\033[90m"

def _color(text, color):
    return f"{color}{text}{RESET}"

def ok(msg):    print(f"  {_color('✓', GREEN)} {msg}")
def warn(msg):  print(f"  {_color('!', YELLOW)} {msg}")
def fail(msg):  print(f"  {_color('✗', RED)} {msg}")
def info(msg):  print(f"  {_color('i', CYAN)} {msg}")

def section(title):
    print(f"\n{BOLD}{CYAN}{'─'*60}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'─'*60}{RESET}")

# ─────────────────────────────────────────────────────────────────────────────
# ERGEBNIS-SAMMLUNG
# ─────────────────────────────────────────────────────────────────────────────

results = {
    "meta": {},
    "tls": {},
    "http_headers": {},
    "http_behavior": {},
    "proxy_chain": {},
    "application": {},
    "disclosure": {},
    "summary": {"pass": 0, "warn": 0, "fail": 0}
}

def record(category, key, status, detail=""):
    """status: 'pass' | 'warn' | 'fail'"""
    results[category][key] = {"status": status, "detail": detail}
    results["summary"][status] += 1
    if status == "pass":
        ok(f"{key}: {detail}")
    elif status == "warn":
        warn(f"{key}: {detail}")
    else:
        fail(f"{key}: {detail}")


# ─────────────────────────────────────────────────────────────────────────────
# HILFSFUNKTIONEN
# ─────────────────────────────────────────────────────────────────────────────

def get(url, verify_ssl=True, allow_redirects=True, timeout=10, extra_headers=None):
    """HTTP GET – requests oder urllib."""
    headers = {
        "User-Agent": "SecurityAudit/1.0 (external-test)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if extra_headers:
        headers.update(extra_headers)

    if HAS_REQUESTS:
        return requests.get(
            url, verify=verify_ssl, allow_redirects=allow_redirects,
            timeout=timeout, headers=headers
        )
    else:
        # urllib-Fallback (kein verify_ssl-Toggle – immer verifiziert)
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return resp
        except urllib.error.HTTPError as e:
            return e


def get_status(resp):
    return resp.status_code if HAS_REQUESTS else resp.status


def get_header(resp, name, default=None):
    if HAS_REQUESTS:
        return resp.headers.get(name, default)
    else:
        return resp.headers.get(name, default)


def get_final_url(resp):
    if HAS_REQUESTS:
        return resp.url
    return getattr(resp, "url", "?")


# ─────────────────────────────────────────────────────────────────────────────
# 1. TLS / ZERTIFIKAT
# ─────────────────────────────────────────────────────────────────────────────

def test_tls(hostname, port):
    section("1 · TLS / Zertifikat")

    # 1a – Zertifikat abrufen
    ctx = ssl.create_default_context()
    try:
        conn = ctx.wrap_socket(socket.create_connection((hostname, port), timeout=10),
                               server_hostname=hostname)
        cert = conn.getpeercert()
        cipher = conn.cipher()        # (name, protocol, bits)
        proto  = conn.version()
        conn.close()
    except ssl.SSLCertVerificationError as e:
        record("tls", "Zertifikat gültig", "fail", str(e))
        return
    except Exception as e:
        record("tls", "TLS-Verbindung", "fail", str(e))
        return

    # Ablaufdatum
    not_after = datetime.datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
    days_left = (not_after - datetime.datetime.utcnow()).days
    if days_left > 30:
        record("tls", "Zertifikat Ablauf", "pass", f"gültig noch {days_left} Tage ({not_after.date()})")
    elif days_left > 0:
        record("tls", "Zertifikat Ablauf", "warn", f"läuft in {days_left} Tagen ab!")
    else:
        record("tls", "Zertifikat Ablauf", "fail", "ABGELAUFEN!")

    # Aussteller (Cloudflare, Let's Encrypt …)
    issuer = dict(x[0] for x in cert.get("issuer", []))
    issuer_cn = issuer.get("commonName", "unbekannt")
    record("tls", "Zertifikat-Aussteller", "pass" if issuer_cn else "warn",
           issuer_cn)

    # Subject / Hostname-Match
    subject = dict(x[0] for x in cert.get("subject", []))
    san = [v for _, v in cert.get("subjectAltName", [])]
    matched = hostname in san or hostname == subject.get("commonName", "")
    if not matched:
        # Wildcard-Prüfung
        for entry in san:
            if entry.startswith("*.") and hostname.endswith(entry[1:]):
                matched = True
                break
    record("tls", "Hostname-Match", "pass" if matched else "fail",
           f"SAN: {', '.join(san[:3])}{'…' if len(san) > 3 else ''}")

    # TLS-Version
    tls_versions = {"TLSv1":   "fail",
                    "TLSv1.1": "fail",
                    "TLSv1.2": "warn",
                    "TLSv1.3": "pass"}
    status = tls_versions.get(proto, "warn")
    record("tls", "TLS-Version", status, proto)

    # Cipher
    cipher_name, cipher_proto, cipher_bits = cipher
    if cipher_bits and cipher_bits >= 256:
        cstatus = "pass"
    elif cipher_bits and cipher_bits >= 128:
        cstatus = "warn"
    else:
        cstatus = "fail"
    record("tls", "Cipher Suite", cstatus,
           f"{cipher_name} ({cipher_bits} bit)")

    # TLS 1.0 und 1.1 explizit ablehnen?
    for bad_ver in [ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1_1]:
        ctx2 = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx2.check_hostname = True
        ctx2.verify_mode = ssl.CERT_REQUIRED
        try:
            ctx2.maximum_version = bad_ver
            conn2 = ctx2.wrap_socket(
                socket.create_connection((hostname, port), timeout=5),
                server_hostname=hostname)
            conn2.close()
            record("tls", f"{bad_ver.name} abgelehnt", "fail",
                   f"Server akzeptiert noch veraltetes {bad_ver.name}!")
        except ssl.SSLError:
            record("tls", f"{bad_ver.name} abgelehnt", "pass",
                   f"Server lehnt {bad_ver.name} korrekt ab")
        except Exception:
            record("tls", f"{bad_ver.name} abgelehnt", "pass",
                   f"Verbindung mit {bad_ver.name} fehlgeschlagen (OK)")


# ─────────────────────────────────────────────────────────────────────────────
# 2. HTTP-SICHERHEITSHEADER
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_HEADERS = {
    "Strict-Transport-Security": {
        "check": lambda v: "max-age=" in v and int(v.split("max-age=")[1].split(";")[0].strip()) >= 31536000,
        "ok":    "HSTS korrekt gesetzt",
        "fail":  "HSTS fehlt oder max-age zu kurz (min. 31536000 = 1 Jahr)",
        "warn":  "HSTS vorhanden aber schwach"
    },
    "X-Frame-Options": {
        "check": lambda v: v.upper() in ("DENY", "SAMEORIGIN"),
        "ok":    "Clickjacking-Schutz aktiv",
        "fail":  "Fehlt – Clickjacking möglich",
        "warn":  "Vorhanden aber ungewöhnlicher Wert"
    },
    "X-Content-Type-Options": {
        "check": lambda v: v.lower() == "nosniff",
        "ok":    "MIME-Sniffing deaktiviert",
        "fail":  "Fehlt – MIME-Sniffing möglich",
        "warn":  "Vorhanden aber nicht 'nosniff'"
    },
    "Referrer-Policy": {
        "check": lambda v: v.lower() in ("no-referrer", "strict-origin",
                                          "strict-origin-when-cross-origin",
                                          "same-origin"),
        "ok":    "Referrer-Policy sinnvoll gesetzt",
        "fail":  "Fehlt",
        "warn":  "Schwache Referrer-Policy"
    },
    "Content-Security-Policy": {
        "check": lambda v: bool(v),
        "ok":    "CSP vorhanden",
        "fail":  "CSP fehlt",
        "warn":  "CSP vorhanden"
    },
    "Permissions-Policy": {
        "check": lambda v: bool(v),
        "ok":    "Permissions-Policy gesetzt",
        "fail":  "Fehlt",
        "warn":  "Vorhanden"
    },
}

UNWANTED_HEADERS = {
    "X-Powered-By":    "Technologie-Disclosure (X-Powered-By)",
    "X-AspNet-Version":"Technologie-Disclosure (ASP.NET)",
    "Server":          "Server-Banner",
}

def test_headers(hostname, port, verify_ssl):
    section("2 · HTTP-Sicherheitsheader")
    url = f"https://{hostname}:{port}/" if port != 443 else f"https://{hostname}/"
    try:
        resp = get(url, verify_ssl=verify_ssl, allow_redirects=True)
    except Exception as e:
        fail(f"Verbindung fehlgeschlagen: {e}")
        return

    for header, meta in REQUIRED_HEADERS.items():
        val = get_header(resp, header, "")
        if not val:
            record("http_headers", header, "fail", meta["fail"])
        elif meta["check"](val):
            record("http_headers", header, "pass", f"{meta['ok']} ({val[:80]})")
        else:
            record("http_headers", header, "warn", f"{meta['warn']} ({val[:80]})")

    for header, label in UNWANTED_HEADERS.items():
        val = get_header(resp, header, "")
        if val:
            record("http_headers", label, "warn", f"offenbart: '{val}'")
        else:
            record("http_headers", label, "pass", "Header nicht exponiert")


# ─────────────────────────────────────────────────────────────────────────────
# 3. HTTP-VERHALTEN
# ─────────────────────────────────────────────────────────────────────────────

def test_http_behavior(hostname, port, verify_ssl):
    section("3 · HTTP-Verhalten & Weiterleitungen")

    https_url = f"https://{hostname}:{port}/" if port != 443 else f"https://{hostname}/"
    http_url  = f"http://{hostname}/"

    # HTTP → HTTPS Redirect
    try:
        resp = get(http_url, verify_ssl=False, allow_redirects=False, timeout=8)
        sc = get_status(resp)
        if sc in (301, 302, 307, 308):
            loc = get_header(resp, "Location", "")
            if loc.startswith("https://"):
                record("http_behavior", "HTTP→HTTPS Redirect", "pass",
                       f"HTTP {sc} → {loc[:60]}")
            else:
                record("http_behavior", "HTTP→HTTPS Redirect", "warn",
                       f"Weiterleitung vorhanden, Ziel nicht HTTPS: {loc[:60]}")
        else:
            record("http_behavior", "HTTP→HTTPS Redirect", "fail",
                   f"Kein Redirect – HTTP antwortet mit {sc}")
    except Exception as e:
        record("http_behavior", "HTTP→HTTPS Redirect", "warn",
               f"HTTP-Port nicht erreichbar ({e}) – evtl. Cloudflare blockiert")

    # HTTPS erreichbar
    try:
        resp = get(https_url, verify_ssl=verify_ssl, allow_redirects=True)
        sc = get_status(resp)
        if sc < 400:
            record("http_behavior", "HTTPS erreichbar", "pass", f"HTTP {sc}")
        else:
            record("http_behavior", "HTTPS erreichbar", "warn", f"HTTP {sc}")
    except Exception as e:
        record("http_behavior", "HTTPS erreichbar", "fail", str(e))
        return

    # Erlaubte HTTP-Methoden
    dangerous_methods = ["TRACE", "TRACK", "DELETE", "PUT", "CONNECT"]
    if HAS_REQUESTS:
        for method in dangerous_methods:
            try:
                r = requests.request(method, https_url, verify=verify_ssl, timeout=5,
                                     allow_redirects=False)
                sc = r.status_code
                if sc in (405, 501, 403):
                    record("http_behavior", f"Methode {method}", "pass",
                           f"abgelehnt ({sc})")
                elif sc == 200 and method == "TRACE":
                    record("http_behavior", f"Methode {method}", "fail",
                           "TRACE aktiv – XST-Angriff möglich!")
                else:
                    record("http_behavior", f"Methode {method}", "warn",
                           f"unerwarteter Status {sc}")
            except Exception as e:
                record("http_behavior", f"Methode {method}", "warn", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# 4. PROXY-KETTE (Cloudflare-Header)
# ─────────────────────────────────────────────────────────────────────────────

def test_proxy_chain(hostname, port, verify_ssl):
    section("4 · Proxy-Kette (Cloudflare → Zoraxy → nginx)")

    url = f"https://{hostname}:{port}/" if port != 443 else f"https://{hostname}/"
    try:
        resp = get(url, verify_ssl=verify_ssl)
    except Exception as e:
        fail(f"Verbindung fehlgeschlagen: {e}")
        return

    # Cloudflare-spezifische Header
    cf_ray = get_header(resp, "CF-Ray", "")
    if cf_ray:
        record("proxy_chain", "Cloudflare CF-Ray", "pass", cf_ray)
    else:
        record("proxy_chain", "Cloudflare CF-Ray", "warn",
               "CF-Ray fehlt – kein Cloudflare oder abgefangen?")

    cf_cache = get_header(resp, "CF-Cache-Status", "")
    if cf_cache:
        record("proxy_chain", "Cloudflare Cache-Status", "pass", cf_cache)
    else:
        record("proxy_chain", "Cloudflare Cache-Status", "warn",
               "CF-Cache-Status fehlt")

    cf_connecting_ip = get_header(resp, "CF-Connecting-IP", "")
    if cf_connecting_ip:
        record("proxy_chain", "CF-Connecting-IP in Response", "warn",
               f"IP wird in Response exponiert: {cf_connecting_ip}")
    else:
        record("proxy_chain", "CF-Connecting-IP in Response", "pass",
               "IP nicht in Response exponiert (korrekt)")

    # nginx-Signatur
    server = get_header(resp, "Server", "")
    if "nginx" in server.lower():
        record("proxy_chain", "nginx-Banner", "warn",
               f"nginx-Version sichtbar: '{server}' – server_tokens off empfohlen")
    elif server:
        record("proxy_chain", "nginx-Banner", "pass", f"Server: '{server}'")
    else:
        record("proxy_chain", "nginx-Banner", "pass", "Server-Header leer")

    # X-Forwarded-For darf nicht vom Client manipulierbar in der App ankommen
    try:
        spoofed_ip = "1.3.3.7"
        resp2 = get(url, verify_ssl=verify_ssl,
                    extra_headers={"X-Forwarded-For": spoofed_ip})
        # Wir können nicht direkt sehen was Django empfängt,
        # aber wir prüfen ob der Header auch in der Antwort erscheint
        reflected = get_header(resp2, "X-Forwarded-For", "")
        if spoofed_ip in reflected:
            record("proxy_chain", "X-Forwarded-For Spoofing", "warn",
                   "Gespoofter Header wird in Response reflektiert")
        else:
            record("proxy_chain", "X-Forwarded-For Spoofing", "pass",
                   "X-Forwarded-For nicht in Response reflektiert")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 5. APPLIKATIONS-TESTS
# ─────────────────────────────────────────────────────────────────────────────

def test_application(hostname, port, verify_ssl):
    section("5 · Applikations-Sicherheit (Django)")

    base = f"https://{hostname}:{port}" if port != 443 else f"https://{hostname}"

    # 5a – Login-Seite erreichbar
    try:
        resp = get(f"{base}/login/", verify_ssl=verify_ssl, allow_redirects=True)
        sc = get_status(resp)
        if sc == 200:
            record("application", "Login-Seite", "pass", "/login/ erreichbar")
        else:
            record("application", "Login-Seite", "warn", f"HTTP {sc}")
    except Exception as e:
        record("application", "Login-Seite", "warn", str(e))

    # 5b – /admin/ muss gesperrt oder umgeleitet sein (Django-Admin ist /djadmin/)
    for admin_path in ["/admin/", "/admin/login/"]:
        try:
            resp = get(f"{base}{admin_path}", verify_ssl=verify_ssl,
                       allow_redirects=False)
            sc = get_status(resp)
            if sc in (301, 302, 307, 308):
                loc = get_header(resp, "Location", "")
                if "djadmin" in loc or "login" in loc:
                    record("application", f"Admin-URL {admin_path}", "pass",
                           f"Redirect → {loc[:60]}")
                else:
                    record("application", f"Admin-URL {admin_path}", "warn",
                           f"Redirect → {loc[:60]}")
            elif sc == 404:
                record("application", f"Admin-URL {admin_path}", "pass",
                       "404 – nicht erreichbar (OK)")
            elif sc == 403:
                record("application", f"Admin-URL {admin_path}", "pass",
                       "403 Forbidden (OK)")
            elif sc == 200:
                record("application", f"Admin-URL {admin_path}", "fail",
                       "Standard-Admin-URL öffentlich zugänglich!")
            else:
                record("application", f"Admin-URL {admin_path}", "warn",
                       f"HTTP {sc}")
        except Exception as e:
            record("application", f"Admin-URL {admin_path}", "warn", str(e))

    # 5c – Cookie-Flags prüfen (nach Login-Seite)
    try:
        resp = get(f"{base}/login/", verify_ssl=verify_ssl, allow_redirects=True)
        if HAS_REQUESTS:
            for cookie in resp.cookies:
                name = cookie.name
                secure = cookie.secure
                httponly = "httponly" in (cookie._rest or {}) or \
                           getattr(cookie, "has_nonstandard_attr", lambda _: False)("HttpOnly")
                samesite = cookie.get_nonstandard_attr("SameSite") if hasattr(cookie, "get_nonstandard_attr") else None

                # Aus Set-Cookie-Header analysieren
                raw_sc = resp.headers.get("Set-Cookie", "")
                if name.lower() in ("sessionid", "csrftoken"):
                    if "Secure" in raw_sc:
                        record("application", f"Cookie {name} Secure", "pass", "Secure-Flag gesetzt")
                    else:
                        record("application", f"Cookie {name} Secure", "fail", "Secure-Flag fehlt!")
                    if "HttpOnly" in raw_sc and name.lower() == "sessionid":
                        record("application", f"Cookie {name} HttpOnly", "pass", "HttpOnly-Flag gesetzt")
                    elif name.lower() == "sessionid":
                        record("application", f"Cookie {name} HttpOnly", "fail", "HttpOnly fehlt!")
                    if "SameSite" in raw_sc:
                        record("application", f"Cookie {name} SameSite", "pass",
                               raw_sc.split("SameSite=")[1].split(";")[0].strip() if "SameSite=" in raw_sc else "gesetzt")
                    else:
                        record("application", f"Cookie {name} SameSite", "warn", "SameSite nicht gesetzt")
    except Exception as e:
        info(f"Cookie-Prüfung: {e}")

    # 5d – Brute-Force-Schutz (Login mehrfach probieren)
    section_info = "Rate-Limiting / Brute-Force"
    try:
        blocked = False
        for i in range(6):
            if HAS_REQUESTS:
                r = requests.post(
                    f"{base}/login/",
                    data={"username": "testuser_audit", "password": f"falsch_{i}"},
                    verify=verify_ssl, allow_redirects=False, timeout=5
                )
                if r.status_code in (429, 403):
                    record("application", "Brute-Force-Schutz", "pass",
                           f"Rate-Limit nach {i+1} Versuchen (HTTP {r.status_code})")
                    blocked = True
                    break
        if not blocked:
            record("application", "Brute-Force-Schutz", "warn",
                   "Kein HTTP-Level-Rate-Limit erkannt (6 Versuche ohne 429/403) "
                   "– ggf. App-seitig oder fail2ban aktiv")
    except Exception as e:
        record("application", "Brute-Force-Schutz", "warn", str(e))

    # 5e – CSRF-Token vorhanden
    try:
        resp = get(f"{base}/login/", verify_ssl=verify_ssl, allow_redirects=True)
        if HAS_REQUESTS:
            body = resp.text
        else:
            body = resp.read().decode("utf-8", errors="replace")
        if "csrfmiddlewaretoken" in body or "csrftoken" in body:
            record("application", "CSRF-Token", "pass",
                   "csrfmiddlewaretoken im Formular gefunden")
        else:
            record("application", "CSRF-Token", "warn",
                   "csrfmiddlewaretoken nicht im HTML gefunden")
    except Exception as e:
        record("application", "CSRF-Token", "warn", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# 6. INFORMATION-DISCLOSURE (sensible Pfade)
# ─────────────────────────────────────────────────────────────────────────────

SENSITIVE_PATHS = [
    ("/.env",            "Environment-Datei"),
    ("/.git/config",     "Git-Repository-Config"),
    ("/wp-admin/",       "WordPress Admin"),
    ("/phpmyadmin/",     "phpMyAdmin"),
    ("/server-status",   "nginx/Apache server-status"),
    ("/robots.txt",      "robots.txt (Info)"),
    ("/sitemap.xml",     "sitemap.xml (Info)"),
    ("/static/",         "Static-Files"),
    ("/media/",          "Media-Files"),
    ("/api/",            "API-Endpoint"),
    ("/__debug__/",      "Django Debug Toolbar"),
    ("/djadmin/",        "Django Admin (umbenannt)"),
]

def test_disclosure(hostname, port, verify_ssl):
    section("6 · Information-Disclosure & sensible Pfade")

    base = f"https://{hostname}:{port}" if port != 443 else f"https://{hostname}"

    for path, label in SENSITIVE_PATHS:
        try:
            resp = get(f"{base}{path}", verify_ssl=verify_ssl,
                       allow_redirects=False, timeout=8)
            sc = get_status(resp)

            if path in ("/.env", "/.git/config") and sc == 200:
                record("disclosure", label, "fail",
                       f"KRITISCH: {path} öffentlich zugänglich! (HTTP {sc})")
            elif path in ("/server-status", "/__debug__/") and sc == 200:
                record("disclosure", label, "fail",
                       f"Diagnose-Endpoint offen: {path} (HTTP {sc})")
            elif sc == 200 and path in ("/robots.txt", "/sitemap.xml", "/static/", "/media/"):
                record("disclosure", label, "pass", f"erreichbar (HTTP {sc}) – OK")
            elif sc in (301, 302, 307, 308):
                loc = get_header(resp, "Location", "")
                record("disclosure", label, "pass",
                       f"Redirect → {loc[:60]} (HTTP {sc})")
            elif sc in (403, 404):
                record("disclosure", label, "pass", f"gesperrt/nicht vorhanden (HTTP {sc})")
            elif sc == 200:
                record("disclosure", label, "warn", f"zugänglich (HTTP {sc})")
            else:
                record("disclosure", label, "pass", f"HTTP {sc}")
        except Exception as e:
            record("disclosure", label, "warn", f"Fehler: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# ZUSAMMENFASSUNG
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(hostname, output_json):
    s = results["summary"]
    total = s["pass"] + s["warn"] + s["fail"]

    print(f"\n{'═'*60}")
    print(f"{BOLD}  ZUSAMMENFASSUNG – {hostname}{RESET}")
    print(f"{'═'*60}")
    print(f"  {_color('✓ PASS', GREEN)}: {s['pass']}/{total}")
    print(f"  {_color('! WARN', YELLOW)}: {s['warn']}/{total}")
    print(f"  {_color('✗ FAIL', RED)}: {s['fail']}/{total}")

    if s["fail"] == 0 and s["warn"] <= 3:
        print(f"\n  {_color('Gesamtbewertung: SEHR GUT', GREEN + BOLD)}")
    elif s["fail"] == 0:
        print(f"\n  {_color('Gesamtbewertung: GUT (Verbesserungen empfohlen)', YELLOW + BOLD)}")
    elif s["fail"] <= 2:
        print(f"\n  {_color('Gesamtbewertung: AUSREICHEND – Probleme beheben!', YELLOW + BOLD)}")
    else:
        print(f"\n  {_color('Gesamtbewertung: KRITISCH – sofortige Maßnahmen!', RED + BOLD)}")

    print(f"{'═'*60}\n")

    if output_json:
        print(json.dumps(results, indent=2, default=str))


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Externe Sicherheitsprüfung: Cloudflare → Zoraxy → nginx → Django"
    )
    parser.add_argument("hostname", help="Hostname / Domain (z.B. meine-domain.de)")
    parser.add_argument("--port",      type=int, default=443, help="HTTPS-Port (Standard: 443)")
    parser.add_argument("--no-verify", action="store_true",
                        help="SSL-Zertifikat NICHT prüfen (nur für Debugging!)")
    parser.add_argument("--json",      action="store_true",
                        help="Ergebnisse zusätzlich als JSON ausgeben")
    args = parser.parse_args()

    verify_ssl = not args.no_verify

    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  Externes Sicherheits-Audit{RESET}")
    print(f"{BOLD}  Ziel: {args.hostname}:{args.port}{RESET}")
    print(f"{BOLD}  Zeit: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print(f"{BOLD}  Kette: Cloudflare → Zoraxy → nginx → Django{RESET}")
    if not verify_ssl:
        print(f"  {_color('WARNUNG: SSL-Verifikation deaktiviert!', RED)}")
    if not HAS_REQUESTS:
        print(f"  {_color('Hinweis: requests nicht installiert – eingeschränkte Tests', YELLOW)}")
        print(f"  {_color('  → pip install requests', YELLOW)}")
    print(f"{BOLD}{'═'*60}{RESET}")

    results["meta"] = {
        "hostname": args.hostname,
        "port":     args.port,
        "timestamp": datetime.datetime.now().isoformat(),
        "verify_ssl": verify_ssl,
    }

    test_tls(args.hostname, args.port)
    test_headers(args.hostname, args.port, verify_ssl)
    test_http_behavior(args.hostname, args.port, verify_ssl)
    test_proxy_chain(args.hostname, args.port, verify_ssl)
    test_application(args.hostname, args.port, verify_ssl)
    test_disclosure(args.hostname, args.port, verify_ssl)

    print_summary(args.hostname, args.json)


if __name__ == "__main__":
    main()
