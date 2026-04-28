"""
Configuration utility functions for DjangoMultiDeploy.
"""
import os
import subprocess
from django.conf import settings

from .registry import get_project, set_project_conf_value, service_action


def get_allowed_hosts(name):
    """Read ALLOWED_HOSTS list from the project's .env file."""
    conf = get_project(name)
    if not conf:
        return []
    env_path = os.path.join(conf.get('APPDIR', f'/srv/{name}'), '.env')
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('ALLOWED_HOSTS='):
                    val = line[len('ALLOWED_HOSTS='):].strip().strip('"').strip("'")
                    return [h.strip() for h in val.split(',') if h.strip()]
    except OSError:
        pass
    return []


def get_nginx_server_names(name):
    """Read server_name from nginx site config. Returns list of names."""
    nginx_path = f'/etc/nginx/sites-available/{name}'
    if not os.path.exists(nginx_path):
        return []
    try:
        with open(nginx_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('server_name '):
                    val = line[len('server_name '):].rstrip(';').strip()
                    return [n for n in val.split() if n and n != '_']
    except OSError:
        pass
    return []


def update_allowed_hosts(name, hosts):
    """
    Update ALLOWED_HOSTS in .env and server_name in nginx config.
    Restarts the Django service and reloads nginx.
    Returns (ok, message).
    """
    conf = get_project(name)
    if not conf:
        return False, 'Projekt nicht gefunden'
    appdir = conf.get('APPDIR', f'/srv/{name}')
    env_path = os.path.join(appdir, '.env')

    # Sanitize host list — lowercase to avoid CSRF case-sensitive mismatches
    hosts = [h.strip().lower() for h in hosts if h.strip()]
    if not hosts:
        return False, 'Mindestens ein Host erforderlich'

    # --- Update .env ---
    try:
        with open(env_path) as f:
            lines = f.readlines()
        new_lines = []
        found_allowed = False
        found_csrf = False
        csrf_value = ','.join(f'https://{h}' for h in hosts)
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('ALLOWED_HOSTS='):
                new_lines.append(f'ALLOWED_HOSTS={",".join(hosts)}\n')
                found_allowed = True
            elif stripped.startswith('CSRF_TRUSTED_ORIGINS='):
                new_lines.append(f'CSRF_TRUSTED_ORIGINS={csrf_value}\n')
                found_csrf = True
            else:
                new_lines.append(line)
        if not found_allowed:
            new_lines.append(f'ALLOWED_HOSTS={",".join(hosts)}\n')
        if not found_csrf:
            new_lines.append(f'CSRF_TRUSTED_ORIGINS={csrf_value}\n')
        with open(env_path, 'w') as f:
            f.writelines(new_lines)
    except OSError as e:
        return False, f'.env konnte nicht aktualisiert werden: {e}'

    # --- Update nginx server_name ---
    nginx_path = f'/etc/nginx/sites-available/{name}'
    if os.path.exists(nginx_path):
        try:
            with open(nginx_path) as f:
                content = f.read()
            import re
            new_names = ' '.join(hosts)
            content = re.sub(
                r'server_name\s+[^;]+;',
                f'server_name {new_names};',
                content
            )
            with open(nginx_path, 'w') as f:
                f.write(content)
        except OSError as e:
            return False, f'nginx-Konfiguration konnte nicht aktualisiert werden: {e}'

    # --- Update registry conf ---
    conf_path = os.path.join('/etc/django-servers.d', f'{name}.conf')
    if os.path.exists(conf_path):
        try:
            with open(conf_path) as f:
                lines = f.readlines()
            new_lines = []
            for line in lines:
                if line.strip().startswith('PRIMARY_HOST='):
                    new_lines.append(f'PRIMARY_HOST="{hosts[0]}"\n')
                else:
                    new_lines.append(line)
            with open(conf_path, 'w') as f:
                f.writelines(new_lines)
        except OSError:
            pass

    # --- Restart service + reload nginx ---
    msgs = []
    ok1, out1 = service_action(name, 'restart')
    msgs.append(f'Service: {"OK" if ok1 else out1}')

    try:
        subprocess.run(['nginx', '-t'], check=True, capture_output=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True, timeout=10)
        msgs.append('nginx: neu geladen')
    except Exception as e:
        msgs.append(f'nginx reload: {e}')

    return True, ' | '.join(msgs)


def sync_env_to_conf(name, env_content):
    """
    Parse MODE and DEBUG from .env content and write them back into
    /etc/django-servers.d/<name>.conf so the manager display stays in sync.
    """
    conf_path = os.path.join(settings.REGISTRY_DIR, f'{name}.conf')
    if not os.path.exists(conf_path):
        return
    # Extract MODE and DEBUG from .env content
    env_vals = {}
    for line in env_content.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, _, v = line.partition('=')
            env_vals[k.strip()] = v.strip().strip('"\'')
    update_keys = {k: env_vals[k] for k in ('MODE', 'DEBUG') if k in env_vals}
    if not update_keys:
        return
    try:
        with open(conf_path) as f:
            lines = f.readlines()
        new_lines = []
        for line in lines:
            key = line.split('=', 1)[0].strip()
            if key in update_keys:
                new_lines.append(f'{key}="{update_keys.pop(key)}"\n')
            else:
                new_lines.append(line)
        with open(conf_path, 'w') as f:
            f.writelines(new_lines)
    except OSError:
        pass
