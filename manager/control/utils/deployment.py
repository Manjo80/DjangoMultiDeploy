"""
Deployment utility functions for DjangoMultiDeploy.
"""
import os
import subprocess
import shlex
from django.conf import settings

from .registry import get_project
from .deploy_keys import KEYS_DIR


def _patch_project_update_script(script_path):
    """
    Idempotently patches an existing project update script:
    1. git stash before git pull  (fixes "please commit your changes" abort)
    2. load_glossary after collectstatic  (optional management command)
    """
    try:
        with open(script_path) as f:
            content = f.read()

        changed = False

        # ── Patch 1: git stash ───────────────────────────────────────────────
        if 'git stash' not in content:
            # Ensure pull.rebase false is present (older scripts may lack it)
            if 'pull.rebase false' not in content:
                old = '  git config --global --add safe.directory "$APPDIR" 2>/dev/null || true\n'
                new = (
                    '  git config --global --add safe.directory "$APPDIR" 2>/dev/null || true\n'
                    '  git config --global pull.rebase false 2>/dev/null || true\n'
                )
                if old in content:
                    content = content.replace(old, new, 1)
                    changed = True

            old = '  git config --global pull.rebase false 2>/dev/null || true\n'
            new = (
                '  git config --global pull.rebase false 2>/dev/null || true\n'
                '  git -C "$APPDIR" stash --quiet 2>/dev/null \\\n'
                '    || git -C "$APPDIR" checkout -- . 2>/dev/null \\\n'
                '    || true\n'
            )
            if old in content:
                content = content.replace(old, new, 1)
                changed = True

        # ── Patch 2: load_glossary after collectstatic ───────────────────────
        if 'load_glossary' not in content:
            old = (
                'python manage.py collectstatic --noinput"\n'
                '\n'
                '# Service neustarten'
            )
            new = (
                'python manage.py collectstatic --noinput"\n'
                '\n'
                '# Glossar neu einlesen (optional — wird übersprungen wenn Command nicht vorhanden)\n'
                'echo "📖 Lade Glossar (falls vorhanden)..."\n'
                '_gout=$(su - "$APPUSER" -s /bin/bash -c'
                ' "cd $APPDIR && source .venv/bin/activate && python manage.py load_glossary 2>&1") \\\n'
                '  && echo "✅ Glossar geladen" \\\n'
                '  || { echo "$_gout" | grep -q "Unknown command\\|No such command" \\\n'
                '       && echo "⏭️  load_glossary nicht vorhanden (übersprungen)" \\\n'
                '       || echo "⚠️  Glossar laden fehlgeschlagen: $_gout"; }\n'
                '\n'
                '# Service neustarten'
            )
            if old in content:
                content = content.replace(old, new, 1)
                changed = True

        if changed:
            with open(script_path, 'w') as f:
                f.write(content)
    except OSError:
        pass


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

    # Auto-patch: add git stash if missing from older installed scripts.
    # Without stash, git pull aborts when local files were modified after install.
    _patch_project_update_script(script)

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


def reset_project(name):
    """
    Full reset: backup → stop service → drop+recreate DB → git fetch+reset --hard
    → pip install → migrate → collectstatic → restart service.
    Preserves: .env, .venv, media/, staticfiles/
    Returns (ok, output).
    """
    conf = get_project(name)
    if not conf:
        return False, 'Projekt nicht gefunden'

    appdir  = conf.get('APPDIR', f'/srv/{name}')
    appuser = conf.get('APPUSER', name)
    dbtype  = conf.get('DBTYPE', '')
    dbname  = conf.get('DBNAME', '')
    log = []
    ok  = True

    def _run(*cmd, timeout=60):
        try:
            r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout + r.stderr).strip()
        except Exception as e:
            return 1, str(e)

    def _run_as(cmd, timeout=300):
        r = subprocess.run(
            ['su', '-', appuser, '-s', '/bin/bash', '-c', cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, (r.stdout + r.stderr).strip()[-800:]

    # 1. Backup
    log.append('💾 Erstelle Backup vor Reset...')
    backup_script = f'/usr/local/bin/{name}_backup.sh'
    if os.path.exists(backup_script):
        rc, out = _run(backup_script, timeout=120)
        log.append('✅ Backup erstellt' if rc == 0 else '⚠️ Backup fehlgeschlagen — Reset wird fortgesetzt')
    else:
        log.append('⏭️ Kein Backup-Script gefunden')

    # 2. Stop service
    _run('systemctl', 'stop', name)
    log.append(f'🔴 Service {name} gestoppt')

    # 3. Drop + recreate database
    if dbtype and dbname:
        log.append(f'🗄️ Setze Datenbank zurück ({dbtype} / {dbname})...')
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
            _run('su', '-s', '/bin/bash', 'postgres', '-c',
                 f'psql -c "CREATE DATABASE \\"{dbname}\\";"')
            if dbuser:
                _run('su', '-s', '/bin/bash', 'postgres', '-c',
                     f'psql -c "GRANT ALL PRIVILEGES ON DATABASE \\"{dbname}\\" TO \\"{dbuser}\\";"')
            log.append(f'✅ PostgreSQL-Datenbank {dbname} neu erstellt')
        elif dbtype == 'mysql':
            _run('mysql', '-u', 'root', '-e', f'DROP DATABASE IF EXISTS `{dbname}`;')
            _run('mysql', '-u', 'root', '-e',
                 f'CREATE DATABASE `{dbname}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;')
            if dbuser:
                _run('mysql', '-u', 'root', '-e',
                     f"GRANT ALL ON `{dbname}`.* TO '{dbuser}'@'localhost'; FLUSH PRIVILEGES;")
            log.append(f'✅ MySQL-Datenbank {dbname} neu erstellt')
    else:
        log.append('⏭️ Keine Datenbank konfiguriert — überspringe DB-Reset')

    # 4. Git fetch + reset --hard (keeps .env, .venv, media, staticfiles)
    if os.path.isdir(os.path.join(appdir, '.git')):
        log.append('📥 Git: Lade neueste Version vom Remote...')
        deploy_key_id = conf.get('DEPLOY_KEY_ID', '').strip()
        key_path = ''
        if deploy_key_id:
            candidate = os.path.join(KEYS_DIR, f'{deploy_key_id}_ed25519')
            if os.path.exists(candidate):
                key_path = candidate
        ssh_env = (
            f'GIT_SSH_COMMAND="ssh -i {key_path} -o IdentitiesOnly=yes -o ConnectTimeout=30" '
            if key_path else ''
        )
        _run('git', 'config', '--global', '--add', 'safe.directory', appdir)
        _run('git', 'config', '--global', 'pull.rebase', 'false')
        rc, out = _run('bash', '-c', f'{ssh_env}git -C {appdir} fetch --all --quiet', timeout=60)
        if rc != 0:
            log.append(f'❌ git fetch fehlgeschlagen:\n{out}')
            ok = False
        else:
            rc2, out2 = _run('bash', '-c',
                f'branch=$(git -C {appdir} rev-parse --abbrev-ref HEAD 2>/dev/null || echo main); '
                f'git -C {appdir} reset --hard origin/$branch 2>/dev/null '
                f'|| git -C {appdir} reset --hard origin/main 2>/dev/null '
                f'|| git -C {appdir} reset --hard origin/master',
                timeout=30)
            log.append('✅ Git: neueste Version geladen' if rc2 == 0 else f'⚠️ git reset: {out2}')
            _run('bash', '-c',
                f'git -C {appdir} clean -fd '
                f'--exclude=.env --exclude=.venv --exclude=media --exclude=staticfiles '
                f'2>/dev/null || true',
                timeout=30)
    else:
        log.append('⏭️ Kein Git-Repository gefunden — überspringe git reset')

    if not ok:
        _run('systemctl', 'start', name)
        log.append(f'⚠️ Reset abgebrochen — Service {name} neu gestartet')
        return ok, '\n'.join(log)

    venv_activate = os.path.join(appdir, '.venv', 'bin', 'activate')
    req_file      = os.path.join(appdir, 'requirements.txt')

    # 5. pip install
    if os.path.isfile(req_file):
        log.append('📦 Installiere Python-Abhängigkeiten...')
        rc, out = _run_as(
            f'source {venv_activate} && pip install --no-cache-dir --prefer-binary -r {req_file}',
            timeout=300,
        )
        log.append(f'{"✅" if rc == 0 else "❌"} pip install\n{out[-400:]}')
        if rc != 0:
            ok = False

    # 6. migrate (fresh DB)
    log.append('📊 Führe Migrationen aus (neue Datenbank)...')
    rc, out = _run_as(
        f'cd {appdir} && source {venv_activate} && python manage.py migrate --noinput',
        timeout=180,
    )
    log.append(f'{"✅" if rc == 0 else "❌"} migrate\n{out[-500:]}')
    if rc != 0:
        ok = False

    # 7. collectstatic
    rc, _ = _run_as(
        f'cd {appdir} && source {venv_activate} && python manage.py collectstatic --noinput',
        timeout=60,
    )
    log.append(f'{"✅" if rc == 0 else "⚠️"} collectstatic')

    # 8. load_glossary (optional — silently skipped if command doesn't exist)
    rc, out = _run_as(
        f'cd {appdir} && source {venv_activate} && python manage.py load_glossary 2>&1',
        timeout=120,
    )
    if rc == 0:
        log.append('✅ Glossar geladen')
    elif 'Unknown command' in out or 'No such command' in out:
        log.append('⏭️ load_glossary nicht vorhanden (übersprungen)')
    else:
        log.append(f'⚠️ Glossar laden fehlgeschlagen:\n{out[-300:]}')

    # 9. Restart service
    _run('systemctl', 'start', name)
    log.append(f'✅ Service {name} neu gestartet')
    log.append('✅ Reset abgeschlossen')
    return ok, '\n'.join(log)


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
