"""
Admin-only views: audit log, security settings, manager settings,
.env editor, firewall, manager service control, and manager self-scans.
"""
import os
import re
import subprocess
import logging
from pathlib import Path

from django.shortcuts import render, redirect
from django.http import JsonResponse, Http404
from django.views.decorators.http import require_POST
from django.conf import settings

from ..models import AuditLog, SecuritySettings
from ..utils import (
    get_project, service_action,
    get_ufw_status, get_ufw_port_rules, ufw_toggle_port,
    sync_env_to_conf,
    run_manager_pip_audit, run_manager_deploy_check,
    run_http_security_scan,
)
from ._helpers import admin_required, operator_required, _check_project_access

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
            subprocess.run(['nginx', '-t'], check=True, capture_output=True)
            subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True, timeout=10)
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
                    ['bash', '-c', f'sleep 2 && systemctl restart {svc}'],
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
        'allowed_hosts': allowed_hosts,
        'csrf_origins':  csrf_origins,
        'error':         error,
        'success_msg':   success_msg,
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
                ['bash', '-c', f'sleep 2 && systemctl restart {svc}'],
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
        subprocess.run(['systemctl', 'daemon-reload'], check=False, timeout=10)
        subprocess.run(['systemctl', 'restart', 'djmanager'], check=False, timeout=15)
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
                    _set_manager_bind('0.0.0.0' if toggle == 'allow' else '127.0.0.1')
            else:
                error = msg

        elif action in ('allow', 'deny'):
            ok, msg = ufw_toggle_port(port, proto, action)
            if ok:
                AuditLog.log(request, f'Firewall: Port {port}/{proto} → {action}')
                success_msg = msg
                if port == '8888' and proto == 'tcp':
                    _set_manager_bind('0.0.0.0' if action == 'allow' else '127.0.0.1')
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
            ['bash', '-c', f'sleep 1 && systemctl {action} {svc}'],
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

    log_path = f'/tmp/{svc}_update_{int(time.time())}.log'
    try:
        with open(log_path, 'w') as logf:
            subprocess.Popen(
                ['bash', script], stdout=logf, stderr=logf,
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
    """Run pip-audit + manage.py check --deploy on the manager itself."""
    from django.contrib.auth.decorators import login_required  # noqa
    if not request.user.is_staff:
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)
    pip_results   = run_manager_pip_audit()
    deploy_issues = run_manager_deploy_check()
    return JsonResponse({
        'pip_audit':    pip_results,
        'deploy_check': deploy_issues,
    })


@login_required
def manager_http_scan(request):
    """HTTP/TLS security scan for the manager itself."""
    import ipaddress
    if not request.user.is_staff:
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
        result = run_http_security_scan(url, hostname=None, check_tls=False)
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
        result = run_http_security_scan(url, hostname=hostname, check_tls=check_tls)

    return JsonResponse(result)


# keep login_required importable inside this module
from django.contrib.auth.decorators import login_required  # noqa: E402
