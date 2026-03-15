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
        found = False
        for line in lines:
            if line.strip().startswith('ALLOWED_HOSTS='):
                new_lines.append(f'ALLOWED_HOSTS={",".join(hosts)}\n')
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f'ALLOWED_HOSTS={",".join(hosts)}\n')
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
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return result


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
