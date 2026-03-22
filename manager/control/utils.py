"""
Utility functions for reading the DjangoMultiDeploy project registry
and interacting with systemd services.
"""
import os
import json
import subprocess
import glob
import shlex
import ssl
import socket
import ipaddress
import urllib.request
import urllib.error
import datetime
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


def list_backups(project):
    """Return sorted list of backup file paths for a project."""
    backup_dir = f'/var/backups/{project}'
    if not os.path.isdir(backup_dir):
        return []
    files = sorted(
        glob.glob(os.path.join(backup_dir, '*.tar.gz')),
        reverse=True
    )
    import datetime
    result = []
    for f in files:
        stat = os.stat(f)
        result.append({
            'path': f,
            'name': os.path.basename(f),
            'size_mb': round(stat.st_size / 1024 / 1024, 2),
            'mtime': stat.st_mtime,
            'mtime_str': datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%d.%m.%Y %H:%M'),
        })
    return result


def run_management_command(name, raw_cmd):
    """
    Run a Django management command for the given project as the app user
    inside the project's .venv. Returns (ok, output).

    raw_cmd examples accepted:
      'load_glossary'
      'load_glossary --file=data.json'
      'python manage.py load_glossary'
      'manage.py migrate --run-syncdb'
    """
    import shlex, re as _re
    conf    = get_project(name)
    if not conf:
        return False, 'Projekt nicht gefunden'
    appdir  = conf.get('APPDIR', f'/srv/{name}')
    appuser = conf.get('APPUSER', '')
    if not appuser:
        return False, 'APPUSER nicht konfiguriert'

    venv_python = os.path.join(appdir, '.venv', 'bin', 'python')
    manage_py   = os.path.join(appdir, 'manage.py')
    if not os.path.exists(venv_python):
        return False, f'venv nicht gefunden: {venv_python}'
    if not os.path.exists(manage_py):
        return False, f'manage.py nicht gefunden: {manage_py}'

    # Normalize input → extract just the subcommand + args
    # Strip leading 'python manage.py', 'manage.py', './manage.py'
    cmd_clean = raw_cmd.strip()
    cmd_clean = _re.sub(r'^(python\s+)?(\./)?manage\.py\s*', '', cmd_clean).strip()
    if not cmd_clean:
        return False, 'Kein Kommando angegeben'

    # Security: block shell metacharacters and control characters
    if _re.search(r'[;&|`$<>()\r\n]', cmd_clean):
        return False, 'Ungültige Zeichen im Kommando (keine Shell-Sonderzeichen erlaubt)'

    full_cmd = (
        f'cd {shlex.quote(appdir)} && '
        f'{shlex.quote(venv_python)} manage.py {cmd_clean}'
    )
    try:
        result = subprocess.run(
            ['su', '-', appuser, '-s', '/bin/bash', '-c', full_cmd],
            capture_output=True, text=True, timeout=300,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output or '(keine Ausgabe)'
    except subprocess.TimeoutExpired:
        return False, 'Timeout nach 300 Sekunden'
    except Exception as e:
        return False, str(e)


def run_update(name):
    """Run the project update script. Returns (ok, output)."""
    import re as _re
    script = f'/usr/local/bin/{name}_update.sh'
    if not os.path.exists(script):
        return False, f'Update script not found: {script}'

    # Auto-patch: if the project has a registered deploy key, ensure the
    # update script uses it. This fixes projects installed before the
    # per-project deploy key system was in place (they had the global
    # djmanager key hardcoded) and keeps scripts in sync after key rotation.
    conf = get_project(name)
    if conf:
        key_id = conf.get('DEPLOY_KEY_ID', '').strip()
        if key_id:
            key_path = os.path.join(KEYS_DIR, f'{key_id}_ed25519')
            if os.path.exists(key_path):
                try:
                    with open(script) as f:
                        content = f.read()
                    new_content = _re.sub(
                        r'GITHUB_DEPLOY_KEY="[^"]*"',
                        f'GITHUB_DEPLOY_KEY="{key_path}"',
                        content,
                    )
                    if new_content != content:
                        with open(script, 'w') as f:
                            f.write(new_content)
                except OSError:
                    pass

    try:
        result = subprocess.run(
            [script],
            capture_output=True, text=True, timeout=300
        )
        output = result.stdout + result.stderr
        if result.returncode != 0 and (
            'Repository not found' in output
            or 'Could not read from remote repository' in output
        ):
            output += (
                f'\n\n💡 Hinweis: Der Deploy-Key für Projekt "{name}" hat keinen Zugriff auf das GitHub-Repository.\n'
                f'   → Manager → Deploy Keys → Key für "{name}" anlegen/zuweisen\n'
                f'   → GitHub → Repo → Settings → Deploy keys → Key eintragen (Read access)'
            )
        return result.returncode == 0, output
    except Exception as e:
        return False, str(e)


def extract_project_zip(zip_path, dest_dir, skip_tops=None):
    """
    Extract a ZIP to dest_dir.
    - Strips single top-level directory (GitHub-style: repo-main/)
    - Path-traversal protected
    - Skips entries whose top-level component is in skip_tops
    Returns (extracted_count, skipped_count)
    """
    import zipfile, shutil
    skip_tops = set(skip_tops or [])
    real_dest = os.path.realpath(dest_dir)

    with zipfile.ZipFile(zip_path, 'r') as zf:
        names = zf.namelist()
        # Security: reject any path with '..'
        for n in names:
            if '..' in n.replace('\\', '/').split('/'):
                raise ValueError(f'Unsicherer Pfad in ZIP: {n}')

        # Detect single top-level directory (GitHub zip: repo-main/...)
        tops_with_slash = {n.split('/')[0] for n in names if '/' in n}
        tops_all = {n.split('/')[0].rstrip('/') for n in names}
        prefix = ''
        if len(tops_with_slash) == 1 and tops_all == tops_with_slash:
            prefix = list(tops_with_slash)[0] + '/'

        extracted, skipped = 0, 0
        for member in zf.infolist():
            rel = member.filename[len(prefix):] if (prefix and member.filename.startswith(prefix)) else member.filename
            if not rel or rel.endswith('/'):
                continue  # skip directory entries
            top = rel.split('/')[0]
            if top in skip_tops:
                skipped += 1
                continue
            target = os.path.join(dest_dir, rel)
            if not os.path.realpath(os.path.dirname(target)).startswith(real_dest):
                skipped += 1
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(member) as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            extracted += 1
    return extracted, skipped


def update_project_from_zip(name, uploaded_file):
    """
    Update an existing project by extracting an uploaded ZIP over the project directory.
    Preserves: .env, .venv, media/, staticfiles/
    Then runs: pip install, migrate, collectstatic, restarts service.
    Returns (ok: bool, output: str)
    """
    import tempfile
    conf = get_project(name)
    if not conf:
        return False, 'Projekt nicht gefunden'
    appdir = conf.get('APPDIR', f'/srv/{name}')
    appuser = conf.get('APPUSER', name)
    output_lines = []

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.zip', prefix=f'dmd_{name}_') as tmp:
            for chunk in uploaded_file.chunks():
                tmp.write(chunk)
            zip_path = tmp.name
    except Exception as e:
        return False, f'Fehler beim Speichern der ZIP: {e}'

    try:
        extracted, skipped = extract_project_zip(
            zip_path, appdir, skip_tops={'.env', '.venv', 'media', 'staticfiles'}
        )
        output_lines.append(f'✅ {extracted} Dateien extrahiert, {skipped} geschützte Pfade übersprungen')

        subprocess.run(['chown', '-R', f'{appuser}:{appuser}', appdir],
                       check=True, capture_output=True)

        venv_activate = os.path.join(appdir, '.venv', 'bin', 'activate')
        req_file = os.path.join(appdir, 'requirements.txt')

        def _run_as(cmd, timeout=300):
            r = subprocess.run(
                ['su', '-', appuser, '-s', '/bin/bash', '-c', cmd],
                capture_output=True, text=True, timeout=timeout
            )
            return r.returncode, (r.stdout + r.stderr).strip()[-800:]

        if os.path.isfile(req_file):
            rc, out = _run_as(
                f'source {venv_activate} && pip install --no-cache-dir --prefer-binary -r {req_file}'
            )
            output_lines.append(f'📦 pip install {"✅" if rc == 0 else "❌"}\n{out}')

        rc, out = _run_as(
            f'cd {appdir} && source {venv_activate} && python manage.py migrate --noinput',
            timeout=120
        )
        output_lines.append(f'🔄 migrate {"✅" if rc == 0 else "❌"}\n{out}')

        rc, out = _run_as(
            f'cd {appdir} && source {venv_activate} && python manage.py collectstatic --noinput',
            timeout=60
        )
        output_lines.append(f'📁 collectstatic {"✅" if rc == 0 else "⚠️"}\n{out[-200:]}')

        r = subprocess.run(['systemctl', 'restart', name],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            output_lines.append('✅ Service neu gestartet')
        else:
            output_lines.append(f'⚠️ Service-Restart: {r.stderr.strip()}')

        return True, '\n'.join(output_lines)
    except Exception as e:
        return False, f'Fehler: {e}'
    finally:
        try:
            os.unlink(zip_path)
        except OSError:
            pass


def delete_backup(project, filename):
    """
    Delete a single backup file for a project.
    Returns (ok, message). Path-traversal protected.
    """
    # Reject any filename with path components
    if not filename or os.path.basename(filename) != filename or '/' in filename or '..' in filename:
        return False, 'Ungültiger Dateiname'
    if not filename.endswith('.tar.gz'):
        return False, 'Nur .tar.gz Dateien können gelöscht werden'
    backup_dir = f'/var/backups/{project}'
    full_path = os.path.join(backup_dir, filename)
    # Resolve and verify the path stays inside backup_dir
    try:
        real_path = os.path.realpath(full_path)
        real_dir = os.path.realpath(backup_dir)
        if not real_path.startswith(real_dir + os.sep):
            return False, 'Zugriff verweigert'
    except Exception as e:
        return False, str(e)
    if not os.path.isfile(real_path):
        return False, f'Datei nicht gefunden: {filename}'
    try:
        os.remove(real_path)
        return True, f'{filename} gelöscht'
    except OSError as e:
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
    Remove a project directly (no shell script) so NONINTERACTIVE handling
    is reliable. opts keys: remove_appdir, remove_db, remove_user,
    remove_backups, remove_logs.  Returns (ok, output).
    """
    import shutil
    conf    = get_project(name) or {}
    appdir  = conf.get('APPDIR', f'/srv/{name}')
    appuser = conf.get('APPUSER', '')
    dbtype  = conf.get('DBTYPE', '')
    dbname  = conf.get('DBNAME', '')
    nginx_port    = conf.get('NGINX_PORT', '')
    gunicorn_port = conf.get('GUNICORN_PORT', '')
    log = []
    ok  = True

    def _run(*cmd):
        try:
            subprocess.run(list(cmd), capture_output=True, timeout=30)
        except Exception:
            pass

    def _rm(path):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.remove(path)
        except Exception as e:
            log.append(f'⚠️  {path}: {e}')

    # ── Service ───────────────────────────────────────────────────────────────
    _run('systemctl', 'stop', name)
    _run('systemctl', 'disable', name)
    _rm(f'/etc/systemd/system/{name}.service')
    _run('systemctl', 'daemon-reload')
    log.append(f'✅ Service {name} gestoppt')

    # ── nginx ─────────────────────────────────────────────────────────────────
    _rm(f'/etc/nginx/sites-enabled/{name}')
    _rm(f'/etc/nginx/sites-available/{name}')
    _run('nginx', '-t')
    _run('systemctl', 'reload', 'nginx')
    log.append('✅ nginx-Config entfernt')

    # ── UFW ───────────────────────────────────────────────────────────────────
    if nginx_port:
        _run('ufw', 'delete', 'allow', f'{nginx_port}/tcp')
    if gunicorn_port:
        _run('ufw', 'delete', 'allow', f'{gunicorn_port}/tcp')

    # ── Config files ──────────────────────────────────────────────────────────
    for p in [
        f'/etc/sudoers.d/{name}-service',
        f'/etc/logrotate.d/{name}',
        f'/etc/django-servers.d/{name}.conf',
    ]:
        _rm(p)
    log.append('✅ Konfigurationsdateien entfernt')

    # ── Cron ──────────────────────────────────────────────────────────────────
    try:
        res = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        if res.returncode == 0:
            new_cron = '\n'.join(
                l for l in res.stdout.splitlines() if f'{name}_backup.sh' not in l
            )
            subprocess.run(['crontab', '-'], input=new_cron, text=True,
                           capture_output=True)
    except Exception:
        pass

    # ── Optional: app directory ───────────────────────────────────────────────
    if opts.get('remove_appdir') and appdir:
        _rm(appdir)
        log.append(f'✅ Projektverzeichnis {appdir} entfernt')

    # ── Optional: logs ────────────────────────────────────────────────────────
    if opts.get('remove_logs'):
        _rm(f'/var/log/{name}')
        log.append('✅ Logs entfernt')

    # ── Optional: backups ─────────────────────────────────────────────────────
    if opts.get('remove_backups'):
        _rm(f'/var/backups/{name}')
        log.append('✅ Backups entfernt')

    # ── Optional: database ────────────────────────────────────────────────────
    if opts.get('remove_db') and dbname:
        # Try to read DBUSER from the project .env
        dbuser = ''
        env_path = os.path.join(appdir, '.env')
        if os.path.exists(env_path):
            for line in open(env_path, errors='ignore'):
                if line.startswith('DB_USER=') or line.startswith('DATABASE_USER='):
                    dbuser = line.split('=', 1)[1].strip().strip('"\'')
                    break
        if dbtype == 'postgresql':
            _run('su', '-s', '/bin/bash', 'postgres', '-c',
                 f'psql -c "DROP DATABASE IF EXISTS \\"{dbname}\\";"')
            if dbuser:
                _run('su', '-s', '/bin/bash', 'postgres', '-c',
                     f'psql -c "DROP USER IF EXISTS \\"{dbuser}\\";"')
            log.append(f'✅ PostgreSQL DB {dbname} entfernt')
        elif dbtype == 'mysql':
            cmd = f'DROP DATABASE IF EXISTS `{dbname}`;'
            if dbuser:
                cmd += f" DROP USER IF EXISTS '{dbuser}'@'localhost';"
            _run('mysql', '-u', 'root', '-e', cmd)
            log.append(f'✅ MySQL DB {dbname} entfernt')

    # ── Optional: Linux user ──────────────────────────────────────────────────
    if opts.get('remove_user') and appuser:
        try:
            result = subprocess.run(
                ['deluser', '--remove-home', appuser],
                capture_output=True, timeout=30
            )
            if result.returncode != 0:
                subprocess.run(['userdel', '-r', appuser], capture_output=True)
            log.append(f'✅ Linux-User {appuser} entfernt')
        except Exception as e:
            log.append(f'⚠️  User entfernen: {e}')

    # ── Remove scripts ────────────────────────────────────────────────────────
    for script in [
        f'/usr/local/bin/{name}_update.sh',
        f'/usr/local/bin/{name}_backup.sh',
        f'/usr/local/bin/{name}_remove.sh',
    ]:
        _rm(script)

    log.append('✅ Fertig')
    return ok, '\n'.join(log)


GLOBAL_DEPLOY_KEY = '/root/.ssh/djmanager_github_ed25519'

# ── Deploy Key Registry ────────────────────────────────────────────────────────
KEYS_DIR      = '/root/.ssh/djmanager_keys'
KEYS_REGISTRY = '/root/.ssh/djmanager_keys/registry.json'


def _load_key_registry():
    """Return the key registry dict (id → metadata). Never raises."""
    import json
    try:
        with open(KEYS_REGISTRY) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_key_registry(registry):
    import json
    os.makedirs(KEYS_DIR, mode=0o700, exist_ok=True)
    with open(KEYS_REGISTRY, 'w') as f:
        json.dump(registry, f, indent=2)
    os.chmod(KEYS_REGISTRY, 0o600)


def create_deploy_key(label):
    """
    Create a new ed25519 deploy key pair, store in KEYS_DIR and registry.
    Returns (key_id, pub_key_content, error).
    """
    import uuid, json
    from datetime import datetime
    key_id  = uuid.uuid4().hex[:12]
    priv    = os.path.join(KEYS_DIR, f'{key_id}_ed25519')
    pub     = priv + '.pub'
    try:
        import socket
        comment = f'djmanager-{key_id}@{socket.getfqdn()}'
        os.makedirs(KEYS_DIR, mode=0o700, exist_ok=True)
        subprocess.run(
            ['ssh-keygen', '-t', 'ed25519', '-C', comment, '-f', priv, '-N', ''],
            check=True, capture_output=True,
        )
        os.chmod(priv, 0o600)
        os.chmod(pub,  0o644)
    except Exception as e:
        return None, None, f'Key konnte nicht erstellt werden: {e}'
    # Get fingerprint
    try:
        fp_result = subprocess.run(
            ['ssh-keygen', '-lf', pub], capture_output=True, text=True
        )
        fingerprint = fp_result.stdout.split()[1] if fp_result.returncode == 0 else ''
    except Exception:
        fingerprint = ''
    with open(pub) as f:
        pub_content = f.read().strip()
    # Add to registry
    registry = _load_key_registry()
    registry[key_id] = {
        'id':          key_id,
        'label':       label or key_id,
        'created_at':  datetime.now().isoformat(timespec='seconds'),
        'fingerprint': fingerprint,
    }
    _save_key_registry(registry)
    return key_id, pub_content, None


def list_deploy_keys():
    """
    Return list of key dicts, each with an extra 'projects' list of project
    names that currently use this key (via DEPLOY_KEY_ID in their .conf).
    """
    registry = _load_key_registry()
    # Build project → key_id map
    proj_key = {}
    for p in get_all_projects():
        kid = p.get('DEPLOY_KEY_ID', '').strip()
        if kid:
            proj_key.setdefault(kid, []).append(p['PROJECTNAME'])
    keys = []
    for key_id, meta in registry.items():
        pub = os.path.join(KEYS_DIR, f'{key_id}_ed25519.pub')
        keys.append({
            **meta,
            'projects':  proj_key.get(key_id, []),
            'pub_exists': os.path.exists(pub),
        })
    keys.sort(key=lambda k: k.get('created_at', ''))
    return keys


def get_deploy_key_pubkey(key_id):
    """Return (pub_key_content, error)."""
    pub = os.path.join(KEYS_DIR, f'{key_id}_ed25519.pub')
    if not os.path.exists(pub):
        return None, f'Public Key nicht gefunden (ID: {key_id})'
    try:
        with open(pub) as f:
            return f.read().strip(), None
    except OSError as e:
        return None, str(e)


def delete_deploy_key(key_id):
    """
    Delete key files and remove from registry.
    Returns (ok, error). Refuses if any project still uses this key.
    """
    # Check assignments
    for p in get_all_projects():
        if p.get('DEPLOY_KEY_ID', '').strip() == key_id:
            return False, f'Key wird noch von Projekt "{p["PROJECTNAME"]}" verwendet.'
    registry = _load_key_registry()
    if key_id not in registry:
        return False, 'Key nicht in Registry gefunden.'
    for suffix in ('_ed25519', '_ed25519.pub'):
        path = os.path.join(KEYS_DIR, key_id + suffix)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            return False, str(e)
    del registry[key_id]
    _save_key_registry(registry)
    return True, None


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


def assign_project_deploy_key(project, key_id):
    """
    Assign deploy key key_id to project:
    - writes DEPLOY_KEY_ID to the project's .conf
    - patches the update script to use the correct key path
    Returns (ok, error).
    """
    import re as _re
    registry = _load_key_registry()
    if key_id and key_id not in registry:
        return False, f'Key ID "{key_id}" nicht in Registry.'
    ok, err = set_project_conf_value(project, 'DEPLOY_KEY_ID', key_id)
    if not ok:
        return False, err
    # Patch update script
    if key_id:
        key_path = os.path.join(KEYS_DIR, f'{key_id}_ed25519')
        script   = f'/usr/local/bin/{project}_update.sh'
        if os.path.exists(script):
            try:
                with open(script) as f:
                    content = f.read()
                new_content = _re.sub(
                    r'GITHUB_DEPLOY_KEY="[^"]*"',
                    f'GITHUB_DEPLOY_KEY="{key_path}"',
                    content,
                )
                if new_content != content:
                    with open(script, 'w') as f:
                        f.write(new_content)
            except OSError:
                pass
    return True, None


def get_project_deploy_key(project):
    """
    Return (pub_key_content, error) for the project's currently assigned key.
    Falls back to legacy /root/.ssh/deploy_{project}_ed25519 if no registry key.
    """
    conf = get_project(project)
    if not conf:
        return None, 'Projekt nicht gefunden'
    key_id = conf.get('DEPLOY_KEY_ID', '').strip()
    if key_id:
        return get_deploy_key_pubkey(key_id)
    # Legacy fallback
    legacy = f'/root/.ssh/deploy_{project}_ed25519.pub'
    if os.path.exists(legacy):
        try:
            with open(legacy) as f:
                return f.read().strip(), None
        except OSError as e:
            return None, str(e)
    return None, 'Kein Deploy Key zugewiesen.'


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

    # Sanitize host list
    hosts = [h.strip() for h in hosts if h.strip()]
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


def get_ufw_status(gunicorn_port=None):
    """
    Returns dict with ufw status and relevant rules.
    {enabled, rules: [{num, action, to, from, comment}], port_blocked}
    """
    result = {'enabled': False, 'rules': [], 'port_blocked': None, 'available': False}
    try:
        r = subprocess.run(['ufw', 'status', 'numbered'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0 and 'not found' in r.stderr:
            return result
        result['available'] = True
        output = r.stdout
        result['enabled'] = 'Status: active' in output
        for line in output.splitlines():
            # Format: [ 1] 80/tcp                     ALLOW IN    Anywhere
            import re
            m = re.match(r'\[\s*(\d+)\]\s+(\S+)\s+(ALLOW|DENY|REJECT)\s+(\S+)\s*(.*)', line)
            if m:
                result['rules'].append({
                    'num': m.group(1),
                    'to': m.group(2),
                    'action': m.group(3),
                    'frm': m.group(4),
                    'comment': m.group(5).strip(),
                })
        if gunicorn_port:
            port_str = str(gunicorn_port)
            for rule in result['rules']:
                if port_str in rule['to'] and rule['action'] == 'DENY':
                    result['port_blocked'] = True
                    break
            if result['port_blocked'] is None and result['enabled']:
                result['port_blocked'] = False

        # Prüfe wichtige Ports für den Dashboard-Banner
        if result['enabled'] and result['rules']:
            import re as _re

            def _check_port(port_num):
                """'allow' | 'deny' | None (kein Regel)"""
                p = str(port_num)
                for rule in result['rules']:
                    to = rule['to']
                    # Matches: "22", "22/tcp", "22/udp", "22 (v6)"
                    if _re.match(r'^' + p + r'(/\w+)?(\s|$)', to):
                        return rule['action'].lower()
                return None

            def _check_range(lo, hi):
                """'deny' wenn ein DENY-Regel den Bereich abdeckt, sonst 'allow'/'none'"""
                pat = _re.compile(r'^(\d+):(\d+)')
                for rule in result['rules']:
                    m = pat.match(rule['to'])
                    if m and int(m.group(1)) <= lo and int(m.group(2)) >= hi:
                        return rule['action'].lower()
                return None

            result['ports'] = {
                'ssh':      _check_port(22),
                'http':     _check_port(80),
                'https':    _check_port(443),
                'manager':  _check_port(8888),
                'gunicorn': _check_range(8000, 8999),
            }
        else:
            result['ports'] = {}

    except FileNotFoundError:
        pass
    except Exception:
        pass
    return result


def get_ufw_port_rules():
    """
    Returns a list of all current ufw rules with port info.
    [{'port': '8888', 'proto': 'tcp', 'action': 'ALLOW'|'DENY', 'comment': '...'}]
    """
    import re
    rules = []
    try:
        r = subprocess.run(['ufw', 'status', 'verbose'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return rules
        for line in r.stdout.splitlines():
            # e.g. "8888/tcp                   DENY IN     Anywhere"
            # or   "80/tcp (v6)                ALLOW IN    Anywhere (v6)"
            m = re.match(r'(\d+)(?:/(tcp|udp))?\s+(ALLOW|DENY|REJECT)\s+(?:IN\s+)?Anywhere', line)
            if m:
                port  = m.group(1)
                proto = m.group(2) or 'tcp'
                action = m.group(3)
                # extract comment from numbered output if present
                comment_match = re.search(r'#\s*(.+)', line)
                comment = comment_match.group(1).strip() if comment_match else ''
                # avoid duplicates (IPv4/IPv6)
                if not any(r['port'] == port and r['proto'] == proto for r in rules):
                    rules.append({'port': port, 'proto': proto, 'action': action, 'comment': comment})
    except Exception:
        pass
    return rules


def ufw_toggle_port(port, proto, action):
    """
    Open or close a port via ufw.
    action: 'allow' or 'deny'
    Returns (success: bool, message: str)
    """
    import re
    port = str(port).strip()
    proto = proto.strip().lower()
    action = action.strip().lower()

    if not re.match(r'^\d{1,5}$', port) or int(port) > 65535:
        return False, f'Ungültige Port-Nummer: {port}'
    if proto not in ('tcp', 'udp'):
        return False, f'Ungültiges Protokoll: {proto}'
    if action not in ('allow', 'deny'):
        return False, f'Ungültige Aktion: {action}'

    try:
        r = subprocess.run(
            ['ufw', action, f'{port}/{proto}'],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            subprocess.run(['ufw', 'reload'], capture_output=True, timeout=10)
            verb = 'geöffnet' if action == 'allow' else 'gesperrt'
            return True, f'Port {port}/{proto} {verb}.'
        return False, (r.stderr or r.stdout).strip()
    except Exception as e:
        return False, str(e)


def get_nginx_stats(name, max_lines=20000):
    """
    Parse per-project nginx access log.
    Format: IP - user [DD/Mon/YYYY:time] "METHOD /path HTTP/x" STATUS bytes "ref" "ua" [req_time]
    Returns dict: total, by_status, by_day (last 7), top_urls, top_ips, avg_rt, has_rt
    """
    import re
    import datetime
    from collections import defaultdict

    log_path = f'/var/log/nginx/{name}.access.log'
    fallback = '/var/log/nginx/access.log'
    result = {
        'available': False,
        'log_path': log_path,
        'total': 0,
        'by_status': {},
        'by_day': [],
        'top_urls': [],
        'top_ips': [],
        'avg_rt': None,
        'has_rt': False,
    }
    path = log_path if os.path.isfile(log_path) else fallback if os.path.isfile(fallback) else None
    if not path:
        return result
    result['available'] = True
    result['log_path'] = path

    pat = re.compile(
        r'(?P<ip>\S+) - \S+ \[(?P<day>\d{2}/\w{3}/\d{4}):[^\]]+\] '
        r'"(?P<method>\S+) (?P<url>\S+) [^"]*" '
        r'(?P<status>\d+) \S+ "[^"]*" "[^"]*"'
        r'(?: (?P<rt>[\d.]+))?'
    )
    _mo = {'Jan': '01', 'Feb': '02', 'Mar': '03', 'Apr': '04', 'May': '05', 'Jun': '06',
           'Jul': '07', 'Aug': '08', 'Sep': '09', 'Oct': '10', 'Nov': '11', 'Dec': '12'}

    today = datetime.date.today()
    last7_fmt = [(today - datetime.timedelta(days=i)).strftime('%d/%b/%Y') for i in range(6, -1, -1)]
    by_day = {d: 0 for d in last7_fmt}
    by_status = defaultdict(int)
    url_counts = defaultdict(int)
    ip_counts = defaultdict(int)
    rt_total, rt_count = 0.0, 0

    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            fsize = f.tell()
            seek_pos = max(0, fsize - max_lines * 250)
            f.seek(seek_pos)
            if seek_pos > 0:
                f.readline()
            raw = f.read()
        lines = raw.decode('utf-8', errors='replace').splitlines()
    except OSError:
        return result

    for line in lines:
        m = pat.match(line)
        if not m:
            continue
        result['total'] += 1
        status = m.group('status')
        by_status[status[0] + 'xx'] += 1
        day = m.group('day')
        if day in by_day:
            by_day[day] += 1
        url = m.group('url')
        if not any(url.startswith(p) for p in ('/static/', '/media/', '/favicon')):
            url_counts[url] += 1
        ip_counts[m.group('ip')] += 1
        if m.group('rt'):
            try:
                rt_total += float(m.group('rt'))
                rt_count += 1
            except ValueError:
                pass

    result['by_status'] = dict(sorted(by_status.items()))
    result['by_day'] = [
        {'label': d.split('/')[0] + '.' + _mo.get(d.split('/')[1], '?'), 'count': by_day[d]}
        for d in last7_fmt
    ]
    result['top_urls'] = sorted(url_counts.items(), key=lambda x: -x[1])[:10]
    result['top_ips'] = sorted(ip_counts.items(), key=lambda x: -x[1])[:10]
    if rt_count:
        result['has_rt'] = True
        result['avg_rt'] = round(rt_total / rt_count * 1000)  # ms
    return result


def get_service_restarts(name, days=14):
    """
    Query systemd journal for start/fail/stop events of a service in the last N days.
    Returns {'available': bool, 'events': [...], 'starts': int, 'failures': int}
    """
    import datetime
    since = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime('%Y-%m-%d')
    result = {'available': False, 'events': [], 'starts': 0, 'failures': 0, 'stops': 0}
    try:
        r = subprocess.run(
            ['journalctl', '-u', name, '--since', since, '--no-pager',
             '-o', 'short-iso', '--grep',
             'Started|Failed|Stopped|Restarting|Main process exited'],
            capture_output=True, text=True, timeout=10
        )
        result['available'] = True
        events = []
        for line in r.stdout.splitlines():
            if not line or line.startswith('--'):
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            ts, msg = parts[0], parts[1]
            if 'Started' in msg:
                kind = 'start'
                result['starts'] += 1
            elif 'Failed' in msg or 'exited' in msg or 'failed' in msg:
                kind = 'fail'
                result['failures'] += 1
            elif 'Stopped' in msg or 'Stopping' in msg:
                kind = 'stop'
                result['stops'] += 1
            else:
                kind = 'info'
            display = msg.split(': ', 1)[-1] if ': ' in msg else msg
            # Format timestamp: 2024-01-15T10:30:00+0100 -> 15.01. 10:30
            try:
                dt_str = ts[:16]  # 2024-01-15T10:30
                from datetime import datetime as dt
                dobj = dt.fromisoformat(dt_str)
                ts_disp = dobj.strftime('%d.%m. %H:%M')
            except Exception:
                ts_disp = ts[:16]
            events.append({'time': ts_disp, 'event': kind, 'msg': display[:120]})
        result['events'] = list(reversed(events))[-50:]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return result


def get_server_stats():
    """Return basic server resource stats: RAM, disk, load average."""
    stats = {}
    # Memory
    try:
        with open('/proc/meminfo') as f:
            mem = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(':')] = int(parts[1])
        total_kb = mem.get('MemTotal', 0)
        available_kb = mem.get('MemAvailable', 0)
        stats['mem_total_mb'] = round(total_kb / 1024)
        stats['mem_used_mb'] = round((total_kb - available_kb) / 1024)
        stats['mem_percent'] = round((total_kb - available_kb) / total_kb * 100) if total_kb else 0
    except Exception:
        stats['mem_total_mb'] = stats['mem_used_mb'] = stats['mem_percent'] = None
    # Disk (root filesystem)
    try:
        st = os.statvfs('/')
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
        used = total - free
        stats['disk_total_gb'] = round(total / 1024 ** 3, 1)
        stats['disk_used_gb'] = round(used / 1024 ** 3, 1)
        stats['disk_percent'] = round(used / total * 100) if total else 0
    except Exception:
        stats['disk_total_gb'] = stats['disk_used_gb'] = stats['disk_percent'] = None
    # Load average
    try:
        with open('/proc/loadavg') as f:
            parts = f.read().split()
        stats['load1'] = parts[0]
        stats['load5'] = parts[1]
        stats['load15'] = parts[2]
    except Exception:
        stats['load1'] = stats['load5'] = stats['load15'] = None
    return stats


def get_last_backup(project):
    """Return mtime of most recent backup, or None."""
    backups = list_backups(project)
    if backups:
        import datetime
        ts = backups[0]['mtime']
        return datetime.datetime.fromtimestamp(ts).strftime('%d.%m.%Y %H:%M')
    return None


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


# ──────────────────────────────────────────────────────────────────────────────
# Security scans
# ──────────────────────────────────────────────────────────────────────────────

def run_pip_audit(project):
    """
    Run pip-audit against the project's venv.
    Returns a dict: {'ok': bool, 'vulnerabilities': [...], 'error': str}
    """
    venv_python = f'/srv/{project}/.venv/bin/python'
    if not os.path.exists(venv_python):
        return {'ok': False, 'vulnerabilities': [], 'error': 'venv nicht gefunden'}

    try:
        result = subprocess.run(
            [venv_python, '-m', 'pip_audit', '--format=json', '--progress-spinner=off'],
            capture_output=True, text=True, timeout=120,
        )
        # pip_audit exits with 1 when vulnerabilities are found — not an error
        output = result.stdout.strip()
        if not output:
            return {'ok': True, 'vulnerabilities': [], 'error': ''}
        data = json.loads(output)
        vulns = []
        for dep in data.get('dependencies', []):
            for vuln in dep.get('vulns', []):
                vulns.append({
                    'package': dep.get('name', ''),
                    'version': dep.get('version', ''),
                    'id':      vuln.get('id', ''),
                    'fix':     vuln.get('fix_versions', []),
                    'desc':    vuln.get('description', '')[:200],
                })
        return {'ok': True, 'vulnerabilities': vulns, 'error': ''}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'vulnerabilities': [], 'error': 'Timeout (pip-audit)'}
    except json.JSONDecodeError:
        return {'ok': False, 'vulnerabilities': [], 'error': 'Ungültige pip-audit Ausgabe'}
    except Exception as e:
        return {'ok': False, 'vulnerabilities': [], 'error': str(e)}


def run_django_deploy_check(project):
    """
    Run `manage.py check --deploy` and return parsed issues.
    Returns a dict: {'ok': bool, 'issues': [...], 'error': str}
    """
    venv_python = f'/srv/{project}/.venv/bin/python'
    manage_py   = f'/srv/{project}/manage.py'
    if not os.path.exists(venv_python) or not os.path.exists(manage_py):
        return {'ok': False, 'issues': [], 'error': 'Projekt-Dateien nicht gefunden'}

    conf = _parse_conf(f'/etc/django-servers.d/{project}.conf') if os.path.exists(
        f'/etc/django-servers.d/{project}.conf') else {}
    env_file = f'/srv/{project}/.env'

    try:
        env = os.environ.copy()
        # Remove manager's DJANGO_SETTINGS_MODULE so manage.py sets its own
        env.pop('DJANGO_SETTINGS_MODULE', None)
        env['PYTHONPATH'] = f'/srv/{project}'
        # Load env from .env file — direct assignment so project values always win
        # over anything the manager process may have inherited (e.g. DEBUG, MODE)
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, _, v = line.partition('=')
                        env[k.strip()] = v.strip().strip('"\'')  # overwrite, not setdefault

        result = subprocess.run(
            [venv_python, manage_py, 'check', '--deploy'],
            capture_output=True, text=True, timeout=30,
            cwd=f'/srv/{project}', env=env,
        )
        mode_used = env.get('MODE', '?')
        output = (result.stdout + result.stderr).strip()
        # Prepend detected MODE so it's visible in the output
        output = f'[deploy check] MODE={mode_used}\n\n' + output
        # Filter silenced checks from output (compatible with all Django versions)
        # W008=SECURE_SSL_REDIRECT: handled by nginx
        SILENCED = {'security.W008'}
        filtered_lines = []
        skip_block = False
        for line in output.splitlines():
            stripped = line.strip()
            if any(f'({c})' in stripped for c in SILENCED):
                skip_block = True  # skip this warning line
                continue
            if skip_block and stripped and not stripped.startswith('?:') and not stripped.startswith('System check'):
                continue  # skip continuation lines of silenced warning
            skip_block = False
            filtered_lines.append(line)
        output = '\n'.join(filtered_lines)
        issues = []
        for line in output.splitlines():
            line = line.strip()
            if line and (
                line.startswith('WARNINGS:') or
                line.startswith('System check') or
                ': (' in line
            ):
                issues.append(line)
        ok = result.returncode == 0
        # Keep last 5000 chars so the actual error at the end of the traceback is visible
        raw = output[-5000:] if len(output) > 5000 else output
        return {'ok': ok, 'issues': issues, 'raw': raw, 'error': ''}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'issues': [], 'raw': '', 'error': 'Timeout (deploy check)'}
    except Exception as e:
        return {'ok': False, 'issues': [], 'raw': '', 'error': str(e)}


def run_manager_pip_audit():
    """Run pip-audit against the manager's own venv."""
    venv_python = os.path.join(settings.MANAGER_VENV, 'bin', 'python')
    if not os.path.exists(venv_python):
        return {'ok': False, 'vulnerabilities': [], 'error': f'Manager-venv nicht gefunden: {venv_python}'}
    try:
        result = subprocess.run(
            [venv_python, '-m', 'pip_audit', '--format=json', '--progress-spinner=off'],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout.strip()
        if not output:
            return {'ok': True, 'vulnerabilities': [], 'error': ''}
        data = json.loads(output)
        vulns = []
        for dep in data.get('dependencies', []):
            for vuln in dep.get('vulns', []):
                vulns.append({
                    'package': dep.get('name', ''),
                    'version': dep.get('version', ''),
                    'id':      vuln.get('id', ''),
                    'fix':     vuln.get('fix_versions', []),
                    'desc':    vuln.get('description', '')[:200],
                })
        return {'ok': True, 'vulnerabilities': vulns, 'error': ''}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'vulnerabilities': [], 'error': 'Timeout (pip-audit)'}
    except json.JSONDecodeError:
        return {'ok': False, 'vulnerabilities': [], 'error': 'Ungültige pip-audit Ausgabe'}
    except Exception as e:
        return {'ok': False, 'vulnerabilities': [], 'error': str(e)}


def run_manager_deploy_check():
    """Run manage.py check --deploy on the manager itself."""
    venv_python = os.path.join(settings.MANAGER_VENV, 'bin', 'python')
    manage_py   = str(Path(settings.BASE_DIR) / 'manage.py')
    if not os.path.exists(venv_python) or not os.path.exists(manage_py):
        return {'ok': False, 'issues': [], 'raw': '', 'error': 'Manager-Dateien nicht gefunden'}
    try:
        env = os.environ.copy()
        env.pop('DJANGO_SETTINGS_MODULE', None)
        result = subprocess.run(
            [venv_python, manage_py, 'check', '--deploy'],
            capture_output=True, text=True, timeout=30,
            cwd=str(Path(manage_py).parent), env=env,
        )
        output = (result.stdout + result.stderr).strip()
        issues = []
        for line in output.splitlines():
            line = line.strip()
            if line and (
                line.startswith('WARNINGS:') or
                line.startswith('System check') or
                ': (' in line
            ):
                issues.append(line)
        ok = result.returncode == 0
        raw = output[-5000:] if len(output) > 5000 else output
        return {'ok': ok, 'issues': issues, 'raw': raw, 'error': ''}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'issues': [], 'raw': '', 'error': 'Timeout (deploy check)'}
    except Exception as e:
        return {'ok': False, 'issues': [], 'raw': '', 'error': str(e)}


def run_migration_status(name):
    """
    Run 'manage.py showmigrations --list' for the given project.
    Returns {'ok': bool, 'apps': [{'app': str, 'migrations': [{'name': str, 'applied': bool}]}], 'error': str}
    """
    import re as _re
    conf = get_project(name)
    if not conf:
        return {'ok': False, 'apps': [], 'error': 'Projekt nicht gefunden'}
    appdir      = conf.get('APPDIR', f'/srv/{name}')
    appuser     = conf.get('APPUSER', '')
    venv_python = os.path.join(appdir, '.venv', 'bin', 'python')
    manage_py   = os.path.join(appdir, 'manage.py')
    if not os.path.exists(venv_python):
        return {'ok': False, 'apps': [], 'error': f'venv nicht gefunden: {venv_python}'}
    if not os.path.exists(manage_py):
        return {'ok': False, 'apps': [], 'error': f'manage.py nicht gefunden: {manage_py}'}

    full_cmd = (
        f'cd {shlex.quote(appdir)} && '
        f'{shlex.quote(venv_python)} manage.py showmigrations --list'
    )
    run_args = (['su', '-', appuser, '-s', '/bin/bash', '-c', full_cmd]
                if appuser else ['bash', '-c', full_cmd])
    try:
        result = subprocess.run(run_args, capture_output=True, text=True, timeout=60)
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0 and not result.stdout.strip():
            return {'ok': False, 'apps': [], 'error': output[:500]}

        # Parse output: app lines have no leading space, migration lines start with ' [X]' or ' [ ]'
        apps = []
        current_app = None
        for line in result.stdout.splitlines():
            app_match = _re.match(r'^(\S+)$', line.strip())
            mig_match = _re.match(r'^\s+\[( |X)\]\s+(.+)$', line)
            if app_match and not line.startswith(' '):
                current_app = {'app': line.strip(), 'migrations': []}
                apps.append(current_app)
            elif mig_match and current_app is not None:
                current_app['migrations'].append({
                    'name':    mig_match.group(2).strip(),
                    'applied': mig_match.group(1) == 'X',
                })
        return {'ok': True, 'apps': apps, 'error': ''}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'apps': [], 'error': 'Timeout nach 60 Sekunden'}
    except Exception as e:
        return {'ok': False, 'apps': [], 'error': str(e)}


def run_pip_outdated(name):
    """
    Run 'pip list --outdated --format=json' in the project venv.
    Returns {'ok': bool, 'packages': [{'name', 'current', 'latest', 'type'}], 'error': str}
    """
    conf = get_project(name)
    if not conf:
        return {'ok': False, 'packages': [], 'error': 'Projekt nicht gefunden'}
    appdir      = conf.get('APPDIR', f'/srv/{name}')
    appuser     = conf.get('APPUSER', '')
    venv_python = os.path.join(appdir, '.venv', 'bin', 'python')
    if not os.path.exists(venv_python):
        return {'ok': False, 'packages': [], 'error': f'venv nicht gefunden: {venv_python}'}

    full_cmd = f'{shlex.quote(venv_python)} -m pip list --outdated --format=json'
    run_args = (['su', '-', appuser, '-s', '/bin/bash', '-c', full_cmd]
                if appuser else ['bash', '-c', full_cmd])
    try:
        result = subprocess.run(run_args, capture_output=True, text=True, timeout=120)
        raw = result.stdout.strip()
        if not raw:
            return {'ok': True, 'packages': [], 'error': ''}
        data = json.loads(raw)
        packages = [
            {
                'name':    p.get('name', ''),
                'current': p.get('version', ''),
                'latest':  p.get('latest_version', ''),
                'type':    p.get('latest_filetype', ''),
            }
            for p in data
        ]
        return {'ok': True, 'packages': packages, 'error': ''}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'packages': [], 'error': 'Timeout nach 120 Sekunden'}
    except json.JSONDecodeError:
        return {'ok': False, 'packages': [], 'error': 'Ungültige pip-Ausgabe'}
    except Exception as e:
        return {'ok': False, 'packages': [], 'error': str(e)}


def run_pip_upgrade(name, package_name):
    """
    Upgrade a single package in the project venv.
    Returns {'ok': bool, 'output': str}
    """
    import re as _re
    if not package_name or not _re.match(r'^[A-Za-z0-9_.\-]+$', package_name):
        return {'ok': False, 'output': 'Ungültiger Paketname'}
    conf = get_project(name)
    if not conf:
        return {'ok': False, 'output': 'Projekt nicht gefunden'}
    appdir      = conf.get('APPDIR', f'/srv/{name}')
    appuser     = conf.get('APPUSER', '')
    venv_python = os.path.join(appdir, '.venv', 'bin', 'python')
    if not os.path.exists(venv_python):
        return {'ok': False, 'output': f'venv nicht gefunden: {venv_python}'}

    full_cmd = f'{shlex.quote(venv_python)} -m pip install --upgrade {shlex.quote(package_name)}'
    run_args = (['su', '-', appuser, '-s', '/bin/bash', '-c', full_cmd]
                if appuser else ['bash', '-c', full_cmd])
    try:
        result = subprocess.run(run_args, capture_output=True, text=True, timeout=180)
        output = (result.stdout + result.stderr).strip()
        return {'ok': result.returncode == 0, 'output': output or '(keine Ausgabe)'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'output': 'Timeout nach 180 Sekunden'}
    except Exception as e:
        return {'ok': False, 'output': str(e)}


# ──────────────────────────────────────────────────────────────────────────────
# HTTP / TLS Security Scan
# ──────────────────────────────────────────────────────────────────────────────

def _get_local_ips():
    """Return the set of IPv4 addresses assigned to this host.

    Tries ``hostname -I`` first (Linux), falls back to socket-based
    resolution so the function works on any POSIX system.
    """
    ips = set()
    # Primary: hostname -I (Linux only)
    try:
        out = subprocess.check_output(['hostname', '-I'], text=True, timeout=3)
        for token in out.split():
            try:
                addr = ipaddress.ip_address(token)
                if isinstance(addr, ipaddress.IPv4Address):
                    ips.add(token)
            except ValueError:
                pass
    except Exception:
        pass
    # Fallback: socket resolution (cross-platform)
    if not ips:
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith('127.'):
                    ips.add(ip)
        except Exception:
            pass
    return ips


def _resolve_connect_ip(hostname, port):
    """
    Resolve hostname to an IPv4 connect address.
    If the IP belongs to this host (hairpin-NAT situation), return 127.0.0.1
    so nginx can be reached via the loopback interface.
    Returns (connect_ip, original_ipv4) or (None, None) on failure.
    """
    try:
        infos = socket.getaddrinfo(hostname, port, socket.AF_INET, socket.SOCK_STREAM)
        if not infos:
            return None, None
        ipv4 = infos[0][4][0]
        local_ips = _get_local_ips()
        if ipv4 in local_ips:
            return '127.0.0.1', ipv4
        # The resolved IP is not in local_ips — this can happen when the server
        # is behind NAT and only has a private interface IP (hostname -I returns
        # e.g. 10.x.x.x) while gps2.famhub.eu resolves to the public IP.
        # Probe loopback: if nginx is listening on this port locally, use 127.0.0.1.
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=2):
                return '127.0.0.1', ipv4
        except OSError:
            pass
        return ipv4, ipv4
    except socket.gaierror:
        return None, None


def _http_get(url, timeout=10, verify_ssl=True, follow_redirects=False):
    """
    Fetch a URL and return (status_code, headers_dict, body_bytes, final_url, error).
    headers_dict keys are lowercased.
    Forces IPv4 TCP connections to avoid IPv6 issues, while preserving SNI/Host.
    """
    import http.client

    if verify_ssl:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        # Allow broader cipher compatibility — needed for self-signed certs with older key sizes
        try:
            ctx.set_ciphers('DEFAULT:@SECLEVEL=1')
        except ssl.SSLError:
            pass

    # Custom connection classes that force IPv4 at TCP level but keep original
    # hostname for SNI (TLS) and Host header — required for nginx virtual hosting.
    # Also detects hairpin-NAT (server connecting to its own external IP) and
    # redirects to 127.0.0.1 so nginx loopback routing works correctly.
    class _V4Conn(http.client.HTTPConnection):
        def connect(self):
            connect_ip, _ = _resolve_connect_ip(self.host, self.port or 80)
            if connect_ip:
                self.host = connect_ip
            super().connect()

    class _V4TLSConn(http.client.HTTPSConnection):
        def connect(self):
            sni_host = self.host  # original hostname for SNI / nginx vhost routing
            connect_ip, _ = _resolve_connect_ip(self.host, self.port or 443)
            if connect_ip:
                original_host = self.host
                self.host = connect_ip
                # TCP-only connect (HTTPConnection, no SSL)
                http.client.HTTPConnection.connect(self)
                # Restore original hostname so SSL uses correct SNI
                self.host = original_host
                server_hostname = self._tunnel_host if self._tunnel_host else self.host
                self.sock = self._context.wrap_socket(
                    self.sock, server_hostname=server_hostname
                )
            else:
                super().connect()

    class _V4HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(_V4Conn, req)

    class _V4HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_V4TLSConn, req, context=ctx)

    opener = urllib.request.build_opener(_V4HTTPHandler(), _V4HTTPSHandler())
    opener.addheaders = [('User-Agent', 'DjangoMultiDeploySecurityScanner/1.0')]
    if not follow_redirects:
        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener.add_handler(NoRedirectHandler())
    try:
        with opener.open(url, timeout=timeout) as resp:
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            # Preserve ALL Set-Cookie headers — dict comprehension above keeps
            # only the last one when multiple cookies are set.
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
        return None, {}, b'', url, str(e.reason)
    except Exception as e:
        return None, {}, b'', url, str(e)


def _check_tls(hostname, port=443):
    """Return TLS info dict for hostname:port."""
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
    try:
        ctx = ssl.create_default_context()
        # Force IPv4 and handle hairpin-NAT (server → own external IP → 127.0.0.1)
        connect_ip, _ = _resolve_connect_ip(hostname, port)
        connect_addr = (connect_ip or hostname, port)
        with socket.create_connection(connect_addr, timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                result['reachable'] = True
                result['tls_version'] = ssock.version()
                cipher = ssock.cipher()
                result['cipher'] = cipher[0] if cipher else None
                cert = ssock.getpeercert()
                # Subject
                subj = dict(x[0] for x in cert.get('subject', []))
                result['cert_subject'] = subj.get('commonName', '')
                issuer = dict(x[0] for x in cert.get('issuer', []))
                result['cert_issuer'] = issuer.get('organizationName', issuer.get('commonName', ''))
                # Expiry
                not_after = cert.get('notAfter', '')
                if not_after:
                    exp = datetime.datetime.strptime(not_after, '%b %d %H:%M:%S %Y %Z')
                    result['cert_expiry'] = exp.strftime('%Y-%m-%d')
                    result['cert_days_left'] = (exp - datetime.datetime.utcnow()).days
                result['cert_valid'] = True
    except ssl.SSLCertVerificationError as e:
        result['reachable'] = True
        result['cert_valid'] = False
        result['error'] = f'Zertifikat ungültig: {e}'
    except ssl.SSLError as e:
        # Host reachable but TLS negotiation failed (wrong SNI, cipher mismatch, etc.)
        result['reachable'] = True
        result['error'] = f'TLS-Fehler: {e.reason or str(e)}'
    except ConnectionRefusedError:
        result['error'] = 'Verbindung abgelehnt'
    except socket.timeout:
        result['error'] = 'Timeout'
    except OSError as e:
        # Catches socket errors like EHOSTUNREACH, ENETUNREACH, ETIMEDOUT
        result['error'] = str(e)
    except Exception as e:
        result['error'] = str(e)
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
    # Paths where a redirect (301/302) is likely a false positive
    # (e.g. auth redirect to login page, not an actual file served)
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
    leaks.sort(key=lambda x: path_order.get(x['path'], 999))
    return leaks


def _check_security_headers(headers):
    """Analyse response headers and return list of findings."""
    findings = []

    def check(name, severity, present_ok, absent_msg, value_check_fn=None, ok_msg=None,
              fix_location='nginx', fix_snippet=None):
        val = headers.get(name.lower())
        if val is None:
            findings.append({'header': name, 'severity': severity, 'status': 'missing',
                             'value': None, 'msg': absent_msg,
                             'fix_location': fix_location, 'fix_snippet': fix_snippet})
        else:
            if value_check_fn:
                warn = value_check_fn(val)
                if warn:
                    findings.append({'header': name, 'severity': 'warning', 'status': 'weak',
                                     'value': val, 'msg': warn,
                                     'fix_location': fix_location, 'fix_snippet': None})
                else:
                    findings.append({'header': name, 'severity': 'ok', 'status': 'ok',
                                     'value': val, 'msg': ok_msg or 'OK',
                                     'fix_location': fix_location, 'fix_snippet': None})
            else:
                findings.append({'header': name, 'severity': 'ok', 'status': 'ok',
                                 'value': val, 'msg': ok_msg or 'OK',
                                 'fix_location': fix_location, 'fix_snippet': None})

    # Strict-Transport-Security
    check('Strict-Transport-Security', 'high',
          present_ok=True,
          absent_msg='HSTS fehlt — Browser kann unverschlüsselt verbinden.',
          fix_location='nginx',
          fix_snippet='add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;',
          value_check_fn=lambda v: (
              'max-age zu kurz (empfohlen ≥ 31536000)'
              if 'max-age' in v.lower() and any(
                  int(p.split('=')[1]) < 31536000
                  for p in v.lower().split(';')
                  if 'max-age=' in p and p.split('=')[1].strip().isdigit()
              ) else None
          ))

    # Content-Security-Policy
    def csp_check(v):
        warnings = []
        directives = {}
        for part in v.split(';'):
            part = part.strip()
            if not part:
                continue
            tokens = part.split()
            if tokens:
                directives[tokens[0].lower()] = ' '.join(tokens[1:]).lower()

        script_policy = directives.get('script-src', directives.get('default-src', ''))
        if "'unsafe-inline'" in script_policy:
            warnings.append("'unsafe-inline' in script-src erlaubt XSS")
        if "'unsafe-eval'" in script_policy:
            warnings.append("'unsafe-eval' in script-src erlaubt Code-Injection")

        style_policy = directives.get('style-src', directives.get('default-src', ''))
        if "'unsafe-inline'" in style_policy and "'unsafe-inline'" not in script_policy:
            warnings.append(
                "'unsafe-inline' in style-src (CSS-Injection möglich, kein JS)"
            )

        if not warnings:
            return None
        return '; '.join(warnings)

    check('Content-Security-Policy', 'high',
          present_ok=True,
          absent_msg='CSP fehlt — kein Schutz gegen XSS und Dateninjektionen.',
          fix_location='webapp',
          fix_snippet=(
              "# settings.py\n"
              "MIDDLEWARE += ['csp.middleware.CSPMiddleware']  # pip install django-csp\n"
              "CSP_DEFAULT_SRC = (\"'self'\",)\n"
              "CSP_STYLE_SRC   = (\"'self'\", \"'unsafe-inline'\")\n"
              "CSP_SCRIPT_SRC  = (\"'self'\",)"
          ),
          value_check_fn=csp_check)

    # X-Frame-Options
    check('X-Frame-Options', 'medium',
          present_ok=True,
          absent_msg='X-Frame-Options fehlt — Clickjacking möglich.',
          fix_location='nginx',
          fix_snippet='add_header X-Frame-Options "DENY" always;',
          value_check_fn=lambda v: (
              None if v.upper() in ('DENY', 'SAMEORIGIN') else
              f'Wert "{v}" unbekannt. Empfohlen: DENY oder SAMEORIGIN.'
          ))

    # X-Content-Type-Options
    check('X-Content-Type-Options', 'medium',
          present_ok=True,
          absent_msg='X-Content-Type-Options fehlt — MIME-Sniffing möglich.',
          fix_location='nginx',
          fix_snippet='add_header X-Content-Type-Options "nosniff" always;',
          value_check_fn=lambda v: (
              None if v.lower() == 'nosniff' else f'Wert "{v}" — sollte "nosniff" sein.'
          ))

    # Referrer-Policy
    check('Referrer-Policy', 'low',
          present_ok=True,
          fix_location='nginx',
          fix_snippet='add_header Referrer-Policy "same-origin" always;',
          absent_msg='Referrer-Policy fehlt — URLs können an externe Seiten gesendet werden.')

    # Permissions-Policy
    check('Permissions-Policy', 'low',
          present_ok=True,
          fix_location='nginx',
          fix_snippet='add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;',
          absent_msg='Permissions-Policy fehlt — Browser-Features nicht eingeschränkt.')

    # X-XSS-Protection (deprecated but informative)
    xss = headers.get('x-xss-protection')
    if xss and xss.strip() == '0':
        findings.append({'header': 'X-XSS-Protection', 'severity': 'info', 'status': 'ok',
                         'value': xss, 'msg': 'Auf 0 gesetzt (browser-seitig deaktiviert, CSP bevorzugt)',
                         'fix_location': 'nginx', 'fix_snippet': None})

    # Server header (version leak)
    server = headers.get('server')
    if server:
        import re
        if re.search(r'[\d.]', server):
            findings.append({'header': 'Server', 'severity': 'low', 'status': 'weak',
                             'value': server,
                             'msg': 'Server-Header enthält Versionsinfos — per nginx: server_tokens off;',
                             'fix_location': 'nginx',
                             'fix_snippet': 'server_tokens off;  # in nginx http{} oder server{} Block'})
        else:
            findings.append({'header': 'Server', 'severity': 'ok', 'status': 'ok',
                             'value': server, 'msg': 'Kein Versions-Leak.',
                             'fix_location': 'nginx', 'fix_snippet': None})

    # X-Powered-By
    powered = headers.get('x-powered-by')
    if powered:
        findings.append({'header': 'X-Powered-By', 'severity': 'low', 'status': 'weak',
                         'value': powered,
                         'msg': 'X-Powered-By gibt Technologie-Infos preis — sollte entfernt werden.',
                         'fix_location': 'nginx',
                         'fix_snippet': 'proxy_hide_header X-Powered-By;'})

    # Cross-Origin-Opener-Policy (COOP)
    coop = headers.get('cross-origin-opener-policy')
    if coop is None:
        findings.append({'header': 'Cross-Origin-Opener-Policy', 'severity': 'low', 'status': 'missing',
                         'value': None,
                         'msg': 'COOP fehlt — Schutz gegen Spectre/Cross-Origin-Leaks empfohlen.',
                         'fix_location': 'nginx',
                         'fix_snippet': 'add_header Cross-Origin-Opener-Policy "same-origin" always;'})
    else:
        findings.append({'header': 'Cross-Origin-Opener-Policy', 'severity': 'ok', 'status': 'ok',
                         'value': coop, 'msg': 'OK', 'fix_location': 'nginx', 'fix_snippet': None})

    # Cross-Origin-Embedder-Policy (COEP)
    coep = headers.get('cross-origin-embedder-policy')
    if coep is None:
        findings.append({'header': 'Cross-Origin-Embedder-Policy', 'severity': 'low', 'status': 'missing',
                         'value': None,
                         'msg': 'COEP fehlt — für SharedArrayBuffer / high-res timers nötig. Empfohlen: require-corp',
                         'fix_location': 'nginx',
                         'fix_snippet': 'add_header Cross-Origin-Embedder-Policy "require-corp" always;'})
    else:
        findings.append({'header': 'Cross-Origin-Embedder-Policy', 'severity': 'ok', 'status': 'ok',
                         'value': coep, 'msg': 'OK', 'fix_location': 'nginx', 'fix_snippet': None})

    # Cross-Origin-Resource-Policy (CORP)
    corp = headers.get('cross-origin-resource-policy')
    if corp is None:
        findings.append({'header': 'Cross-Origin-Resource-Policy', 'severity': 'low', 'status': 'missing',
                         'value': None,
                         'msg': 'CORP fehlt — Ressourcen können Cross-Origin eingebettet werden. Empfohlen: same-origin',
                         'fix_location': 'nginx',
                         'fix_snippet': 'add_header Cross-Origin-Resource-Policy "same-origin" always;'})
    else:
        findings.append({'header': 'Cross-Origin-Resource-Policy', 'severity': 'ok', 'status': 'ok',
                         'value': corp, 'msg': 'OK', 'fix_location': 'nginx', 'fix_snippet': None})

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
                    'fix_location': 'webapp',
                    'fix_snippet': (
                        "# settings.py (django-cors-headers)\n"
                        "CORS_ALLOW_ALL_ORIGINS = False\n"
                        "CORS_ALLOWED_ORIGINS = ['https://example.com']\n"
                        "CORS_ALLOW_CREDENTIALS = True"
                    ),
                })
            else:
                findings.append({
                    'header': 'Access-Control-Allow-Origin',
                    'severity': 'medium',
                    'status': 'weak',
                    'value': acao,
                    'msg': 'CORS-Wildcard (*) erlaubt jeder Website Zugriff auf API-Antworten. Prüfen ob gewollt.',
                    'fix_location': 'webapp',
                    'fix_snippet': (
                        "# settings.py (django-cors-headers)\n"
                        "CORS_ALLOW_ALL_ORIGINS = False\n"
                        "CORS_ALLOWED_ORIGINS = ['https://example.com']"
                    ),
                })
        else:
            findings.append({
                'header': 'Access-Control-Allow-Origin',
                'severity': 'ok',
                'status': 'ok',
                'value': acao,
                'msg': f'CORS auf bestimmte Origin eingeschränkt: {acao}',
                'fix_location': 'webapp',
                'fix_snippet': None,
            })
    return findings


def _check_cookies(headers):
    """Parse Set-Cookie headers and check security flags."""
    # urllib gives us a single 'set-cookie' header (comma-joined for multiple)
    raw = headers.get('set-cookie', '')
    if not raw:
        return []
    findings = []
    # Split multiple cookies (naive but sufficient for most cases)
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
                          'msg': 'Secure-Flag fehlt — Cookie wird auch über HTTP gesendet.',
                          'fix_location': 'webapp',
                          'fix_snippet': 'SESSION_COOKIE_SECURE = True  # settings.py'})
        if not flags['httponly']:
            issues.append({'flag': 'HttpOnly', 'severity': 'medium',
                          'msg': 'HttpOnly-Flag fehlt — Cookie per JavaScript auslesbar (XSS-Risiko).',
                          'fix_location': 'webapp',
                          'fix_snippet': 'SESSION_COOKIE_HTTPONLY = True  # settings.py'})
        if not flags['samesite']:
            issues.append({'flag': 'SameSite', 'severity': 'medium',
                          'msg': 'SameSite-Flag fehlt — CSRF-Risiko erhöht.',
                          'fix_location': 'webapp',
                          'fix_snippet': "SESSION_COOKIE_SAMESITE = 'Lax'  # settings.py"})
        elif flags['samesite'] == 'none' and not flags['secure']:
            issues.append({'flag': 'SameSite=None', 'severity': 'high',
                          'msg': 'SameSite=None ohne Secure-Flag ist ungültig.',
                          'fix_location': 'webapp',
                          'fix_snippet': (
                              "SESSION_COOKIE_SAMESITE = 'Lax'  # settings.py\n"
                              "SESSION_COOKIE_SECURE = True"
                          )})
        findings.append({'name': name, 'issues': issues, 'flags': flags})
    return findings


def _check_http_redirect(hostname, port=80):
    """Check if HTTP (port 80) redirects to HTTPS."""
    # IPv6 addresses must be wrapped in brackets in URLs
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

    # Extract actual port from URL (do not hardcode 443/80)
    from urllib.parse import urlparse as _urlparse
    _parsed_url = _urlparse(target_url)
    _netloc_port = _parsed_url.port  # None if not explicit
    _tls_port = _netloc_port if _netloc_port else 443
    _http_port = 80  # HTTP redirect check always uses port 80

    # 1. TLS check
    if check_tls and is_https and hostname:
        result['tls'] = _check_tls(hostname, port=_tls_port)

    # 2. HTTP → HTTPS redirect check
    if hostname and is_https:
        result['http_redirect'] = _check_http_redirect(hostname, port=_http_port)

    # 3. Fetch the target URL for headers + cookies
    status, hdrs, body, final_url, err = _http_get(
        target_url, timeout=12, verify_ssl=False, follow_redirects=True
    )
    if err or status is None:
        result['error'] = f'Verbindungsfehler: {err or "keine Antwort"}'
        return result

    result['http_status'] = status
    result['final_url'] = final_url

    # 4. Security headers
    result['headers'] = _check_security_headers(hdrs)

    # 5. CORS
    result['cors'] = _check_cors(hdrs)

    # 6. Cookies
    result['cookies'] = _check_cookies(hdrs)

    # 7. Config / file leaks
    base_url = target_url.rstrip('/')
    # Use root base
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    scan_base = f'{parsed.scheme}://{parsed.netloc}'
    result['config_leaks'] = _check_config_leaks(scan_base, timeout=6)

    # 8. Summary
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

    # merge warning into medium for display
    sev_count['medium'] = sev_count.get('medium', 0) + sev_count.pop('warning', 0)
    result['summary'] = sev_count
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Public IP detection
# ──────────────────────────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────────────────────────
# Port scanner
# ──────────────────────────────────────────────────────────────────────────────

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

    Returns dict:
      {
        'host': str,
        'mode': str,
        'open_ports': [{'port': int, 'service': str, 'severity': str, 'note': str}, ...],
        'scanned': int,
        'error': str or None,
      }
    """
    import concurrent.futures

    result = {
        'host': host,
        'mode': mode,
        'open_ports': [],
        'scanned': 0,
        'error': None,
    }

    # Resolve host once
    try:
        resolved = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        # Pick first address
        addr_family = resolved[0][0]
        ip_addr = resolved[0][4][0]
    except socket.gaierror as e:
        result['error'] = f'DNS-Auflösung fehlgeschlagen: {e}'
        return result

    if mode == 'common':
        ports_to_scan = sorted(_PORT_INFO.keys())
    else:
        # Range scan — cap at 10000 ports for safety
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
    return result
