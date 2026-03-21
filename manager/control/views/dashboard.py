"""Dashboard view and manager self-info helper."""
import logging
import subprocess
import traceback
from pathlib import Path

from django.shortcuts import render
from django.conf import settings
from django.contrib.auth.decorators import login_required

from ..models import UserProfile, FavoriteCommand
from ..utils import (
    get_all_projects, get_ufw_status, get_server_stats,
    get_service_status, get_last_backup,
)
from ._helpers import _get_role, _allowed_projects, _is_ip_address

logger = logging.getLogger('djmanager.views.dashboard')


def _get_manager_info():
    """Build a pseudo-project dict for the djmanager service itself."""
    service = getattr(settings, 'MANAGER_SERVICE_NAME', 'djmanager')
    mgr_dir = str(settings.BASE_DIR)
    env_path = Path(settings.BASE_DIR) / '.env'

    env = {}
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, _, v = line.partition('=')
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                        v = v[1:-1]
                    env[k.strip()] = v
    except OSError:
        pass

    git_branch = git_hash = github_url = ''
    try:
        git_hash   = subprocess.check_output(
            ['git', '-C', mgr_dir, 'rev-parse', '--short', 'HEAD'],
            text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        git_branch = subprocess.check_output(
            ['git', '-C', mgr_dir, 'rev-parse', '--abbrev-ref', 'HEAD'],
            text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        github_url = subprocess.check_output(
            ['git', '-C', mgr_dir, 'remote', 'get-url', 'origin'],
            text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
    except Exception:
        pass

    debug_val = env.get('DEBUG', 'False')
    mode = 'dev' if debug_val.lower() in ('true', '1', 'yes') else 'prod'

    return {
        'PROJECTNAME':     service,
        'APPDIR':          mgr_dir,
        'MODE':            mode,
        'DEBUG':           debug_val,
        'DBTYPE':          'sqlite',
        'GUNICORN_PORT':   env.get('MANAGER_PORT', '8888'),
        'GITHUB_REPO_URL': github_url,
        'git_branch':      git_branch,
        'git_hash':        git_hash,
        'last_backup':     get_last_backup(service),
        'status':          get_service_status(service),
        '_is_manager':     True,
    }


@login_required
def dashboard(request):
    try:
        all_projects = get_all_projects()
        allowed      = _allowed_projects(request.user)
        if allowed is not None:
            all_projects = [p for p in all_projects if p.get('PROJECTNAME') in allowed]
        for p in all_projects:
            p['last_backup'] = get_last_backup(p.get('PROJECTNAME', ''))
        ufw          = get_ufw_status()
        server_stats = get_server_stats()
        role         = _get_role(request.user)
        allowed_hosts = [h for h in settings.ALLOWED_HOSTS if h != '*']
        manager_info  = _get_manager_info() if role in (
            UserProfile.ROLE_ADMIN, UserProfile.ROLE_OPERATOR) else None

        manager_allowed_hosts = [
            h for h in allowed_hosts
            if h and h != 'localhost' and not h.startswith('127.')
            and '.' in h and not _is_ip_address(h)
        ]

        project_names = [p.get('PROJECTNAME') for p in all_projects if p.get('PROJECTNAME')]
        fav_qs = FavoriteCommand.objects.filter(project_name__in=project_names)
        fav_by_project = {}
        for fav in fav_qs:
            fav_by_project.setdefault(fav.project_name, []).append(fav)
        for p in all_projects:
            p['favorite_commands'] = fav_by_project.get(p.get('PROJECTNAME', ''), [])

        return render(request, 'control/dashboard.html', {
            'projects':              all_projects,
            'ufw':                   ufw,
            'server_stats':          server_stats,
            'role':                  role,
            'allowed_hosts':         allowed_hosts,
            'manager_info':          manager_info,
            'manager_allowed_hosts': manager_allowed_hosts,
        })
    except Exception:
        logger.error('dashboard() crashed:\n%s', traceback.format_exc())
        raise
