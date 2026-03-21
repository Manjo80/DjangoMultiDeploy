"""
Registry and service utility functions for DjangoMultiDeploy.
"""
import os
import glob
import subprocess
from django.conf import settings


def _parse_conf(path):
    """Parse a shell-style key=value config file into a dict."""
    result = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    k, _, v = line.partition('=')
                    result[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return result


def get_all_projects():
    """Return list of project dicts from /etc/django-servers.d/*.conf"""
    registry_dir = settings.REGISTRY_DIR
    projects = []
    for conf_path in sorted(glob.glob(os.path.join(registry_dir, '*.conf'))):
        data = _parse_conf(conf_path)
        if data.get('PROJECTNAME'):
            data['_conf_path'] = conf_path
            data['status'] = get_service_status(data['PROJECTNAME'])
            projects.append(data)
    return projects


def get_project(name):
    """Return a single project dict or None."""
    conf_path = os.path.join(settings.REGISTRY_DIR, f'{name}.conf')
    if not os.path.exists(conf_path):
        return None
    data = _parse_conf(conf_path)
    data['_conf_path'] = conf_path
    data['status'] = get_service_status(name)
    return data


def get_service_status(name):
    """Return 'active', 'inactive', 'failed', or 'unknown'."""
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', name],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() or 'unknown'
    except Exception:
        return 'unknown'


def service_action(name, action):
    """Run systemctl action (start/stop/restart) on service. Returns (ok, output)."""
    if action not in ('start', 'stop', 'restart', 'reload'):
        return False, 'Invalid action'
    try:
        result = subprocess.run(
            ['systemctl', action, name],
            capture_output=True, text=True, timeout=30
        )
        ok = result.returncode == 0
        return ok, (result.stdout + result.stderr).strip()
    except Exception as e:
        return False, str(e)


def get_journal_logs(name, lines=100):
    """Return last N lines of journalctl output for a service."""
    try:
        result = subprocess.run(
            ['journalctl', '-u', name, '-n', str(lines), '--no-pager', '--output=short-iso'],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout
    except Exception as e:
        return f'Error reading logs: {e}'


def get_nginx_log(name, log_type='access', lines=100):
    """Return last N lines of nginx access or error log for a project."""
    # Install script writes: /var/log/nginx/{name}.access.log (dot separator)
    # Fallback: underscore separator (older installs) and default nginx logs
    candidates = [
        f'/var/log/nginx/{name}.{log_type}.log',      # current format
        f'/var/log/nginx/{name}_{log_type}.log',       # legacy format
        f'/var/log/nginx/{log_type}.log',              # default nginx log
    ]
    for log_path in candidates:
        if os.path.exists(log_path):
            try:
                result = subprocess.run(
                    ['tail', '-n', str(lines), log_path],
                    capture_output=True, text=True, timeout=10
                )
                return result.stdout or f'(Datei leer: {log_path})'
            except Exception as e:
                return f'Fehler beim Lesen von {log_path}: {e}'
    return (
        f'Log-Datei nicht gefunden. Gesucht:\n'
        + '\n'.join(f'  {p}' for p in candidates)
    )


def set_project_conf_value(project, key, value):
    """Write or update a single KEY=value line in the project's .conf file."""
    from django.conf import settings as djsettings
    conf_path = os.path.join(
        getattr(djsettings, 'REGISTRY_DIR', '/etc/django-servers.d'),
        f'{project}.conf',
    )
    if not os.path.exists(conf_path):
        return False, f'Conf nicht gefunden: {conf_path}'
    try:
        with open(conf_path) as f:
            lines = f.readlines()
        new_lines = []
        found = False
        for line in lines:
            if line.startswith(f'{key}=') or line.startswith(f'{key} ='):
                new_lines.append(f'{key}="{value}"\n')
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f'{key}="{value}"\n')
        with open(conf_path, 'w') as f:
            f.writelines(new_lines)
        return True, None
    except OSError as e:
        return False, str(e)
