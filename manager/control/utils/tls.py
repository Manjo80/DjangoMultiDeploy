"""
Let's Encrypt / certbot integration (optional).

This is a convenience for single-server setups that terminate TLS on the local
nginx. It is never required: deployments that sit behind an external reverse
proxy with its own certificates simply never call it.

All hostnames are validated with is_valid_hostname() before reaching certbot,
and certbot is always invoked with an argument list (never a shell string), so
a crafted domain cannot inject extra flags or shell commands.
"""
import datetime
import os
import shutil
import subprocess

from .validators import is_valid_hostname

CERTBOT_BIN = shutil.which('certbot') or '/usr/bin/certbot'
LIVE_DIR = '/etc/letsencrypt/live'


def certbot_available():
    return os.path.exists(CERTBOT_BIN)


def cert_status(domain):
    """
    Return certificate status for *domain* without invoking certbot.

    Reads the cert file under /etc/letsencrypt/live/<domain>/ and reports the
    expiry. Returns {'present': bool, 'expires': str|None, 'days_left': int|None}.
    """
    info = {'present': False, 'expires': None, 'days_left': None}
    if not is_valid_hostname(domain):
        return info
    cert_path = os.path.join(LIVE_DIR, domain, 'cert.pem')
    if not os.path.isfile(cert_path):
        return info
    info['present'] = True
    try:
        # Parse notAfter via the stdlib so we don't shell out for a read.
        import ssl
        not_after = ssl._ssl._test_decode_cert(cert_path)['notAfter']
        expires = datetime.datetime.strptime(not_after, '%b %d %H:%M:%S %Y %Z')
        info['expires'] = expires.strftime('%d.%m.%Y')
        info['days_left'] = (expires - datetime.datetime.utcnow()).days
    except Exception:
        pass
    return info


def obtain_certificate(domain, email, staging=False):
    """
    Request/renew a certificate for *domain* via the certbot nginx plugin.

    Returns {'ok': bool, 'output': str}. Safe to call repeatedly — certbot
    reuses an existing certificate and reconfigures nginx idempotently.
    """
    if not is_valid_hostname(domain):
        return {'ok': False, 'output': f'Ungültiger Hostname: {domain!r}'}
    if not certbot_available():
        return {'ok': False, 'output':
                'certbot ist nicht installiert. Bitte "python3-certbot-nginx" '
                'installieren oder die Installation mit INSTALL_CERTBOT=j ausführen.'}

    cmd = [
        CERTBOT_BIN, '--nginx',
        '-d', domain,
        '--non-interactive', '--agree-tos',
        '--redirect',
    ]
    if email:
        cmd += ['-m', email]
    else:
        cmd += ['--register-unsafely-without-email']
    if staging:
        cmd += ['--staging']

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return {'ok': r.returncode == 0, 'output': (r.stdout + r.stderr).strip()}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'output': 'certbot Timeout (180s überschritten).'}
    except Exception as exc:
        return {'ok': False, 'output': str(exc)}
