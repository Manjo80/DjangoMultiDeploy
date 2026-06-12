"""
Central input validators for security-sensitive identifiers.

Project names, usernames and DB identifiers flow into filesystem paths and
shell/SQL commands that run as root.  Validating them at a single, well-known
boundary blocks path traversal and command/SQL injection regardless of which
call site forgot to sanitise.
"""
import re

# Project name → systemd service, Linux user, /srv/<name>, nginx site, etc.
# Must start with a letter; letters, digits, underscore and hyphen only.
# No dot (blocks ".."), no slash, no shell metacharacters, no whitespace.
_PROJECT_NAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')

# SQL/DB identifier (database name, role/user name).
_DB_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')

# POSIX-ish Linux account name.
_LINUX_USER_RE = re.compile(r'^[a-z_][a-z0-9_-]{0,31}$')

# DNS hostname (for Let's Encrypt / certbot -d). Labels of letters, digits and
# hyphens, dot-separated; no wildcards, no scheme, no path, no whitespace.
_HOSTNAME_RE = re.compile(
    r'^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)'
    r'(\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$'
)


def is_valid_project_name(name):
    """True if *name* is a safe project identifier."""
    return bool(name) and bool(_PROJECT_NAME_RE.match(name))


def is_valid_db_identifier(value):
    """True if *value* is a safe SQL identifier (db / user name)."""
    return bool(value) and bool(_DB_IDENTIFIER_RE.match(value))


def is_valid_linux_user(value):
    """True if *value* is a safe Linux account name."""
    return bool(value) and bool(_LINUX_USER_RE.match(value))


def is_valid_hostname(value):
    """True if *value* is a safe fully-qualified DNS hostname (no wildcards)."""
    return bool(value) and bool(_HOSTNAME_RE.match(value))
