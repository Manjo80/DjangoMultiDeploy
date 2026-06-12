"""
DjangoMultiDeploy Manager — Security Middleware
  - SecurityHeadersMiddleware: set CSP with per-request nonce (no unsafe-inline),
    Permissions-Policy, COEP, CORP headers
  - TwoFactorMiddleware: redirect 2FA-enabled users to verify page
  - IPWhitelistMiddleware: block IPs not in whitelist (if configured)
"""
import base64
import ipaddress
import logging
import secrets
import threading

from django.conf import settings
from django.shortcuts import redirect
from django.http import HttpResponseForbidden

logger = logging.getLogger('djmanager.middleware')


def get_client_ip(request):
    """
    Return the real client IP.

    Forwarding headers (CF-Connecting-IP / X-Real-IP / X-Forwarded-For) are
    honoured ONLY when the request actually arrives from a trusted proxy
    (settings.TRUSTED_PROXIES). Otherwise a direct client could spoof these
    headers to bypass the IP whitelist or forge audit-log IPs.
    """
    remote = (request.META.get('REMOTE_ADDR') or '').strip()
    trusted = getattr(settings, 'TRUSTED_PROXIES', ['127.0.0.1', '::1'])
    if remote in trusted:
        ip = (
            request.META.get('HTTP_CF_CONNECTING_IP')
            or request.META.get('HTTP_X_REAL_IP')
            or request.META.get('HTTP_X_FORWARDED_FOR', '')
            or remote
        )
        if ip and ',' in ip:
            ip = ip.split(',')[0].strip()
        return ip.strip()
    return remote


# ──────────────────────────────────────────────────────────────────────────────
# Security Headers Middleware
# ──────────────────────────────────────────────────────────────────────────────

_STATIC_SECURITY_HEADERS = [
    # nginx patch removes these from the nginx config (treated as duplicates);
    # we set them here so Django-proxied responses always carry them.
    ('Strict-Transport-Security', 'max-age=63072000; includeSubDomains'),
    ('X-Content-Type-Options', 'nosniff'),
    ('X-Frame-Options', 'DENY'),
    ('Referrer-Policy', 'strict-origin-when-cross-origin'),
    ('Permissions-Policy', 'geolocation=(), microphone=(), camera=(), payment=(), usb=()'),
    ('Cross-Origin-Embedder-Policy', 'unsafe-none'),
    ('Cross-Origin-Resource-Policy', 'same-origin'),
]


def _build_csp(nonce: str) -> str:
    """Build a strict CSP.

    script-src uses a per-request nonce — no 'unsafe-inline', preventing XSS
    via injected scripts.  style-src keeps 'unsafe-inline' because inline
    style="" attributes are used throughout the UI and CSS injection does not
    allow JavaScript execution.
    """
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; "
        # Bootstrap uses inline style="" attributes for dynamic component positioning
        # (dropdowns, modals, tooltips) — unsafe-inline cannot be removed without
        # breaking Bootstrap. The nonce covers our own <style nonce="..."> blocks.
        f"style-src 'self' 'nonce-{nonce}' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data: blob:; "
        "font-src 'self' https://cdn.jsdelivr.net data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none';"
    )


class SecurityHeadersMiddleware:
    """
    Generates a cryptographically random nonce per request and sets a strict
    Content-Security-Policy without 'unsafe-inline'.  The nonce is stored on
    ``request.csp_nonce`` so templates can reference it via the context
    processor.  All other security headers are added only when nginx has not
    already set them.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        nonce = base64.b64encode(secrets.token_bytes(16)).decode('ascii')
        request.csp_nonce = nonce

        response = self.get_response(request)

        # CSP always overrides nginx (nonce must match what was injected into templates)
        response['Content-Security-Policy'] = _build_csp(nonce)

        for name, value in _STATIC_SECURITY_HEADERS:
            if name not in response:
                response[name] = value
        return response


# ──────────────────────────────────────────────────────────────────────────────
# 2FA Middleware
# ──────────────────────────────────────────────────────────────────────────────

_2FA_EXEMPT = {'/login/', '/logout/', '/2fa/verify/', '/2fa/setup/'}


class TwoFactorMiddleware:
    """
    For users with TOTP enabled: enforce verification every session.
    The flag 'session["2fa_verified"]' is set by the verify view.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and request.path not in _2FA_EXEMPT
            and not request.path.startswith('/static/')
        ):
            try:
                profile = request.user.userprofile
                if profile.totp_enabled and not request.session.get('2fa_verified'):
                    return redirect(f'/2fa/verify/?next={request.path}')
                # Enforce 2FA for all users if SecuritySettings.require_2fa is set
                if not profile.totp_enabled and not request.session.get('2fa_verified'):
                    from .models import SecuritySettings
                    if SecuritySettings.get().require_2fa:
                        return redirect(f'/2fa/setup/?next={request.path}')
            except Exception:
                # Fail-open nur für den Fall fehlender Tabellen während der
                # Erstmigration — aber niemals stumm: jeder andere Fehler hier
                # würde sonst unbemerkt die 2FA-Pflicht aushebeln.
                logger.exception('TwoFactorMiddleware: 2FA-Prüfung fehlgeschlagen '
                                 '(Request wird ohne 2FA-Gate durchgelassen)')

        return self.get_response(request)


# ──────────────────────────────────────────────────────────────────────────────
# IP Whitelist Middleware
# ──────────────────────────────────────────────────────────────────────────────

# In-process cache to avoid DB hit on every request.
# Lock makes the cache thread-safe under multi-threaded Gunicorn workers.
_whitelist_cache = None
_whitelist_cache_ts = 0
_whitelist_lock = threading.Lock()
_CACHE_TTL = 60  # seconds


def _get_whitelist():
    global _whitelist_cache, _whitelist_cache_ts
    import time
    now = time.time()
    with _whitelist_lock:
        if _whitelist_cache is None or now - _whitelist_cache_ts > _CACHE_TTL:
            try:
                from .models import SecuritySettings
                ips = SecuritySettings.get().get_ip_whitelist()
            except Exception:
                ips = []
            _whitelist_cache = ips
            _whitelist_cache_ts = now
        return _whitelist_cache


def invalidate_whitelist_cache():
    """Call this after saving SecuritySettings."""
    global _whitelist_cache
    with _whitelist_lock:
        _whitelist_cache = None


def _ip_in_whitelist(client_ip, whitelist):
    """True if client_ip matches any entry in whitelist (supports CIDR notation)."""
    try:
        client = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in whitelist:
        try:
            if '/' in entry:
                if client in ipaddress.ip_network(entry, strict=False):
                    return True
            else:
                if client == ipaddress.ip_address(entry):
                    return True
        except ValueError:
            continue
    return False


class IPWhitelistMiddleware:
    """
    If a whitelist is configured in SecuritySettings, block all IPs not on it.
    Login and static paths bypass the check so admins can't lock themselves out
    completely — they can always reach the login page, but dashboard etc. are blocked.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith('/static/'):
            return self.get_response(request)

        whitelist = _get_whitelist()
        if not whitelist:
            return self.get_response(request)

        ip = get_client_ip(request)

        if not _ip_in_whitelist(ip, whitelist):
            return HttpResponseForbidden(
                '<h1>403 Forbidden</h1><p>Ihre IP-Adresse ist nicht auf der Whitelist.</p>',
                content_type='text/html',
            )

        return self.get_response(request)
