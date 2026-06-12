"""
Admin-only views: audit log, security settings, manager settings,
.env editor, firewall, manager service control, and manager self-scans.
"""
import os
import re
import shutil
import subprocess
import logging
import tempfile
from pathlib import Path

_NGINX     = shutil.which('nginx')     or '/usr/sbin/nginx'
_SYSTEMCTL = shutil.which('systemctl') or '/usr/bin/systemctl'
_BASH      = shutil.which('bash')      or '/bin/bash'

from django.shortcuts import render, redirect
from django.http import JsonResponse, Http404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.conf import settings

from ..models import AuditLog, SecuritySettings
from ..utils import (
    get_project, service_action,
    get_ufw_status, get_ufw_port_rules, ufw_toggle_port,
    sync_env_to_conf, get_allowed_hosts,
    run_manager_pip_audit, run_manager_deploy_check, run_manager_bandit,
    run_manager_pip_outdated, run_manager_pip_upgrade,
    run_http_security_scan,
    run_nuclei_scan, nuclei_version_info, update_nuclei,
    run_zap_scan, zap_version_info, update_zap,
    run_bandit,
    patch_manager_nginx_config,
    start_job, get_job,
)
from ._helpers import admin_required, operator_required, _check_project_access, is_admin

logger = logging.getLogger('djmanager.views.admin')


# ── Audit Log ─────────────────────────────────────────────────────────────────

@admin_required
def audit_log_view(request):
    logs = AuditLog.objects.select_related('user').all()[:500]
    return render(request, 'control/audit_log.html', {'logs': logs})


# ── Security Settings ─────────────────────────────────────────────────────────

@admin_required
def security_settings_view(request):
    from ..middleware import invalidate_whitelist_cache
    sec = SecuritySettings.get()
    error = None
    success_msg = None

    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        if action == 'save':
            sec.ip_whitelist = request.POST.get('ip_whitelist', '').strip()
            sec.require_2fa  = bool(request.POST.get('require_2fa'))
            try:
                timeout = int(request.POST.get('session_timeout_hours', 8))
                sec.session_timeout_hours = max(0, min(timeout, 720))
            except ValueError:
                error = 'Ungültiger Timeout-Wert.'
            if not error:
                sec.save()
                invalidate_whitelist_cache()
                AuditLog.log(request, 'Sicherheitseinstellungen geändert')
                success_msg = 'Einstellungen gespeichert.'

    return render(request, 'control/security_settings.html', {
        'sec':         sec,
        'error':       error,
        'success_msg': success_msg,
    })


# ── Manager Settings (ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS) ──────────────────

@admin_required
def manager_settings_view(request):
    """Read/write ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS in manager .env."""
    env_path = Path(settings.BASE_DIR) / '.env'

    def _read_env():
        result = {}
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, _, v = line.partition('=')
                        v = v.strip()
                        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                            v = v[1:-1]
                        result[k.strip()] = v
        except OSError:
            pass
        return result

    def _write_env(d):
        with open(env_path, 'w') as f:
            for k, v in d.items():
                f.write(f'{k}={v}\n')
        os.chmod(env_path, 0o600)

    def _update_nginx_server_names(hosts):
        nginx_path = '/etc/nginx/sites-available/djmanager'
        if not os.path.exists(nginx_path):
            return
        try:
            with open(nginx_path) as f:
                content = f.read()
            new_names = ' '.join(hosts) + ' _' if hosts else '_'
            content = re.sub(r'server_name\s+[^;]+;', f'server_name {new_names};', content)
            with open(nginx_path, 'w') as f:
                f.write(content)
            subprocess.run([_NGINX, '-t'], check=True, capture_output=True)
            subprocess.run([_SYSTEMCTL, 'reload', 'nginx'], capture_output=True, timeout=10)
        except Exception:
            pass

    error = None
    success_msg = None

    if request.method == 'POST':
        action = request.POST.get('action', '')
        host   = request.POST.get('host', '').strip().strip('/')

        if host and not re.match(r'^[\w.\-:\[\]*]+$', host):
            error = 'Ungültiger Hostname — erlaubt: Buchstaben, Ziffern, .-:[]'
            host  = ''

        if not error and host:
            env       = _read_env()
            cur_hosts = [h.strip() for h in env.get('ALLOWED_HOSTS', '').split(',') if h.strip()]
            cur_csrf  = [c.strip() for c in env.get('CSRF_TRUSTED_ORIGINS', '').split(',') if c.strip()]

            def _schedule_restart():
                svc = getattr(settings, 'MANAGER_SERVICE_NAME', 'djmanager')
                subprocess.Popen(
                    [_BASH, '-c', f'sleep 2 && {_SYSTEMCTL} restart {svc}'],
                    close_fds=True, start_new_session=True,
                )

            if action == 'add':
                if host in cur_hosts:
                    error = f'"{host}" ist bereits eingetragen.'
                else:
                    cur_hosts.append(host)
                    for scheme in ('http', 'https'):
                        entry = f'{scheme}://{host}'
                        if entry not in cur_csrf:
                            cur_csrf.append(entry)
                    env['ALLOWED_HOSTS']        = ','.join(cur_hosts)
                    env['CSRF_TRUSTED_ORIGINS'] = ','.join(cur_csrf)
                    _write_env(env)
                    _update_nginx_server_names(cur_hosts)
                    _schedule_restart()
                    AuditLog.log(request, 'Manager: Host hinzugefügt', details=host)
                    success_msg = f'Host "{host}" hinzugefügt. Service wird neu gestartet…'

            elif action == 'remove':
                if host not in cur_hosts:
                    error = f'"{host}" nicht gefunden.'
                else:
                    cur_hosts = [h for h in cur_hosts if h != host]
                    host_esc  = re.escape(host)
                    cur_csrf  = [c for c in cur_csrf
                                 if not re.match(rf'^https?://{host_esc}(?:[:/]|$)', c)]
                    env['ALLOWED_HOSTS']        = ','.join(cur_hosts)
                    env['CSRF_TRUSTED_ORIGINS'] = ','.join(cur_csrf)
                    _write_env(env)
                    _update_nginx_server_names(cur_hosts)
                    _schedule_restart()
                    AuditLog.log(request, 'Manager: Host entfernt', details=host)
                    success_msg = f'Host "{host}" entfernt. Service wird neu gestartet…'

    env           = _read_env()
    allowed_hosts = [h.strip() for h in env.get('ALLOWED_HOSTS', '').split(',') if h.strip()]
    csrf_origins  = [c.strip() for c in env.get('CSRF_TRUSTED_ORIGINS', '').split(',') if c.strip()]

    return render(request, 'control/manager_settings.html', {
        'allowed_hosts':    allowed_hosts,
        'csrf_origins':     csrf_origins,
        'extern_scan_hosts': _manager_scan_hosts(),
        'error':            error,
        'success_msg':      success_msg,
    })


# ── Manager .env Editor ───────────────────────────────────────────────────────

@admin_required
def manager_env_view(request):
    """Read/write the manager's .env file directly via the web UI."""
    env_path = Path(settings.BASE_DIR) / '.env'
    error = None
    success_msg = None

    if request.method == 'POST':
        new_content = request.POST.get('env_content', '')
        try:
            with open(env_path, 'w') as f:
                f.write(new_content)
            os.chmod(env_path, 0o600)
            svc = getattr(settings, 'MANAGER_SERVICE_NAME', 'djmanager')
            subprocess.Popen(
                [_BASH, '-c', f'sleep 2 && {_SYSTEMCTL} restart {svc}'],
                close_fds=True, start_new_session=True,
            )
            AuditLog.log(request, 'Manager: .env bearbeitet')
            success_msg = 'Die .env-Datei wurde gespeichert. Service wird neu gestartet…'
        except OSError as e:
            error = f'Fehler beim Schreiben: {e}'

    try:
        with open(env_path) as f:
            env_content = f.read()
    except OSError:
        env_content = ''

    return render(request, 'control/env_editor.html', {
        'page_title':  'Manager — .env bearbeiten',
        'env_content': env_content,
        'back_url':    'manager_settings',
        'back_label':  '← Manager-Einstellungen',
        'save_url':    'manager_env',
        'error':       error,
        'success_msg': success_msg,
    })


# ── Project .env Editor ───────────────────────────────────────────────────────

@admin_required
def project_env_view(request, name):
    """Read/write a project's .env file directly via the web UI."""
    if not _check_project_access(request.user, name):
        return render(request, 'control/403.html', status=403)

    conf = get_project(name)
    if not conf:
        raise Http404(f'Projekt "{name}" nicht gefunden.')

    appdir   = conf.get('APPDIR', f'/srv/{name}')
    env_path = Path(appdir) / '.env'
    error    = None
    success_msg = None

    if request.method == 'POST':
        new_content = request.POST.get('env_content', '')
        try:
            with open(env_path, 'w') as f:
                f.write(new_content)
            os.chmod(env_path, 0o600)
            sync_env_to_conf(name, new_content)
            ok, out = service_action(name, 'restart')
            if ok:
                success_msg = f'Gespeichert und {name} neu gestartet.'
            else:
                success_msg = f'Gespeichert. Service-Neustart: {out[:200]}'
            AuditLog.log(request, '.env bearbeitet', project=name)
        except OSError as e:
            error = f'Fehler beim Schreiben: {e}'

    try:
        with open(env_path) as f:
            env_content = f.read()
    except OSError:
        env_content = ''

    return render(request, 'control/env_editor.html', {
        'page_title':     f'{name} — .env bearbeiten',
        'env_content':    env_content,
        'back_url':       'project_detail',
        'back_url_kwarg': name,
        'back_label':     f'← {name}',
        'save_url':       'project_env',
        'save_url_kwarg': name,
        'error':          error,
        'success_msg':    success_msg,
    })


# ── Firewall (ufw) ────────────────────────────────────────────────────────────

_SERVICE_FILE = '/etc/systemd/system/djmanager.service'


def _set_manager_bind(host):
    """Switch Gunicorn bind address in djmanager.service and restart the service."""
    try:
        with open(_SERVICE_FILE) as fh:
            content = fh.read()
        new_content = re.sub(
            r'--bind\s+[\d\.]+:(\d+)',
            lambda m: f'--bind {host}:{m.group(1)}',
            content,
        )
        if new_content == content:
            return
        with open(_SERVICE_FILE, 'w') as fh:
            fh.write(new_content)
        subprocess.run([_SYSTEMCTL, 'daemon-reload'], check=False, timeout=10)
        subprocess.run([_SYSTEMCTL, 'restart', 'djmanager'], check=False, timeout=15)
    except Exception:
        pass


@admin_required
def firewall_view(request):
    error = None
    success_msg = None

    if request.method == 'POST':
        action = request.POST.get('action', '')
        port   = request.POST.get('port', '').strip()
        proto  = request.POST.get('proto', 'tcp').strip()

        if action == 'add_custom':
            toggle = request.POST.get('toggle', 'allow')
            ok, msg = ufw_toggle_port(port, proto, toggle)
            if ok:
                AuditLog.log(request, f'Firewall: Port {port}/{proto} → {toggle}')
                success_msg = msg
                if port == '8888' and proto == 'tcp':
                    _set_manager_bind('0.0.0.0' if toggle == 'allow' else '127.0.0.1')  # nosec B104
            else:
                error = msg

        elif action in ('allow', 'deny'):
            ok, msg = ufw_toggle_port(port, proto, action)
            if ok:
                AuditLog.log(request, f'Firewall: Port {port}/{proto} → {action}')
                success_msg = msg
                if port == '8888' and proto == 'tcp':
                    _set_manager_bind('0.0.0.0' if action == 'allow' else '127.0.0.1')  # nosec B104
            else:
                error = msg

    ufw_status  = get_ufw_status()
    port_rules  = get_ufw_port_rules()
    known_ports = [
        ('HTTP',       '80',   'tcp'),
        ('HTTPS',      '443',  'tcp'),
        ('SSH',        '22',   'tcp'),
        ('Manager',    '8888', 'tcp'),
        ('PostgreSQL', '5432', 'tcp'),
        ('MySQL',      '3306', 'tcp'),
    ]
    return render(request, 'control/firewall.html', {
        'ufw_status':  ufw_status,
        'port_rules':  port_rules,
        'known_ports': known_ports,
        'error':       error,
        'success_msg': success_msg,
    })


# ── Manager Service Control ───────────────────────────────────────────────────

@require_POST
@operator_required
def manager_action(request):
    """Start / stop / restart the djmanager service itself."""
    action = request.POST.get('action', '')
    if action not in ('start', 'stop', 'restart'):
        return JsonResponse({'ok': False, 'message': 'Ungültige Aktion'})
    svc = getattr(settings, 'MANAGER_SERVICE_NAME', 'djmanager')
    if action in ('restart', 'stop'):
        subprocess.Popen(
            [_BASH, '-c', f'sleep 1 && {_SYSTEMCTL} {action} {svc}'],
            close_fds=True, start_new_session=True,
        )
        AuditLog.log(request, f'Manager-Service {action}', success=True)
        return JsonResponse({'ok': True, 'message': f'Service wird {action}ed…'})
    from ..utils import service_action as _service_action
    ok, output = _service_action(svc, action)
    AuditLog.log(request, f'Manager-Service {action}', success=ok)
    return JsonResponse({'ok': ok, 'message': output or ('OK' if ok else 'Fehler')})


@require_POST
@admin_required
def manager_update(request):
    """Run djmanager_update.sh asynchronously (git pull + service restart)."""
    import time
    svc    = getattr(settings, 'MANAGER_SERVICE_NAME', 'djmanager')
    script = f'/usr/local/bin/{svc}_update.sh'
    if not os.path.exists(script):
        return JsonResponse({'ok': False, 'output': f'Update-Skript nicht gefunden: {script}'})

    _patch_update_script_rsync_fallback(script)

    log_path = os.path.join(tempfile.gettempdir(), f'{svc}_update_{int(time.time())}.log')
    try:
        with open(log_path, 'w') as logf:
            subprocess.Popen(
                [_BASH, script], stdout=logf, stderr=logf,
                close_fds=True, start_new_session=True,
            )
        AuditLog.log(request, 'Manager-Update gestartet', success=True)
        return JsonResponse({
            'ok':     True,
            'output': 'Update gestartet. Seite wird in ~30 Sekunden neu geladen.',
            'log':    log_path,
        })
    except Exception as e:
        return JsonResponse({'ok': False, 'output': str(e)})


def _patch_update_script_rsync_fallback(script_path):
    """
    Patcht das bestehende djmanager_update.sh einmalig (idempotent):
    1. pull.rebase false + --ff-only Fallback
    2. git stash vor git pull
    3. rsync → cp (kein rsync nötig)
    """
    try:
        with open(script_path) as f:
            content = f.read()

        changed = False

        if 'pull.rebase false' not in content:
            old = '  git config --global --add safe.directory "$SCRIPT_DIR" 2>/dev/null || true\n'
            new = (
                '  git config --global --add safe.directory "$SCRIPT_DIR" 2>/dev/null || true\n'
                '  git config --global pull.rebase false 2>/dev/null || true\n'
            )
            if old in content:
                content = content.replace(old, new, 1)
                changed = True

        if 'git stash' not in content:
            old = (
                '  git config --global pull.rebase false 2>/dev/null || true\n'
                '  if [ -f "$GITHUB_DEPLOY_KEY" ]; then'
            )
            new = (
                '  git config --global pull.rebase false 2>/dev/null || true\n'
                '  git -C "$SCRIPT_DIR" stash --quiet 2>/dev/null || true\n'
                '  if [ -f "$GITHUB_DEPLOY_KEY" ]; then'
            )
            if old in content:
                content = content.replace(old, new, 1)
                changed = True

        if 'rsync' in content:
            old = (
                '  rsync -a \\\n'
                '    --exclude=\'.env\' \\\n'
                '    --exclude=\'db.sqlite3\' \\\n'
                '    --exclude=\'venv/\' \\\n'
                '    --exclude=\'staticfiles/\' \\\n'
                '    "$SCRIPT_DIR/manager/" "$MANAGER_DIR/"'
            )
            new = (
                '  find "$SCRIPT_DIR/manager" -mindepth 1 -maxdepth 1 | while read -r _item; do\n'
                '    _base="$(basename "$_item")"\n'
                '    case "$_base" in\n'
                '      .env|db.sqlite3|venv|staticfiles) continue ;;\n'
                '    esac\n'
                '    cp -a "$_item" "$MANAGER_DIR/"\n'
                '  done'
            )
            if old in content:
                content = content.replace(old, new, 1)
                changed = True

        if changed:
            with open(script_path, 'w') as f:
                f.write(content)
    except Exception:
        pass


# ── Manager self-scans ────────────────────────────────────────────────────────

@login_required
def manager_security_scan(request):
    """Run pip-audit + manage.py check --deploy + bandit on the manager itself."""
    if not is_admin(request.user):
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)

    def _run():
        return {
            'pip_audit':    run_manager_pip_audit(),
            'deploy_check': run_manager_deploy_check(),
            'bandit':       run_manager_bandit(),
        }

    job_id = start_job(_run)
    return JsonResponse({'job_id': job_id, 'status': 'running'})


@login_required
def manager_pip_outdated(request):
    """List outdated packages in the manager venv."""
    if not is_admin(request.user):
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)
    result = run_manager_pip_outdated()
    return JsonResponse(result)


@require_POST
@admin_required
def manager_pip_upgrade_view(request):
    """Upgrade a single package in the manager venv."""
    package = request.POST.get('package', '').strip()
    result = run_manager_pip_upgrade(package)
    AuditLog.log(request, f'Manager pip upgrade: {package}', success=result['ok'])
    return JsonResponse(result)


@login_required
def manager_http_scan(request):
    """HTTP/TLS security scan for the manager itself — runs in background job."""
    import ipaddress
    if not is_admin(request.user):
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)

    target = request.GET.get('target', 'internal')

    manager_port       = getattr(settings, 'MANAGER_PORT', None)
    manager_nginx_port = getattr(settings, 'MANAGER_NGINX_PORT', None)
    mgr_conf_path = '/etc/django-servers.d/djmanager.conf'
    if os.path.exists(mgr_conf_path) and (not manager_port or not manager_nginx_port):
        try:
            with open(mgr_conf_path) as _f:
                for _line in _f:
                    _line = _line.strip()
                    m = re.match(r'GUNICORN_PORT=(\d+)', _line)
                    if m and not manager_port:
                        manager_port = m.group(1)
                    m = re.match(r'NGINX_PORT=(\d+)', _line)
                    if m and not manager_nginx_port:
                        manager_nginx_port = m.group(1)
        except OSError:
            pass

    if target == 'internal':
        if not manager_port:
            host = request.get_host().split(':')[0]
            url  = f'http://{host}/'
        else:
            url = f'http://127.0.0.1:{manager_port}/'
        def _run():
            return run_http_security_scan(url, hostname=None, check_tls=False)
    else:
        hostname = target
        try:
            addr     = ipaddress.ip_address(hostname)
            url_host = f'[{hostname}]' if isinstance(addr, ipaddress.IPv6Address) else hostname
        except ValueError:
            url_host = hostname
        nginx_port = str(manager_nginx_port or '443').strip()
        if nginx_port == '80':
            url       = f'http://{url_host}/'
            check_tls = False
        else:
            url       = f'https://{url_host}/'
            check_tls = True
        _local_port = (
            int(nginx_port)
            if nginx_port not in ('80', '443') and manager_nginx_port
            else None
        )
        def _run():
            return run_http_security_scan(url, hostname=hostname, check_tls=check_tls,
                                          local_port=_local_port)

    job_id = start_job(_run)
    return JsonResponse({'job_id': job_id, 'status': 'running'})


# ── Manager Nuclei / ZAP scans ────────────────────────────────────────────────

def _manager_scan_hosts():
    """Return real domain names from manager ALLOWED_HOSTS (no wildcards/IPs/localhost)."""
    import ipaddress as _ipa
    raw = getattr(settings, 'ALLOWED_HOSTS', [])
    result = []
    for h in raw:
        h = h.strip().lower()
        if not h or h in ('*', 'localhost', '127.0.0.1', '::1'):
            continue
        if h.startswith('.') or '*' in h:
            continue
        try:
            _ipa.ip_address(h)
            continue  # skip plain IPs
        except ValueError:
            pass
        result.append(h)
    return result


# ── Background job poll (shared by all async scan/update views) ───────────────

@login_required
def job_poll_view(request, job_id):
    """Return current status of a background job."""
    if not is_admin(request.user):
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)
    state = get_job(job_id)
    if state is None:
        return JsonResponse({'error': 'Job nicht gefunden'}, status=404)
    return JsonResponse(state)


# ── Manager Nuclei ────────────────────────────────────────────────────────────

@login_required
def manager_nuclei_scan(request):
    if not is_admin(request.user):
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)
    hostname = request.GET.get('target', '').strip().lower()
    if not hostname:
        return JsonResponse({'error': 'Kein Ziel angegeben'}, status=400)
    import ipaddress as _ipa
    try:
        addr = _ipa.ip_address(hostname)
        url_host = f'[{hostname}]' if isinstance(addr, _ipa.IPv6Address) else hostname
    except ValueError:
        url_host = hostname
    target_url = f'https://{url_host}'

    def _run():
        result = run_nuclei_scan(target_url)
        AuditLog.log(request, f'Manager nuclei scan: {hostname}', success=result['ok'])
        return result

    job_id = start_job(_run)
    return JsonResponse({'job_id': job_id, 'status': 'running'})


@login_required
def manager_nuclei_version(request):
    if not is_admin(request.user):
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)
    return JsonResponse(nuclei_version_info())


@require_POST
@admin_required
def manager_nuclei_update(request):
    def _run():
        result = update_nuclei()
        AuditLog.log(request, f'Manager nuclei update: {result.get("version","?")}', success=result['ok'])
        return result

    job_id = start_job(_run)
    return JsonResponse({'job_id': job_id, 'status': 'running'})


# ── Manager ZAP ───────────────────────────────────────────────────────────────

@login_required
def manager_zap_scan(request):
    if not is_admin(request.user):
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)
    import ipaddress as _ipa
    hostname  = request.GET.get('target', request.POST.get('target', '')).strip().lower()
    scan_type = request.GET.get('type', request.POST.get('type', 'baseline'))
    if scan_type not in ('baseline', 'full'):
        scan_type = 'baseline'
    if not hostname:
        return JsonResponse({'error': 'Kein Ziel angegeben'}, status=400)
    try:
        addr = _ipa.ip_address(hostname)
        url_host = f'[{hostname}]' if isinstance(addr, _ipa.IPv6Address) else hostname
    except ValueError:
        url_host = hostname
    auth = None
    if request.method == 'POST':
        login_url = request.POST.get('login_url', '').strip()
        username  = request.POST.get('auth_username', '').strip()
        password  = request.POST.get('auth_password', '').strip()
        if login_url and username and password:
            auth = {
                'login_url':           login_url,
                'username_field':      request.POST.get('username_field', 'username').strip(),
                'password_field':      request.POST.get('password_field', 'password').strip(),
                'username':            username,
                'password':            password,
                'logged_in_indicator': request.POST.get('logged_in_indicator', '').strip(),
            }
    target_url = f'https://{url_host}'
    suffix = ' (auth)' if auth else ''

    def _run():
        result = run_zap_scan(target_url, scan_type=scan_type, auth=auth)
        AuditLog.log(request, f'Manager ZAP {scan_type}{suffix}: {hostname}', success=result['ok'])
        return result

    job_id = start_job(_run)
    return JsonResponse({'job_id': job_id, 'status': 'running'})



@login_required
def manager_zap_version(request):
    if not is_admin(request.user):
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)
    return JsonResponse(zap_version_info())


@require_POST
@admin_required
def manager_zap_update(request):
    def _run():
        result = update_zap()
        AuditLog.log(request, f'Manager ZAP update: {result.get("version","?")}', success=result['ok'])
        return result

    job_id = start_job(_run)
    return JsonResponse({'job_id': job_id, 'status': 'running'})


@login_required
def manager_config_export(request):
    """Return manager .env (secrets masked) + nginx config for scan report. Admin only."""
    if not is_admin(request.user):
        return JsonResponse({'error': 'Nur Admins'}, status=403)

    from ..utils.secrets_mask import mask_secrets as mask

    env_path = Path(settings.BASE_DIR) / '.env'
    try:
        env_content = mask(env_path.read_text())
        env_error = None
    except OSError as e:
        env_content = ''
        env_error = str(e)

    nginx_path = '/etc/nginx/sites-available/djmanager'
    try:
        nginx_content = mask(open(nginx_path).read())
        nginx_error = None
    except OSError as e:
        nginx_content = ''
        nginx_error = str(e)

    return JsonResponse({
        'env': env_content,
        'env_error': env_error,
        'nginx': nginx_content,
        'nginx_error': nginx_error,
    })


@login_required
def manager_nginx_config(request):
    """Read (GET) or save (POST) the nginx config for the manager itself."""
    if not is_admin(request.user):
        return JsonResponse({'error': 'Nur Admins'}, status=403)

    from ..utils.config import get_project_nginx_config, save_project_nginx_config, _CSP_DEFAULT

    if request.method == 'POST':
        content = request.POST.get('content', '')
        if not content.strip():
            return JsonResponse({'ok': False, 'error': 'Inhalt darf nicht leer sein'})
        ok, msg = save_project_nginx_config('djmanager', content)
        if ok:
            AuditLog.log(request, 'Manager nginx config gespeichert', success=True)
        return JsonResponse({'ok': ok, 'message': msg, 'error': msg if not ok else ''})

    content, error = get_project_nginx_config('djmanager')
    return JsonResponse({'content': content, 'error': error, 'csp_default': _CSP_DEFAULT})


@login_required
@require_POST
def manager_nginx_patch(request):
    """Auto-patch the manager nginx config: remove duplicate headers, add /jobs/ location."""
    if not is_admin(request.user):
        return JsonResponse({'ok': False, 'error': 'Nur Admins'}, status=403)
    ok, msg = patch_manager_nginx_config()
    if ok:
        AuditLog.log(request, 'Manager nginx config auto-gepatcht', success=True, details=msg)
    return JsonResponse({'ok': ok, 'message': msg, 'error': msg if not ok else ''})
