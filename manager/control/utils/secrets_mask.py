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


# ── Round-trippable masking for the .env editor ────────────────────────────────
# The editor shows secret values as a sentinel and restores any untouched
# sentinel from the on-disk file on save, so secrets are never displayed in the
# browser nor accidentally overwritten with the placeholder.
MASK_SENTINEL = '********'

# Key-name only matcher (decides whether a KEY=value line is a secret).
_SECRET_KEYNAME_RE = re.compile(
    r'(PASSWORD|SECRET|TOKEN|API[_-]?KEY|AUTH|PRIVATE|CREDENTIAL|PASS|PWD|'
    r'ACCESS[_-]?KEY|SALT|SIGNING|SIGNATURE|DSN|SENTRY|WEBHOOK|CERT|KEY)',
    re.IGNORECASE,
)


def _is_secret_key(key):
    return bool(_SECRET_KEYNAME_RE.search(key))


def mask_env_for_edit(text):
    """Return .env *text* with secret values replaced by the sentinel."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in line:
            key, _, val = line.partition('=')
            if val.strip() and _is_secret_key(key.strip()):
                out.append(f'{key}={MASK_SENTINEL}')
                continue
        out.append(line)
    return '\n'.join(out)


def merge_env_secrets(submitted, original):
    """
    Rebuild the .env from *submitted* (the edited, masked text), restoring any
    secret value still equal to the sentinel from *original* (the on-disk file).
    A user who actually typed a new secret value replaces the sentinel, so their
    new value is kept.
    """
    orig = {}
    for line in original.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in line:
            k, _, v = line.partition('=')
            orig[k.strip()] = v
    out = []
    for line in submitted.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in line:
            k, _, v = line.partition('=')
            if v.strip() == MASK_SENTINEL and k.strip() in orig:
                out.append(f'{k}={orig[k.strip()]}')
                continue
        out.append(line)
    return '\n'.join(out)
