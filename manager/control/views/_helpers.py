"""
Shared decorators and access-check helpers used across the views package.
"""
import ipaddress
import logging
from functools import wraps

from django.shortcuts import render, redirect

from ..models import UserProfile, ProjectPermission

logger = logging.getLogger('djmanager.views')


# ── Role helpers ──────────────────────────────────────────────────────────────

def _get_role(user):
    """Return the role string for a user. Superusers are always 'admin'."""
    if user.is_superuser:
        return UserProfile.ROLE_ADMIN
    try:
        return user.userprofile.role
    except Exception:
        return UserProfile.ROLE_VIEWER


def role_required(*roles):
    """Decorator: require user to have one of the given roles."""
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect(f'/login/?next={request.path}')
            if _get_role(request.user) not in roles:
                return render(request, 'control/403.html', status=403)
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


# Convenience aliases
admin_required    = role_required(UserProfile.ROLE_ADMIN)
operator_required = role_required(UserProfile.ROLE_ADMIN, UserProfile.ROLE_OPERATOR)


# ── Project access helpers ────────────────────────────────────────────────────

def _allowed_projects(user):
    """
    Returns a set of project names the user may access, or None if unrestricted.
    Admin / superuser → None (all projects).
    Operator / Viewer → set of assigned project names (may be empty).
    """
    if user.is_superuser or _get_role(user) == UserProfile.ROLE_ADMIN:
        return None
    return set(
        ProjectPermission.objects.filter(user=user).values_list('project_name', flat=True)
    )


def _check_project_access(user, name):
    """Returns True when the user may access this project."""
    allowed = _allowed_projects(user)
    return allowed is None or name in allowed


# ── HTTP scan host helpers ────────────────────────────────────────────────────

_INTERNAL_HOSTS = {'127.0.0.1', 'localhost', '::1', '0.0.0.0'}


def _is_ip_address(h):
    """Return True if h is a valid IPv4 or IPv6 address (not a hostname)."""
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def _build_extern_scan_hosts(nginx_names, allowed_hosts):
    """Return deduplicated list of external HTTP scan targets (hostnames only)."""
    seen = set()
    result = []
    for h in nginx_names + allowed_hosts:
        h = h.strip().lower()  # DNS hostnames are case-insensitive; normalise to lowercase
        if (h and h not in _INTERNAL_HOSTS
                and not h.startswith('127.')
                and not h.startswith('.')
                and not _is_ip_address(h)
                and h not in seen):
            seen.add(h)
            result.append(h)
    return result
