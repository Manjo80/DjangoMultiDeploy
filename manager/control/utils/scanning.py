"""
HTTP/TLS Security Scanner and Port Scanner utilities for DjangoMultiDeploy.
"""
import os
import shutil
import ssl
import tempfile
import socket
import ipaddress

_CURL = shutil.which('curl') or '/usr/bin/curl'
_WGET = shutil.which('wget') or '/usr/bin/wget'
import datetime
import logging
import concurrent.futures
import urllib.request
import urllib.error
from urllib.parse import urlparse as _urlparse_scheme


def _safe_urlopen(url, **kwargs):
    """Open a URL only if scheme is http or https — rejects file:/, custom schemes."""
    scheme = _urlparse_scheme(url).scheme
    if scheme not in ('http', 'https'):
        raise ValueError(f'Disallowed URL scheme: {scheme!r}')
    return urllib.request.urlopen(url, **kwargs)  # nosec B310 — scheme validated above

# Import scan_log to register the in-memory log handler on first import.
from . import scan_log as _scan_log  # noqa: F401

logger = logging.getLogger('djmanager.scanner')

_DNS_TIMEOUT = 5  # seconds for DNS resolution


def _getaddrinfo(host, port, *args, timeout=_DNS_TIMEOUT):
    """socket.getaddrinfo with a timeout (the stdlib call has none)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(socket.getaddrinfo, host, port, *args)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise socket.gaierror(f'DNS-Timeout nach {timeout}s für {host}')


def _http_get(url, timeout=10, verify_ssl=True, follow_redirects=False):
    """
    Fetch a URL and return (status_code, headers_dict, body_bytes, final_url, error).
    headers_dict keys are lowercased.
    Connects directly to the hostname without any loopback/hairpin-NAT tricks —
    the scanner behaves like an external client.
    """
    if verify_ssl:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.set_ciphers('DEFAULT:@SECLEVEL=1')
        except ssl.SSLError:
            pass

    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    opener.addheaders = [('User-Agent', 'DjangoMultiDeploySecurityScanner/1.0')]
    if not follow_redirects:
        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener.add_handler(NoRedirectHandler())
    try:
        with opener.open(url, timeout=timeout) as resp:
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            sc_all = resp.headers.get_all('set-cookie') if hasattr(resp.headers, 'get_all') else None
            if sc_all and len(sc_all) > 1:
                hdrs['set-cookie'] = '\n'.join(sc_all)
            body = resp.read(4096)
            return resp.status, hdrs, body, resp.url, None
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        if e.headers and hasattr(e.headers, 'get_all'):
            sc_all = e.headers.get_all('set-cookie')
            if sc_all and len(sc_all) > 1:
                hdrs['set-cookie'] = '\n'.join(sc_all)
        return e.code, hdrs, b'', url, None
    except urllib.error.URLError as e:
        logger.debug('HTTP GET URLError %s — %s', url, e.reason)
        return None, {}, b'', url, str(e.reason)
    except Exception as e:
        logger.debug('HTTP GET Fehler %s — %s', url, e)
        return None, {}, b'', url, str(e)


def _check_tls(hostname, port=443):
    """
    Return TLS info dict for hostname:port.
    Connects directly to the hostname — no hairpin-NAT bypass.
    The scanner behaves like an external client checking the certificate
    that real users see (typically the reverse-proxy cert).
    """
    result = {
        'reachable': False,
        'tls_version': None,
        'cipher': None,
        'cert_valid': False,
        'cert_expiry': None,
        'cert_days_left': None,
        'cert_subject': None,
        'cert_issuer': None,
        'error': None,
    }
    logger.debug('TLS-Check: %s:%d', hostname, port)
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                result['reachable'] = True
                result['tls_version'] = ssock.version()
                cipher = ssock.cipher()
                result['cipher'] = cipher[0] if cipher else None
                cert = ssock.getpeercert()
                subj = dict(x[0] for x in cert.get('subject', []))
                result['cert_subject'] = subj.get('commonName', '')
                issuer = dict(x[0] for x in cert.get('issuer', []))
                result['cert_issuer'] = issuer.get('organizationName', issuer.get('commonName', ''))
                not_after = cert.get('notAfter', '')
                if not_after:
                    exp = datetime.datetime.strptime(not_after, '%b %d %H:%M:%S %Y %Z')
                    result['cert_expiry'] = exp.strftime('%Y-%m-%d')
                    result['cert_days_left'] = (exp - datetime.datetime.utcnow()).days
                result['cert_valid'] = True
                logger.debug('TLS OK: %s — %s, Zert. gültig bis %s (%d Tage)',
                             hostname, result['tls_version'], result['cert_expiry'],
                             result['cert_days_left'] or 0)
    except ssl.SSLCertVerificationError as e:
        result['reachable'] = True
        result['cert_valid'] = False
        result['error'] = f'Zertifikat ungültig: {e}'
        logger.warning('TLS Zertifikatsfehler %s:%d — %s', hostname, port, e)
        # Best-effort: get TLS version/cipher even for invalid certs
        try:
            ctx_nv = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx_nv.check_hostname = False
            ctx_nv.verify_mode = ssl.CERT_NONE
            with socket.create_connection((hostname, port), timeout=10) as s2:
                with ctx_nv.wrap_socket(s2, server_hostname=hostname) as ss2:
                    result['tls_version'] = ss2.version()
                    c2 = ss2.cipher()
                    result['cipher'] = c2[0] if c2 else None
        except Exception:
            pass
    except ssl.SSLError as e:
        result['reachable'] = True
        result['error'] = f'TLS-Fehler: {e.reason or str(e)}'
        logger.warning('TLS SSL-Fehler %s:%d — %s', hostname, port, e.reason or e)
    except ConnectionRefusedError:
        result['error'] = 'Verbindung abgelehnt'
        logger.warning('TLS Verbindung abgelehnt: %s:%d', hostname, port)
    except socket.timeout:
        result['error'] = 'Timeout'
        logger.warning('TLS Timeout: %s:%d', hostname, port)
    except OSError as e:
        result['error'] = str(e)
        logger.warning('TLS OSError %s:%d — %s', hostname, port, e)
    except Exception as e:
        result['error'] = str(e)
        logger.warning('TLS unbekannter Fehler %s:%d — %s', hostname, port, e)
    return result


def _check_config_leaks(base_url, timeout=8):
    """Check for common configuration / sensitive file leaks."""
    PATHS = [
        ('/.env',              'Env-Datei (.env)'),
        ('/.env.local',        '.env.local'),
        ('/.env.production',   '.env.production'),
        ('/.git/HEAD',         'Git-Repository (.git/HEAD)'),
        ('/.git/config',       'Git-Konfiguration (.git/config)'),
        ('/backup.zip',        'Backup-Archiv (backup.zip)'),
        ('/backup.tar.gz',     'Backup-Archiv (backup.tar.gz)'),
        ('/db.sqlite3',        'SQLite-Datenbank (db.sqlite3)'),
        ('/phpinfo.php',       'phpinfo.php'),
        ('/wp-login.php',      'WordPress-Login (wp-login.php)'),
        ('/admin/login/',      'Django-Admin erreichbar'),
        ('/djadmin/',          'Django-Admin (djadmin/) erreichbar'),
        ('/robots.txt',        'robots.txt (Info)'),
    ]
    _REDIRECT_FALSE_POSITIVE = {
        '/.env', '/.env.local', '/.env.production',
        '/backup.zip', '/backup.tar.gz', '/db.sqlite3',
        '/phpinfo.php', '/wp-login.php',
    }

    def _probe_path(path, label):
        url = base_url.rstrip('/') + path
        status, hdrs, body, _, err = _http_get(url, timeout=timeout, verify_ssl=False)
        if err:
            return None
        if path in _REDIRECT_FALSE_POSITIVE:
            if status != 200:
                return None
        elif status not in (200, 301, 302, 307, 308):
            return None
        severity = 'critical'
        note = ''
        if path in ('/robots.txt', '/admin/login/', '/djadmin/'):
            severity = 'info'
        elif path.startswith('/.git'):
            severity = 'critical'
            note = 'Git-History enthält möglicherweise Secrets und Code-History!'
        elif path == '/.env':
            severity = 'critical'
            note = 'Env-Datei öffentlich zugänglich — Secrets exponiert!'
        return {
            'path': path,
            'label': label,
            'status': status,
            'severity': severity,
            'note': note,
        }

    import concurrent.futures
    path_order = {path: i for i, (path, _) in enumerate(PATHS)}
    leaks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(PATHS)) as ex:
        futures = {ex.submit(_probe_path, path, label): path for path, label in PATHS}
        for fut in concurrent.futures.as_completed(futures):
            item = fut.result()
            if item:
                leaks.append(item)
                if item['severity'] == 'critical':
                    logger.warning('Kritisches Konfigurationsleck gefunden: %s (HTTP %s)',
                                   item['path'], item['status'])
    leaks.sort(key=lambda x: path_order.get(x['path'], 999))
    return leaks


def _check_security_headers(headers):
    """Analyse response headers and return list of findings."""
    findings = []

    # fix_location: where to fix the issue
    # 'nginx'  = nginx config (add_header / server_tokens)
    # 'env'    = .env file (Django env vars like SECURE_HSTS_SECONDS)
    # 'webapp' = Django settings.py / middleware
    # 'proxy'  = reverse proxy (Cloudflare, Zoraxy, etc.)
    _fix_locations = {
        'Strict-Transport-Security':       'env',        # SECURE_HSTS_SECONDS in .env
        'Content-Security-Policy':         'nginx',      # add_header CSP in nginx
        'X-Frame-Options':                 'webapp',     # X_FRAME_OPTIONS in settings.py
        'X-Content-Type-Options':          'webapp',     # SECURE_CONTENT_TYPE_NOSNIFF in settings.py
        'Referrer-Policy':                 'nginx',      # add_header Referrer-Policy in nginx
        'Permissions-Policy':              'nginx',      # add_header Permissions-Policy in nginx
        'Cross-Origin-Opener-Policy':      'nginx',      # add_header COOP in nginx
        'Cross-Origin-Embedder-Policy':    'nginx',      # add_header COEP in nginx
        'Cross-Origin-Resource-Policy':    'nginx',      # add_header CORP in nginx
        'Server':                          'nginx',      # server_tokens off in nginx
        'X-Powered-By':                    'nginx',      # proxy_hide_header X-Powered-By
    }

    def check(name, severity, present_ok, absent_msg, value_check_fn=None, ok_msg=None):
        val = headers.get(name.lower())
        loc = _fix_locations.get(name)
        if val is None:
            findings.append({'header': name, 'severity': severity, 'status': 'missing',
                             'value': None, 'msg': absent_msg, 'fix_location': loc})
        else:
            if value_check_fn:
                warn = value_check_fn(val)
                if warn:
                    findings.append({'header': name, 'severity': 'warning', 'status': 'weak',
                                     'value': val, 'msg': warn, 'fix_location': loc})
                else:
                    findings.append({'header': name, 'severity': 'ok', 'status': 'ok',
                                     'value': val, 'msg': ok_msg or 'OK', 'fix_location': loc})
            else:
                findings.append({'header': name, 'severity': 'ok', 'status': 'ok',
                                 'value': val, 'msg': ok_msg or 'OK', 'fix_location': loc})

    check('Strict-Transport-Security', 'high',
          present_ok=True,
          absent_msg='HSTS fehlt — Browser kann unverschlüsselt verbinden.',
          value_check_fn=lambda v: (
              'max-age zu kurz (empfohlen ≥ 31536000)'
              if 'max-age' in v.lower() and any(
                  int(p.split('=')[1]) < 31536000
                  for p in v.lower().split(';')
                  if 'max-age=' in p and p.split('=')[1].strip().isdigit()
              ) else None
          ))

    def csp_check(v):
        """Check CSP for dangerous directives.

        'unsafe-inline' in script-src (or default-src without an explicit
        script-src) is a real XSS vector.  'unsafe-inline' in style-src only
        enables CSS injection which cannot directly execute JavaScript, so it
        is reported at a lower level (info) rather than a hard warning.
        """
        warnings = []
        # Parse directives into a dict: name -> value-list
        directives = {}
        for part in v.split(';'):
            part = part.strip()
            if not part:
                continue
            tokens = part.split()
            if tokens:
                directives[tokens[0].lower()] = ' '.join(tokens[1:]).lower()

        # Determine the effective script policy
        script_policy = directives.get('script-src', directives.get('default-src', ''))
        if "'unsafe-inline'" in script_policy:
            warnings.append("'unsafe-inline' in script-src erlaubt XSS")

        if "'unsafe-eval'" in script_policy:
            warnings.append("'unsafe-eval' in script-src erlaubt Code-Injection")

        # style-src unsafe-inline: CSS injection risk (lower severity — no JS exec)
        style_policy = directives.get('style-src', directives.get('default-src', ''))
        if "'unsafe-inline'" in style_policy and "'unsafe-inline'" not in script_policy:
            warnings.append(
                "'unsafe-inline' in style-src (CSS-Injection möglich, kein JS)"
            )

        if not warnings:
            return None
        return '; '.join(warnings)

    check('Content-Security-Policy', 'high',
          present_ok=True,
          absent_msg='CSP fehlt — kein Schutz gegen XSS und Dateninjektionen.',
          value_check_fn=csp_check)

    check('X-Frame-Options', 'medium',
          present_ok=True,
          absent_msg='X-Frame-Options fehlt — Clickjacking möglich.',
          value_check_fn=lambda v: (
              None if v.upper() in ('DENY', 'SAMEORIGIN') else
              f'Wert "{v}" unbekannt. Empfohlen: DENY oder SAMEORIGIN.'
          ))

    check('X-Content-Type-Options', 'medium',
          present_ok=True,
          absent_msg='X-Content-Type-Options fehlt — MIME-Sniffing möglich.',
          value_check_fn=lambda v: (
              None if v.lower() == 'nosniff' else f'Wert "{v}" — sollte "nosniff" sein.'
          ))

    check('Referrer-Policy', 'low',
          present_ok=True,
          absent_msg='Referrer-Policy fehlt — URLs können an externe Seiten gesendet werden.')

    check('Permissions-Policy', 'low',
          present_ok=True,
          absent_msg='Permissions-Policy fehlt — Browser-Features nicht eingeschränkt.')

    xss = headers.get('x-xss-protection')
    if xss and xss.strip() == '0':
        findings.append({'header': 'X-XSS-Protection', 'severity': 'info', 'status': 'ok',
                         'value': xss, 'msg': 'Auf 0 gesetzt (browser-seitig deaktiviert, CSP bevorzugt)'})

    server = headers.get('server')
    if server:
        import re
        if re.search(r'[\d.]', server):
            findings.append({'header': 'Server', 'severity': 'low', 'status': 'weak',
                             'value': server,
                             'msg': 'Server-Header enthält Versionsinfos — per nginx: server_tokens off;'})
        else:
            findings.append({'header': 'Server', 'severity': 'ok', 'status': 'ok',
                             'value': server, 'msg': 'Kein Versions-Leak.'})

    powered = headers.get('x-powered-by')
    if powered:
        findings.append({'header': 'X-Powered-By', 'severity': 'low', 'status': 'weak',
                         'value': powered,
                         'msg': 'X-Powered-By gibt Technologie-Infos preis — sollte entfernt werden.'})

    coop = headers.get('cross-origin-opener-policy')
    if coop is None:
        findings.append({'header': 'Cross-Origin-Opener-Policy', 'severity': 'low', 'status': 'missing',
                         'value': None,
                         'msg': 'COOP fehlt — Schutz gegen Spectre/Cross-Origin-Leaks empfohlen. Empfohlen: same-origin'})
    else:
        findings.append({'header': 'Cross-Origin-Opener-Policy', 'severity': 'ok', 'status': 'ok',
                         'value': coop, 'msg': 'OK'})

    coep = headers.get('cross-origin-embedder-policy')
    if coep is None:
        findings.append({'header': 'Cross-Origin-Embedder-Policy', 'severity': 'low', 'status': 'missing',
                         'value': None,
                         'msg': 'COEP fehlt — für SharedArrayBuffer / high-res timers nötig. Empfohlen: require-corp'})
    else:
        findings.append({'header': 'Cross-Origin-Embedder-Policy', 'severity': 'ok', 'status': 'ok',
                         'value': coep, 'msg': 'OK'})

    corp = headers.get('cross-origin-resource-policy')
    if corp is None:
        findings.append({'header': 'Cross-Origin-Resource-Policy', 'severity': 'low', 'status': 'missing',
                         'value': None,
                         'msg': 'CORP fehlt — Ressourcen können Cross-Origin eingebettet werden. Empfohlen: same-origin'})
    else:
        findings.append({'header': 'Cross-Origin-Resource-Policy', 'severity': 'ok', 'status': 'ok',
                         'value': corp, 'msg': 'OK'})

    return findings


def _check_cors(headers):
    """Check CORS configuration for misconfigurations."""
    findings = []
    acao = headers.get('access-control-allow-origin')
    acac = headers.get('access-control-allow-credentials')
    if acao:
        if acao.strip() == '*':
            if acac and acac.lower() == 'true':
                findings.append({
                    'header': 'Access-Control-Allow-Origin + Credentials',
                    'severity': 'critical',
                    'status': 'weak',
                    'value': f'Origin: {acao}, Credentials: {acac}',
                    'msg': 'CORS-Wildcard (*) mit Credentials=true ist gefährlich — ermöglicht Cross-Origin-Datenklau!',
                    'fix_location': 'nginx',
                })
            else:
                findings.append({
                    'header': 'Access-Control-Allow-Origin',
                    'severity': 'medium',
                    'status': 'weak',
                    'value': acao,
                    'msg': 'CORS-Wildcard (*) erlaubt jeder Website Zugriff auf API-Antworten. Prüfen ob gewollt.',
                    'fix_location': 'nginx',
                })
        else:
            findings.append({
                'header': 'Access-Control-Allow-Origin',
                'severity': 'ok',
                'status': 'ok',
                'value': acao,
                'msg': f'CORS auf bestimmte Origin eingeschränkt: {acao}',
                'fix_location': 'nginx',
            })
    return findings


def _check_cookies(headers):
    """Parse Set-Cookie headers and check security flags."""
    raw = headers.get('set-cookie', '')
    if not raw:
        return []
    findings = []
    cookies = [c.strip() for c in raw.split('\n') if c.strip()]
    if not cookies:
        cookies = [raw]
    for cookie in cookies:
        parts = [p.strip().lower() for p in cookie.split(';')]
        name = cookie.split('=')[0].strip()
        flags = {
            'secure': any(p == 'secure' for p in parts),
            'httponly': any(p == 'httponly' for p in parts),
            'samesite': next((p.split('=')[1] for p in parts if p.startswith('samesite=')), None),
        }
        issues = []
        if not flags['secure']:
            issues.append({'flag': 'Secure', 'severity': 'high',
                          'msg': 'Secure-Flag fehlt — Cookie wird auch über HTTP gesendet.',
                          'fix_location': 'env',
                          'fix_hint': 'SESSION_COOKIE_SECURE=True und CSRF_COOKIE_SECURE=True in .env setzen'})
        if not flags['httponly']:
            issues.append({'flag': 'HttpOnly', 'severity': 'medium',
                          'msg': 'HttpOnly-Flag fehlt — Cookie per JavaScript auslesbar (XSS-Risiko).',
                          'fix_location': 'webapp',
                          'fix_hint': 'CSRF_COOKIE_HTTPONLY=True in settings.py (Achtung: JS-AJAX-Requests brauchen dann {% csrf_token %})'})
        if not flags['samesite']:
            issues.append({'flag': 'SameSite', 'severity': 'medium',
                          'msg': 'SameSite-Flag fehlt — CSRF-Risiko erhöht.',
                          'fix_location': 'webapp',
                          'fix_hint': 'SESSION_COOKIE_SAMESITE="Lax" in settings.py'})
        elif flags['samesite'] == 'none' and not flags['secure']:
            issues.append({'flag': 'SameSite=None', 'severity': 'high',
                          'msg': 'SameSite=None ohne Secure-Flag ist ungültig.',
                          'fix_location': 'webapp',
                          'fix_hint': 'SESSION_COOKIE_SAMESITE="Lax" oder Secure-Flag aktivieren'})
        findings.append({'name': name, 'issues': issues, 'flags': flags})
    return findings


def _check_http_redirect(hostname, port=80):
    """Check if HTTP (port 80) redirects to HTTPS."""
    try:
        addr = ipaddress.ip_address(hostname)
        url_host = f'[{hostname}]' if isinstance(addr, ipaddress.IPv6Address) else hostname
    except ValueError:
        url_host = hostname
    url = f'http://{url_host}:{port}/'
    try:
        status, hdrs, _, _, err = _http_get(url, timeout=8, verify_ssl=False, follow_redirects=False)
        if err:
            return {'available': False, 'redirects_to_https': False, 'error': err}
        location = hdrs.get('location', '')
        redirects = status in (301, 302, 307, 308) and location.startswith('https://')
        return {
            'available': True,
            'status': status,
            'redirects_to_https': redirects,
            'location': location,
            'error': None,
        }
    except Exception as e:
        return {'available': False, 'redirects_to_https': False, 'error': str(e)}


def run_http_security_scan(target_url, hostname=None, check_tls=True, local_port=None):
    """
    Comprehensive HTTP security scan for a target URL.

    Returns a dict with:
      - tls: TLS/certificate info (if check_tls and HTTPS)
      - http_redirect: HTTP→HTTPS redirect check
      - headers: security header findings
      - cookies: cookie flag analysis
      - config_leaks: sensitive file exposure
      - summary: {'critical': int, 'high': int, 'medium': int, 'low': int, 'ok': int}
      - error: str or None (fatal error preventing scan)

    local_port: if set (int/str), HTTP requests go to https://127.0.0.1:{local_port}/
                instead of target_url (hairpin-NAT bypass for servers that cannot
                reach their own public domain).  TLS check still uses hostname.
                HTTP→HTTPS redirect check is skipped when local_port is set.
    """
    logger.info('HTTP-Scan gestartet: %s (TLS=%s)', target_url, check_tls)

    result = {
        'target_url': target_url,
        'tls': None,
        'http_redirect': None,
        'headers': [],
        'cors': [],
        'cookies': [],
        'config_leaks': [],
        'summary': {'critical': 0, 'high': 0, 'medium': 0, 'low': 0, 'info': 0, 'ok': 0},
        'error': None,
    }

    is_https = target_url.startswith('https://')

    from urllib.parse import urlparse as _urlparse
    _parsed_url = _urlparse(target_url)
    _netloc_port = _parsed_url.port
    _tls_port = _netloc_port if _netloc_port else 443
    _http_port = 80

    if check_tls and is_https and hostname:
        result['tls'] = _check_tls(hostname, port=_tls_port)

    # HTTP redirect check: only when scanning externally (local_port bypass skips it
    # because port 80 is not the per-project nginx port and would still time out).
    if hostname and is_https and not local_port:
        result['http_redirect'] = _check_http_redirect(hostname, port=_http_port)

    # Resolve the actual URL to fetch: use local nginx port when provided to avoid
    # hairpin-NAT timeouts (server cannot reach its own public domain).
    if local_port:
        fetch_url = f'https://127.0.0.1:{local_port}/'
        logger.debug('HTTP-Scan hairpin-NAT bypass: %s → %s', target_url, fetch_url)
    else:
        fetch_url = target_url

    status, hdrs, body, final_url, err = _http_get(
        fetch_url, timeout=12, verify_ssl=False, follow_redirects=True
    )
    if err or status is None:
        result['error'] = f'Verbindungsfehler: {err or "keine Antwort"}'
        logger.warning('HTTP-Scan Verbindungsfehler %s — %s', target_url, err or 'keine Antwort')
        return result
    logger.debug('HTTP-Scan Verbindung OK: %s — HTTP %s', target_url, status)

    result['http_status'] = status
    result['final_url'] = final_url
    result['headers'] = _check_security_headers(hdrs)
    result['cors'] = _check_cors(hdrs)
    result['cookies'] = _check_cookies(hdrs)

    from urllib.parse import urlparse
    if local_port:
        scan_base = f'https://127.0.0.1:{local_port}'
    else:
        base_url = target_url.rstrip('/')
        parsed = urlparse(base_url)
        scan_base = f'{parsed.scheme}://{parsed.netloc}'
    result['config_leaks'] = _check_config_leaks(scan_base, timeout=6)

    sev_count = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0, 'info': 0, 'ok': 0, 'warning': 0}

    for h in result['headers']:
        s = h.get('severity', 'info')
        sev_count[s] = sev_count.get(s, 0) + 1

    for c in result['cors']:
        s = c.get('severity', 'info')
        sev_count[s] = sev_count.get(s, 0) + 1

    for ck in result['cookies']:
        for issue in ck.get('issues', []):
            s = issue.get('severity', 'medium')
            sev_count[s] = sev_count.get(s, 0) + 1

    for leak in result['config_leaks']:
        s = leak.get('severity', 'high')
        if s == 'critical':
            sev_count['critical'] += 1
        elif s == 'info':
            sev_count['info'] += 1
        else:
            sev_count['high'] += 1

    if result['tls']:
        tls = result['tls']
        if not tls.get('cert_valid'):
            sev_count['critical'] += 1
        elif tls.get('cert_days_left') is not None and tls['cert_days_left'] < 14:
            sev_count['critical'] += 1
        elif tls.get('cert_days_left') is not None and tls['cert_days_left'] < 30:
            sev_count['high'] += 1
        ver = tls.get('tls_version', '')
        if ver in ('TLSv1', 'TLSv1.1', 'SSLv3', 'SSLv2'):
            sev_count['high'] += 1

    if result['http_redirect'] and result['http_redirect'].get('available'):
        if not result['http_redirect'].get('redirects_to_https'):
            sev_count['high'] += 1

    sev_count['medium'] = sev_count.get('medium', 0) + sev_count.pop('warning', 0)
    result['summary'] = sev_count
    logger.info('HTTP-Scan abgeschlossen: %s — kritisch=%d hoch=%d mittel=%d',
                target_url, sev_count.get('critical', 0),
                sev_count.get('high', 0), sev_count.get('medium', 0))
    return result


def get_public_ip():
    """Detect the server's public IP address. Returns (ipv4, ipv6) tuple, either may be None."""
    ipv4 = None
    ipv6 = None
    services_v4 = [
        'https://api4.ipify.org',
        'https://ipv4.icanhazip.com',
        'https://checkip.amazonaws.com',
    ]
    services_v6 = [
        'https://api6.ipify.org',
        'https://ipv6.icanhazip.com',
    ]
    for url in services_v4:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'DjManager/1.0'})
            with _safe_urlopen(req.full_url, headers=dict(req.headers), timeout=5) as resp:
                ip = resp.read().decode().strip()
                if ip:
                    ipv4 = ip
                    break
        except Exception:
            continue
    for url in services_v6:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'DjManager/1.0'})
            with _safe_urlopen(req.full_url, headers=dict(req.headers), timeout=5) as resp:
                ip = resp.read().decode().strip()
                if ip:
                    ipv6 = ip
                    break
        except Exception:
            continue
    return ipv4, ipv6


# Well-known ports with service names and risk notes
_PORT_INFO = {
    21:    ('FTP',            'high',   'FTP überträgt Zugangsdaten im Klartext'),
    22:    ('SSH',            'info',   'SSH-Zugang — Brute-Force-Schutz empfohlen (fail2ban)'),
    23:    ('Telnet',         'critical','Telnet überträgt alles unverschlüsselt — sofort deaktivieren!'),
    25:    ('SMTP',           'medium', 'SMTP-Port offen — prüfen ob Relay erlaubt'),
    53:    ('DNS',            'medium', 'DNS-Port offen — öffentlicher Resolver? Zone-Transfer prüfen'),
    80:    ('HTTP',           'info',   'HTTP offen — sollte auf HTTPS umleiten'),
    110:   ('POP3',           'high',   'POP3 überträgt Passwörter im Klartext'),
    111:   ('rpcbind',        'high',   'rpcbind/portmapper offen — NFS-Angriffsfläche'),
    135:   ('MSRPC',          'high',   'Microsoft RPC — typisch für Windows-Systeme'),
    137:   ('NetBIOS-NS',     'high',   'NetBIOS Name Service — sollte nicht öffentlich sein'),
    139:   ('NetBIOS-SMB',    'high',   'NetBIOS/SMB — sollte nicht öffentlich sein'),
    143:   ('IMAP',           'medium', 'IMAP offen — prüfen ob TLS erzwungen wird'),
    443:   ('HTTPS',          'info',   'HTTPS offen'),
    445:   ('SMB',            'critical','SMB/Windows-Shares öffentlich — extrem gefährlich (EternalBlue)!'),
    465:   ('SMTPS',          'info',   'SMTP über SSL/TLS'),
    587:   ('SMTP/STARTTLS',  'info',   'SMTP Submission Port'),
    631:   ('IPP',            'medium', 'Drucker-Port (IPP) — sollte nicht öffentlich sein'),
    993:   ('IMAPS',          'info',   'IMAP über SSL/TLS'),
    995:   ('POP3S',          'info',   'POP3 über SSL/TLS'),
    1433:  ('MSSQL',          'critical','Microsoft SQL Server — Datenbank nicht öffentlich!'),
    1521:  ('Oracle DB',      'critical','Oracle Datenbank — nicht öffentlich!'),
    2049:  ('NFS',            'critical','NFS-Dateifreigabe öffentlich — sehr gefährlich!'),
    2181:  ('ZooKeeper',      'critical','ZooKeeper offen — ermöglicht Cluster-Übernahme'),
    3000:  ('Node.js/Dev',    'high',   'Entwicklungsserver offen — nicht für Produktion'),
    3306:  ('MySQL',          'critical','MySQL-Datenbank öffentlich — Brute-Force-Gefahr!'),
    3389:  ('RDP',            'critical','Remote Desktop offen — extrem hohes Angriffsrisiko!'),
    4443:  ('HTTPS-alt',      'info',   'Alternativer HTTPS-Port'),
    5000:  ('Flask/Dev',      'high',   'Entwicklungsserver offen — nicht für Produktion'),
    5432:  ('PostgreSQL',     'critical','PostgreSQL-Datenbank öffentlich — Brute-Force-Gefahr!'),
    5900:  ('VNC',            'critical','VNC Remote Desktop offen — extrem gefährlich!'),
    5985:  ('WinRM HTTP',     'high',   'Windows Remote Management offen'),
    6379:  ('Redis',          'critical','Redis ohne Auth öffentlich — Datenklau und RCE möglich!'),
    6443:  ('Kubernetes API', 'high',   'Kubernetes API-Server offen'),
    8000:  ('HTTP-Dev',       'high',   'Entwicklungsserver offen — nicht für Produktion'),
    8080:  ('HTTP-Proxy/Alt', 'medium', 'Alternativer HTTP-Port — TLS prüfen'),
    8443:  ('HTTPS-alt',      'info',   'Alternativer HTTPS-Port'),
    8888:  ('Jupyter',        'critical','Jupyter Notebook offen — führt beliebigen Code aus!'),
    9000:  ('PHP-FPM/misc',   'high',   'PHP-FPM oder sonstiger Dienst'),
    9090:  ('Prometheus',     'high',   'Prometheus-Metriken öffentlich — Daten-Leak'),
    9200:  ('Elasticsearch',  'critical','Elasticsearch offen — alle Daten ungeschützt lesbar!'),
    9300:  ('ES Transport',   'critical','Elasticsearch Cluster-Port offen'),
    11211: ('Memcached',      'critical','Memcached offen — DDoS-Amplification und Datenleck!'),
    27017: ('MongoDB',        'critical','MongoDB offen — alle Daten ungeschützt lesbar!'),
    27018: ('MongoDB',        'critical','MongoDB-shard offen'),
}


def run_port_scan(host, mode='common', port_start=1, port_end=1024, timeout=1.0, max_workers=50):
    """
    Scan TCP ports on host.

    mode:
      'common'  — scan the predefined list of well-known/risky ports
      'range'   — scan port_start..port_end (max 10000 ports)

    Returns dict with 'host', 'mode', 'open_ports', 'scanned', 'error'.
    """
    import concurrent.futures

    result = {
        'host': host,
        'mode': mode,
        'open_ports': [],
        'scanned': 0,
        'error': None,
    }

    logger.info('Port-Scan gestartet: %s (Modus=%s)', host, mode)

    try:
        resolved = _getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        addr_family = resolved[0][0]
        ip_addr = resolved[0][4][0]
    except socket.gaierror as e:
        result['error'] = f'DNS-Auflösung fehlgeschlagen: {e}'
        logger.warning('Port-Scan DNS-Fehler %s — %s', host, e)
        return result

    if mode == 'common':
        ports_to_scan = sorted(_PORT_INFO.keys())
    else:
        port_start = max(1, int(port_start))
        port_end   = min(65535, int(port_end))
        if port_end - port_start > 10000:
            port_end = port_start + 10000
        ports_to_scan = list(range(port_start, port_end + 1))

    result['scanned'] = len(ports_to_scan)

    def _probe(port):
        try:
            with socket.socket(addr_family, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                r = s.connect_ex((ip_addr, port))
                return port, r == 0
        except Exception:
            return port, False

    open_ports = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_probe, p): p for p in ports_to_scan}
        for fut in concurrent.futures.as_completed(futures):
            port, is_open = fut.result()
            if is_open:
                info = _PORT_INFO.get(port, ('Unknown', 'info', ''))
                open_ports.append({
                    'port':     port,
                    'service':  info[0],
                    'severity': info[1],
                    'note':     info[2],
                })

    open_ports.sort(key=lambda x: x['port'])
    result['open_ports'] = open_ports
    critical_count = sum(1 for p in open_ports if p['severity'] == 'critical')
    if critical_count:
        logger.warning('Port-Scan %s — %d kritische Port(s) offen: %s',
                       host, critical_count,
                       ', '.join(str(p['port']) for p in open_ports if p['severity'] == 'critical'))
    else:
        logger.info('Port-Scan abgeschlossen: %s — %d offene Port(s)', host, len(open_ports))
    return result


# ── Nuclei Scanner ────────────────────────────────────────────────────────────

import subprocess as _subprocess
import json as _json
import platform as _platform
import tempfile as _tempfile
import zipfile as _zipfile
import shutil as _shutil

_APT_GET = _shutil.which('apt-get') or '/usr/bin/apt-get'
_SNAP    = _shutil.which('snap')    or '/usr/bin/snap'

NUCLEI_BIN = '/usr/local/bin/nuclei'
NUCLEI_TEMPLATES = '/opt/nuclei-templates'

# Safe template categories for production scans (no fuzzing/injection)
NUCLEI_SAFE_TEMPLATES = [
    'http/technologies',
    'http/misconfiguration',
    'http/exposures',
    'http/headers',
    'http/takeovers',
]


def _install_nuclei():
    """Install nuclei binary — tries system package managers first, then GitHub release."""
    # 1. Check if already in PATH at a different location
    try:
        sys_bin = _shutil.which('nuclei')
        if sys_bin:
            try:
                _shutil.copy2(sys_bin, NUCLEI_BIN)
                os.chmod(NUCLEI_BIN, 0o755)  # nosec B103
            except Exception:
                pass
            return True
    except Exception:
        pass

    # 2. Try apt
    try:
        if _shutil.which('apt-get'):
            r = _subprocess.run(
                [_APT_GET, 'install', '-y', '--no-install-recommends', 'nuclei'],
                capture_output=True, timeout=60,
            )
            if r.returncode == 0 and _shutil.which('nuclei'):
                try:
                    _shutil.copy2(_shutil.which('nuclei'), NUCLEI_BIN)
                    os.chmod(NUCLEI_BIN, 0o755)  # nosec B103
                except Exception:
                    pass
                return True
    except Exception:
        pass

    # 3. Try via Go (apt install golang-go + go install)
    try:
        go_bin = _shutil.which('go')
        if not go_bin and _shutil.which('apt-get'):
            _subprocess.run([_APT_GET, 'install', '-y', '--no-install-recommends', 'golang-go'],
                            capture_output=True, timeout=120)
            go_bin = _shutil.which('go')
        if go_bin:
            go_env = os.environ.copy()
            go_env.setdefault('GOPATH', '/root/go')
            go_env['PATH'] = go_env['PATH'] + ':/root/go/bin:/usr/local/go/bin'
            r = _subprocess.run(
                [go_bin, 'install', 'github.com/projectdiscovery/nuclei/v3/cmd/nuclei@latest'],
                capture_output=True, timeout=300, env=go_env,
            )
            if r.returncode == 0:
                for candidate in ('/root/go/bin/nuclei', '/usr/local/go/bin/nuclei'):
                    if os.path.exists(candidate):
                        _shutil.copy2(candidate, NUCLEI_BIN)
                        os.chmod(NUCLEI_BIN, 0o755)  # nosec B103
                        return True
    except Exception:
        pass

    # 4. Try snap
    try:
        if _shutil.which('snap'):
            r = _subprocess.run(
                [_SNAP, 'install', 'nuclei', '--classic'],
                capture_output=True, timeout=120,
            )
            if r.returncode == 0 and _shutil.which('nuclei'):
                try:
                    _shutil.copy2(_shutil.which('nuclei'), NUCLEI_BIN)
                    os.chmod(NUCLEI_BIN, 0o755)  # nosec B103
                except Exception:
                    pass
                return True
    except Exception:
        pass

    # 5. GitHub release download (needs internet) — v3.x uses nuclei_VERSION_linux_ARCH.zip
    arch_map = {'x86_64': 'amd64', 'aarch64': 'arm64', 'armv7l': 'arm', 'i386': '386', 'i686': '386'}
    go_arch = arch_map.get(_platform.machine(), 'amd64')

    # Resolve latest version tag via GitHub API (so URL includes version number)
    version = 'v3.8.0'  # safe fallback
    try:
        import urllib.request as _ureq2
        with _safe_urlopen(
            'https://api.github.com/repos/projectdiscovery/nuclei/releases/latest',
            timeout=10
        ) as _resp:
            version = _json.loads(_resp.read()).get('tag_name', version)
    except Exception:
        pass
    ver_num = version.lstrip('v')  # "3.8.0"

    # New v3 filename format: nuclei_3.8.0_linux_amd64.zip
    url = f'https://github.com/projectdiscovery/nuclei/releases/download/{version}/nuclei_{ver_num}_linux_{go_arch}.zip'
    try:
        with _tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, 'nuclei.zip')

            if _shutil.which('curl'):
                r = _subprocess.run(
                    [_CURL, '-fsSL', '--connect-timeout', '15', '-o', zip_path, url],
                    timeout=120, capture_output=True,
                )
            elif _shutil.which('wget'):
                r = _subprocess.run(
                    [_WGET, '-q', '--timeout=15', '-O', zip_path, url],
                    timeout=120, capture_output=True,
                )
            else:
                import urllib.request as _req
                import ssl as _ssl
                ctx = _ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
                with _safe_urlopen(url, context=ctx, timeout=30) as resp:
                    with open(zip_path, 'wb') as f:
                        f.write(resp.read())
                r = type('R', (), {'returncode': 0})()

            if r.returncode != 0:
                logger.error('nuclei download failed (exit %d) url=%s', r.returncode, url)
                return False

            with _zipfile.ZipFile(zip_path) as zf:
                # Binary is at root of zip (no subdirectory in v3)
                binary = next((n for n in zf.namelist()
                               if n.lower() == 'nuclei' or
                               (n.lower().startswith('nuclei') and '/' not in n and not n.endswith('.txt'))), None)
                if not binary:
                    logger.error('nuclei binary not found in zip: %s', zf.namelist())
                    return False
                zf.extract(binary, tmp)
            _shutil.copy2(os.path.join(tmp, binary), NUCLEI_BIN)
            os.chmod(NUCLEI_BIN, 0o755)  # nosec B103
        return True
    except Exception as e:
        logger.error('nuclei install failed: %s', e)
        return False


def _nuclei_installed_version():
    """Return installed nuclei version string or '' if not installed."""
    if not os.path.exists(NUCLEI_BIN):
        return ''
    try:
        r = _subprocess.run([NUCLEI_BIN, '-version'], capture_output=True, text=True, timeout=10)
        for line in (r.stdout + r.stderr).splitlines():
            if 'nuclei' in line.lower() and ('version' in line.lower() or line.strip().startswith('v')):
                # Output is like: "Nuclei Engine Version: v3.8.0" or just "v3.8.0"
                parts = line.split()
                for p in parts:
                    if p.startswith('v') and p[1:2].isdigit():
                        return p.lstrip('v')
    except Exception:
        pass
    return ''


def _nuclei_latest_version():
    """Query GitHub API for latest nuclei release tag. Returns version string or ''."""
    try:
        import urllib.request as _ureq
        with _safe_urlopen(
            'https://api.github.com/repos/projectdiscovery/nuclei/releases/latest',
            timeout=10
        ) as resp:
            tag = _json.loads(resp.read()).get('tag_name', '')
            return tag.lstrip('v')
    except Exception:
        return ''


def nuclei_version_info():
    """Return dict with installed, latest, update_available."""
    installed = _nuclei_installed_version()
    latest    = _nuclei_latest_version()
    needs_update = bool(installed and latest and installed != latest)
    return {
        'installed':      installed or None,
        'latest':         latest or None,
        'update_available': needs_update,
    }


def update_nuclei():
    """Download and install the latest nuclei release. Returns {'ok': bool, 'version': str, 'error': str}."""
    ok = _install_nuclei()
    if ok:
        ver = _nuclei_installed_version()
        return {'ok': True, 'version': ver, 'error': ''}
    return {'ok': False, 'version': '', 'error': 'Installation fehlgeschlagen — Proxmox-Host-Methode verwenden'}


def _ensure_nuclei_templates():
    """Update nuclei templates (idempotent)."""
    try:
        _subprocess.run(
            [NUCLEI_BIN, '-update-templates', '-silent'],
            capture_output=True, timeout=120,
        )
    except Exception:
        pass


def run_nuclei_scan(target_url, templates=None):
    """
    Run a passive nuclei scan against target_url.
    Returns {'ok': bool, 'findings': [...], 'installed': bool, 'error': str}
    """
    installed = os.path.exists(NUCLEI_BIN)
    if not installed:
        return {'ok': False, 'findings': [], 'installed': False,
                'error': (
                    'nuclei ist nicht installiert. '
                    'Bitte den "Jetzt updaten" Button verwenden um nuclei zu installieren.'
                )}

    _ensure_nuclei_templates()

    tpl_args = []
    for t in (templates or NUCLEI_SAFE_TEMPLATES):
        tpl_path = os.path.join(NUCLEI_TEMPLATES, t) if not t.startswith('/') else t
        # nuclei also accepts relative template names without full path
        tpl_args += ['-t', t]

    cmd = [NUCLEI_BIN, '-u', target_url, '-jsonl', '-silent', '-no-color',
           '-rate-limit', '50', '-timeout', '5',
           '-c', '25', '-bulk-size', '25'] + tpl_args

    try:
        result = _subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        findings = []
        severity_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3, 'info': 4, 'unknown': 5}
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = _json.loads(line)
                info = d.get('info', {})
                findings.append({
                    'template':    d.get('template-id', ''),
                    'name':        info.get('name', ''),
                    'severity':    info.get('severity', 'info').lower(),
                    'matched':     d.get('matched-at', d.get('host', '')),
                    'description': (info.get('description') or '')[:200],
                    'tags':        ', '.join(info.get('tags', [])),
                })
            except _json.JSONDecodeError:
                pass
        findings.sort(key=lambda x: severity_order.get(x['severity'], 5))
        return {'ok': True, 'findings': findings, 'installed': True, 'error': ''}
    except _subprocess.TimeoutExpired:
        return {'ok': False, 'findings': [], 'installed': True, 'error': 'Timeout (300s) — zu viele Templates oder langsame Verbindung'}
    except Exception as e:
        return {'ok': False, 'findings': [], 'installed': True, 'error': str(e)}


# ── OWASP ZAP Integration ─────────────────────────────────────────────────────

import time as _time
import signal as _signal

ZAP_DIR     = '/opt/zaproxy'
ZAP_SH      = '/opt/zaproxy/zap.sh'
ZAP_PORT    = 8090
ZAP_API_KEY = 'djmanager-zap-key'
ZAP_PID_FILE = os.path.join(tempfile.gettempdir(), 'djmanager-zap.pid')


def _install_zap():
    """Download and install ZAP standalone (no Docker required)."""
    import urllib.request as _ureq
    import tarfile as _tar

    # Get latest ZAP release version from GitHub API
    try:
        with _safe_urlopen(
            'https://api.github.com/repos/zaproxy/zaproxy/releases/latest',
            timeout=15
        ) as resp:
            import json as _j
            tag = _j.loads(resp.read())['tag_name']  # e.g. "v2.15.0"
    except Exception:
        tag = 'v2.15.0'  # fallback

    version = tag.lstrip('v')
    tar_name = f'ZAP_{version}_Linux.tar.gz'
    url = f'https://github.com/zaproxy/zaproxy/releases/download/{tag}/{tar_name}'

    try:
        with _tempfile.TemporaryDirectory() as tmp:
            tar_path = os.path.join(tmp, tar_name)
            if _shutil.which('curl'):
                r = _subprocess.run([_CURL, '-fsSL', '-o', tar_path, url],
                                    timeout=300, capture_output=True)
                if r.returncode != 0:
                    return False, f'curl download failed: {r.stderr.decode()[:200]}'
            elif _shutil.which('wget'):
                r = _subprocess.run([_WGET, '-q', '-O', tar_path, url],
                                    timeout=300, capture_output=True)
                if r.returncode != 0:
                    return False, 'wget download failed'
            else:
                return False, 'curl oder wget nicht gefunden'

            with _tar.open(tar_path, 'r:gz') as tf:
                # Validate members: reject absolute paths and path traversal
                safe = [m for m in tf.getmembers()
                        if not os.path.isabs(m.name) and '..' not in m.name.split('/')]
                tf.extractall(tmp, members=safe)  # nosec B112 — members filtered above

            # Find extracted directory
            extracted = [d for d in os.listdir(tmp)
                         if os.path.isdir(os.path.join(tmp, d)) and d.startswith('ZAP')]
            if not extracted:
                return False, 'ZAP-Verzeichnis nicht gefunden nach Extraktion'

            src = os.path.join(tmp, extracted[0])
            if os.path.exists(ZAP_DIR):
                _shutil.rmtree(ZAP_DIR)
            _shutil.copytree(src, ZAP_DIR)
            os.chmod(ZAP_SH, 0o755)  # nosec B103
            # Store installed version for quick lookup
            with open(os.path.join(ZAP_DIR, '.version'), 'w') as _vf:
                _vf.write(version)
        return True, f'ZAP {version} installiert'
    except Exception as e:
        return False, str(e)


def _zap_installed_version():
    """Return installed ZAP version string or '' if not installed."""
    ver_file = os.path.join(ZAP_DIR, '.version')
    if os.path.exists(ver_file):
        try:
            return open(ver_file).read().strip()
        except Exception:
            pass
    if not os.path.exists(ZAP_DIR):
        return ''
    # Fallback: parse jar filename e.g. zap-2.15.0.jar
    try:
        for f in os.listdir(ZAP_DIR):
            if f.startswith('zap-') and f.endswith('.jar'):
                return f[4:-4]  # strip "zap-" prefix and ".jar" suffix
    except Exception:
        pass
    return ''


def _zap_latest_version():
    """Query GitHub API for latest ZAP release tag. Returns version string or ''."""
    try:
        import urllib.request as _ureq
        with _safe_urlopen(
            'https://api.github.com/repos/zaproxy/zaproxy/releases/latest',
            timeout=10
        ) as resp:
            tag = _json.loads(resp.read()).get('tag_name', '')
            return tag.lstrip('v')
    except Exception:
        return ''


def zap_version_info():
    """Return dict with installed, latest, update_available."""
    installed = _zap_installed_version()
    latest    = _zap_latest_version()
    needs_update = bool(installed and latest and installed != latest)
    return {
        'installed':        installed or None,
        'latest':           latest or None,
        'update_available': needs_update,
        'is_installed':     os.path.exists(ZAP_SH),
    }


def update_zap():
    """Download and install the latest ZAP release. Returns {'ok': bool, 'version': str, 'error': str}."""
    ok, msg = _install_zap()
    if ok:
        ver = _zap_installed_version()
        return {'ok': True, 'version': ver, 'error': ''}
    return {'ok': False, 'version': '', 'error': msg}


def _ensure_java():
    """Check if Java 11+ is available, install if not."""
    if _shutil.which('java'):
        return True
    r = _subprocess.run([_APT_GET, 'install', '-y', '--no-install-recommends', 'default-jre-headless'],
                        capture_output=True, timeout=120)
    return r.returncode == 0


def _zap_start():
    """Start ZAP daemon. Returns (ok, error_msg)."""
    if not _ensure_java():
        return False, 'Java konnte nicht installiert werden'

    if not os.path.exists(ZAP_SH):
        ok, msg = _install_zap()
        if not ok:
            return False, f'ZAP Installation fehlgeschlagen: {msg}'

    env = os.environ.copy()
    env['ZAP_PORT'] = str(ZAP_PORT)

    proc = _subprocess.Popen(
        [ZAP_SH, '-daemon', '-host', '127.0.0.1', '-port', str(ZAP_PORT),
         '-config', f'api.key={ZAP_API_KEY}',
         '-config', 'api.addrs.addr.name=127.0.0.1',
         '-config', 'api.addrs.addr.regex=false',
         '-config', 'connection.timeoutInSecs=30'],
        stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL,
        env=env, start_new_session=True,
    )
    with open(ZAP_PID_FILE, 'w') as f:
        f.write(str(proc.pid))

    # Wait for ZAP API to respond (up to 60s)
    for _ in range(60):
        _time.sleep(1)
        try:
            import urllib.request as _ureq
            _safe_urlopen(
                f'http://127.0.0.1:{ZAP_PORT}/JSON/core/view/version/?apikey={ZAP_API_KEY}',
                timeout=2
            )
            return True, ''
        except Exception:
            pass
    return False, 'ZAP hat nicht geantwortet nach 60 Sekunden'


def _zap_stop():
    """Stop ZAP daemon."""
    try:
        import urllib.request as _ureq
        _safe_urlopen(
            f'http://127.0.0.1:{ZAP_PORT}/JSON/core/action/shutdown/?apikey={ZAP_API_KEY}',
            timeout=5
        )
    except Exception:
        pass
    if os.path.exists(ZAP_PID_FILE):
        try:
            pid = int(open(ZAP_PID_FILE).read().strip())
            os.kill(pid, _signal.SIGTERM)
        except Exception:
            pass
        try:
            os.remove(ZAP_PID_FILE)
        except Exception:
            pass


def _zap_api(path, params=None):
    """Call ZAP JSON API. Returns parsed JSON or raises."""
    import urllib.parse as _up
    import urllib.request as _ureq
    qs = _up.urlencode({**(params or {}), 'apikey': ZAP_API_KEY})
    url = f'http://127.0.0.1:{ZAP_PORT}/JSON/{path}/?{qs}'
    with _safe_urlopen(url, timeout=30) as r:
        return _json.loads(r.read())


def _zap_setup_auth(ctx_id, target_url, auth):
    """Configure form-based authentication in ZAP for the given context."""
    import urllib.parse as _up
    login_url = auth.get('login_url', '')
    user_field = auth.get('username_field', 'username')
    pass_field = auth.get('password_field', 'password')
    username   = auth.get('username', '')
    password   = auth.get('password', '')
    logged_in_indicator = auth.get('logged_in_indicator', '')

    # Include target in context
    _zap_api('context/action/includeInContext', {
        'contextId': ctx_id,
        'regex': target_url.rstrip('/') + '.*',
    })

    # Form-based auth config (ZAP URL-encodes the field names)
    login_data = (
        f'loginUrl={_up.quote(login_url, safe="")}'
        f'&loginRequestData={_up.quote(user_field + "={%25username%25}&" + pass_field + "={%25password%25}", safe="")}'
    )
    _zap_api('authentication/action/setAuthenticationMethod', {
        'contextId': ctx_id,
        'authMethodName': 'formBasedAuthentication',
        'authMethodConfigParams': login_data,
    })

    if logged_in_indicator:
        _zap_api('authentication/action/setLoggedInIndicator', {
            'contextId': ctx_id,
            'loggedInIndicatorRegex': logged_in_indicator,
        })

    # Create user
    user = _zap_api('users/action/newUser', {'contextId': ctx_id, 'name': 'djmanager-user'})
    user_id = str(user.get('userId', '0'))

    _zap_api('users/action/setAuthenticationCredentials', {
        'contextId': ctx_id,
        'userId': user_id,
        'authCredentialsConfigParams': f'username={_up.quote(username)}&password={_up.quote(password)}',
    })
    _zap_api('users/action/setUserEnabled', {'contextId': ctx_id, 'userId': user_id, 'enabled': 'true'})

    # Forced-user mode so spider/scan use the credentials automatically
    _zap_api('forcedUser/action/setForcedUser', {'contextId': ctx_id, 'userId': user_id})
    _zap_api('forcedUser/action/setForcedUserModeEnabled', {'boolean': 'true'})

    return ctx_id, user_id


def run_zap_scan(target_url, scan_type='baseline', auth=None):
    """
    Run OWASP ZAP scan with auto-spider against target_url.
    scan_type: 'baseline' (passive only) | 'full' (active, only for test systems)
    auth: optional dict with keys login_url, username_field, password_field,
          username, password, logged_in_indicator — enables authenticated spider.
    Returns {'ok': bool, 'alerts': [...], 'scan_type': str, 'authenticated': bool, 'error': str}
    """
    started = False
    authenticated = bool(auth and auth.get('username') and auth.get('login_url'))
    try:
        # Check if ZAP already running
        try:
            _zap_api('core/view/version')
            already_running = True
        except Exception:
            already_running = False

        if not already_running:
            ok, err = _zap_start()
            if not ok:
                return {'ok': False, 'alerts': [], 'scan_type': scan_type,
                        'authenticated': authenticated, 'error': err}
            started = True

        ctx_id = None
        user_id = None
        if authenticated:
            ctx = _zap_api('context/action/newContext', {'contextName': 'djmanager'})
            ctx_id = str(ctx.get('contextId', '1'))
            ctx_id, user_id = _zap_setup_auth(ctx_id, target_url, auth)

        # Open target URL to seed the session
        _zap_api('core/action/accessUrl', {'url': target_url, 'followRedirects': 'true'})

        # Spider (auto-crawl), optionally with auth context
        spider_params = {'url': target_url, 'recurse': 'true'}
        if ctx_id:
            spider_params['contextName'] = 'djmanager'
            spider_params['userId'] = user_id
        spider = _zap_api('spider/action/scan', spider_params)
        spider_id = spider.get('scan', '0')

        # Wait for spider (max 90s)
        for _ in range(90):
            _time.sleep(1)
            prog = _zap_api('spider/view/status', {'scanId': spider_id})
            if int(prog.get('status', 0)) >= 100:
                break

        # Passive scan queue (max 90s)
        for _ in range(90):
            _time.sleep(1)
            recs = _zap_api('pscan/view/recordsToScan')
            if int(recs.get('recordsToScan', 1)) == 0:
                break

        # Active scan (only for 'full' — use carefully!)
        if scan_type == 'full':
            ascan_params = {'url': target_url, 'recurse': 'true'}
            if ctx_id:
                ascan_params['contextId'] = ctx_id
                ascan_params['userId'] = user_id
            active = _zap_api('ascan/action/scan', ascan_params)
            ascan_id = active.get('scan', '0')
            for _ in range(300):  # max 5 min
                _time.sleep(1)
                prog = _zap_api('ascan/view/status', {'scanId': ascan_id})
                if int(prog.get('status', 0)) >= 100:
                    break

        # Get alerts
        raw = _zap_api('core/view/alerts', {'baseurl': target_url})
        alerts_raw = raw.get('alerts', [])

        risk_order = {'High': 0, 'Medium': 1, 'Low': 2, 'Informational': 3}
        alerts = []
        seen = set()
        for a in alerts_raw:
            key = (a.get('alertRef', ''), a.get('url', ''))
            if key in seen:
                continue
            seen.add(key)
            alerts.append({
                'risk':        a.get('risk', ''),
                'name':        a.get('alert', ''),
                'url':         a.get('url', ''),
                'description': (a.get('description') or '')[:300],
                'solution':    (a.get('solution') or '')[:300],
                'reference':   (a.get('reference') or '')[:200],
            })
        alerts.sort(key=lambda x: risk_order.get(x['risk'], 9))

        return {'ok': True, 'alerts': alerts, 'scan_type': scan_type,
                'authenticated': authenticated, 'error': ''}

    except Exception as e:
        return {'ok': False, 'alerts': [], 'scan_type': scan_type,
                'authenticated': authenticated, 'error': str(e)}
    finally:
        if started:
            _zap_stop()
