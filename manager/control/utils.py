"""
Utility functions for reading the DjangoMultiDeploy project registry
and interacting with systemd services.
"""
import os
import json
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

    # Security: block shell metacharacters
    if _re.search(r'[;&|`$<>]', cmd_clean):
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
