"""
Mask secrets before exposing .env / config contents (config export, scan
reports). Covers three shapes the old key-name denylist missed:
  1. KEY=value lines whose key name looks secret,
  2. credentials embedded in URLs / DSNs (scheme://user:pass@host),
  3. PEM private-key blocks.
"""
import re

_MASK = 'xxxx'

# Broad secret key-name matcher for KEY=value lines.
_SECRET_KEY_RE = re.compile(
    r'^(\s*[A-Z0-9_]*'
    r'(?:PASSWORD|SECRET|TOKEN|API[_-]?KEY|AUTH|PRIVATE|CREDENTIAL|'
    r'PASS|PWD|ACCESS[_-]?KEY|SALT|SIGNING|SIGNATURE|DSN|SENTRY|'
    r'WEBHOOK|CERT|KEY)'
    r'[A-Z0-9_]*\s*=\s*)(.+)$',
    re.IGNORECASE | re.MULTILINE,
)

# Credentials inside a URL/DSN: scheme://user:password@host
_URL_CRED_RE = re.compile(r'(://[^:/@\s]+:)([^@/\s]+)(@)')

# PEM private-key blocks.
_PEM_RE = re.compile(
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----',
    re.DOTALL,
)


def mask_secrets(text):
    """Return *text* with secret values masked."""
    if not text:
        return text
    text = _PEM_RE.sub('-----BEGIN PRIVATE KEY-----\n' + _MASK + '\n-----END PRIVATE KEY-----', text)
    text = _URL_CRED_RE.sub(r'\1' + _MASK + r'\3', text)
    text = _SECRET_KEY_RE.sub(r'\1' + _MASK, text)
    return text
