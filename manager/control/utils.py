"""
Utility functions for reading the DjangoMultiDeploy project registry
and interacting with systemd services.
"""
import os
import subprocess
import glob
import shlex
from pathlib import Path
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
    log_path = f'/var/log/nginx/{name}_{log_type}.log'
    if not os.path.exists(log_path):
        return f'Log file not found: {log_path}'
    try:
        result = subprocess.run(
            ['tail', '-n', str(lines), log_path],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout
    except Exception as e:
        return f'Error reading log: {e}'


def list_backups(project):
    """Return sorted list of backup file paths for a project."""
    backup_dir = f'/var/backups/{project}'
    if not os.path.isdir(backup_dir):
        return []
    files = sorted(
        glob.glob(os.path.join(backup_dir, '*.tar.gz')),
        reverse=True
    )
    result = []
    for f in files:
        stat = os.stat(f)
        result.append({
            'path': f,
            'name': os.path.basename(f),
            'size_mb': round(stat.st_size / 1024 / 1024, 2),
            'mtime': stat.st_mtime,
        })
    return result


def run_update(name):
    """Run the project update script. Returns (ok, output)."""
    script = f'/usr/local/bin/{name}_update.sh'
    if not os.path.exists(script):
        return False, f'Update script not found: {script}'
    try:
        result = subprocess.run(
            [script],
            capture_output=True, text=True, timeout=300
        )
        return result.returncode == 0, (result.stdout + result.stderr)
    except Exception as e:
        return False, str(e)


def run_backup(name):
    """Run the project backup script. Returns (ok, output)."""
    script = f'/usr/local/bin/{name}_backup.sh'
    if not os.path.exists(script):
        return False, f'Backup script not found: {script}'
    try:
        result = subprocess.run(
            [script],
            capture_output=True, text=True, timeout=300
        )
        return result.returncode == 0, (result.stdout + result.stderr)
    except Exception as e:
        return False, str(e)


def remove_project(name, opts):
    """
    Run the project removal script with the given options dict.
    opts keys: remove_appdir, remove_db, remove_user, remove_backups, remove_logs
    Returns (ok, output).
    """
    script = f'/usr/local/bin/{name}_remove.sh'
    if not os.path.exists(script):
        return False, f'Remove script not found: {script}'
    env = os.environ.copy()
    env['NONINTERACTIVE'] = 'true'
    for key, val in opts.items():
        env[key.upper()] = 'true' if val else 'false'
    try:
        result = subprocess.run(
            [script],
            capture_output=True, text=True, timeout=120, env=env
        )
        return result.returncode == 0, (result.stdout + result.stderr)
    except Exception as e:
        return False, str(e)


GLOBAL_DEPLOY_KEY = '/root/.ssh/djmanager_github_ed25519'


def get_global_deploy_key():
    """Return (pubkey_content, error). Creates the key if it doesn't exist."""
    pub_path = GLOBAL_DEPLOY_KEY + '.pub'
    if not os.path.exists(GLOBAL_DEPLOY_KEY):
        try:
            import socket
            comment = f'djmanager@{socket.getfqdn()}'
            os.makedirs('/root/.ssh', mode=0o700, exist_ok=True)
            subprocess.run(
                ['ssh-keygen', '-t', 'ed25519', '-C', comment,
                 '-f', GLOBAL_DEPLOY_KEY, '-N', ''],
                check=True, capture_output=True
            )
            os.chmod(GLOBAL_DEPLOY_KEY, 0o600)
            os.chmod(pub_path, 0o644)
        except Exception as e:
            return None, f'Key konnte nicht erstellt werden: {e}'
    if not os.path.exists(pub_path):
        return None, f'Public Key nicht gefunden: {pub_path}'
    try:
        with open(pub_path) as f:
            return f.read().strip(), None
    except OSError as e:
        return None, str(e)


def get_ssh_key(project):
    """Return the SSH private key content for a project's app user."""
    conf = get_project(project)
    if not conf:
        return None, 'Project not found'
    key_path = conf.get('SSH_KEY_PATH') or f"/home/{conf.get('APPUSER', '')}/.ssh/id_ed25519"
    if not os.path.exists(key_path):
        return None, f'Key not found: {key_path}'
    try:
        with open(key_path) as f:
            return f.read(), None
    except OSError as e:
        return None, str(e)


def start_install(params):
    """
    Launch Installv2.sh in NONINTERACTIVE mode as a background process.
    params: dict of env vars to pass.
    Returns (log_path, pid).
    """
    import uuid, time
    install_script = settings.INSTALL_SCRIPT
    log_dir = settings.INSTALL_LOG_DIR
    run_id = str(uuid.uuid4())[:8]
    project = params.get('PROJECTNAME', 'install')
    log_path = os.path.join(log_dir, f'{project}_{run_id}.log')

    env = os.environ.copy()
    env.update(params)
    env['NONINTERACTIVE'] = 'true'

    # SSH key pause mechanism: web UI shows key then confirms continue
    wait_file = f'/tmp/djmanager_installs/{project}_github_wait'
    confirm_file = f'/tmp/djmanager_installs/{project}_github_confirm'
    env['GITHUB_KEY_WAIT_FILE'] = wait_file
    env['GITHUB_KEY_CONFIRM_FILE'] = confirm_file
    os.makedirs('/tmp/djmanager_installs', exist_ok=True)

    with open(log_path, 'w') as log_f:
        proc = subprocess.Popen(
            ['bash', install_script],
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )

    return log_path, proc.pid
