"""
HTTP/TLS Security Scanner and Port Scanner utilities for DjangoMultiDeploy.
"""
import os
import ssl
import socket
import ipaddress
import datetime
import logging
import concurrent.futures
import urllib.request
import urllib.error

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

    def check(name, severity, present_ok, absent_msg, value_check_fn=None, ok_msg=None):
        val = headers.get(name.lower())
        if val is None:
            findings.append({'header': name, 'severity': severity, 'status': 'missing',
                             'value': None, 'msg': absent_msg})
        else:
            if value_check_fn:
                warn = value_check_fn(val)
                if warn:
                    findings.append({'header': name, 'severity': 'warning', 'status': 'weak',
                                     'value': val, 'msg': warn})
                else:
                    findings.append({'header': name, 'severity': 'ok', 'status': 'ok',
                                     'value': val, 'msg': ok_msg or 'OK'})
            else:
                findings.append({'header': name, 'severity': 'ok', 'status': 'ok',
                                 'value': val, 'msg': ok_msg or 'OK'})

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
        warnings = []
        vl = v.lower()
        if 'unsafe-inline' in vl:
            warnings.append("'unsafe-inline' erlaubt XSS")
        if 'unsafe-eval' in vl:
            warnings.append("'unsafe-eval' erlaubt Code-Injection")
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
                })
            else:
                findings.append({
                    'header': 'Access-Control-Allow-Origin',
                    'severity': 'medium',
                    'status': 'weak',
                    'value': acao,
                    'msg': 'CORS-Wildcard (*) erlaubt jeder Website Zugriff auf API-Antworten. Prüfen ob gewollt.',
                })
        else:
            findings.append({
                'header': 'Access-Control-Allow-Origin',
                'severity': 'ok',
                'status': 'ok',
                'value': acao,
                'msg': f'CORS auf bestimmte Origin eingeschränkt: {acao}',
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
                          'msg': 'Secure-Flag fehlt — Cookie wird auch über HTTP gesendet.'})
        if not flags['httponly']:
            issues.append({'flag': 'HttpOnly', 'severity': 'medium',
                          'msg': 'HttpOnly-Flag fehlt — Cookie per JavaScript auslesbar (XSS-Risiko).'})
        if not flags['samesite']:
            issues.append({'flag': 'SameSite', 'severity': 'medium',
                          'msg': 'SameSite-Flag fehlt — CSRF-Risiko erhöht.'})
        elif flags['samesite'] == 'none' and not flags['secure']:
            issues.append({'flag': 'SameSite=None', 'severity': 'high',
                          'msg': 'SameSite=None ohne Secure-Flag ist ungültig.'})
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


def run_http_security_scan(target_url, hostname=None, check_tls=True):
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

    if hostname and is_https:
        result['http_redirect'] = _check_http_redirect(hostname, port=_http_port)

    status, hdrs, body, final_url, err = _http_get(
        target_url, timeout=12, verify_ssl=False, follow_redirects=True
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

    base_url = target_url.rstrip('/')
    from urllib.parse import urlparse
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
            with urllib.request.urlopen(req, timeout=5) as resp:
                ip = resp.read().decode().strip()
                if ip:
                    ipv4 = ip
                    break
        except Exception:
            continue
    for url in services_v6:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'DjManager/1.0'})
            with urllib.request.urlopen(req, timeout=5) as resp:
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
