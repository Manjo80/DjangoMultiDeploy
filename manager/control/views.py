"""
DjangoMultiDeploy Manager — Views
"""
import os
import json
import time
import uuid
import subprocess
from pathlib import Path

from django.shortcuts import render, redirect
from django.http import (
    StreamingHttpResponse, HttpResponse, JsonResponse,
    Http404
)
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from functools import wraps


def admin_required(view_func):
    """Only staff users may access this view."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f'/login/?next={request.path}')
        if not request.user.is_staff:
            return render(request, 'control/403.html', status=403)
        return view_func(request, *args, **kwargs)
    return wrapper

from .utils import (
    get_all_projects, get_project, get_service_status,
    service_action, get_journal_logs, get_nginx_log,
    list_backups, run_update, run_backup, get_ssh_key, start_install,
    get_global_deploy_key, get_allowed_hosts, get_nginx_server_names,
    update_allowed_hosts,
)


# ──────────────────────────────────────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────────────────────────────────────

def dashboard(request):
    projects = get_all_projects()
    return render(request, 'control/dashboard.html', {'projects': projects})


# ──────────────────────────────────────────────────────────────────────────────
# Install wizard
# ──────────────────────────────────────────────────────────────────────────────

@admin_required
def install_form(request):
    import socket, subprocess as _sp
    # Nächsten freien Port ermitteln
    used_ports = {p.get('GUNICORN_PORT') for p in get_all_projects() if p.get('GUNICORN_PORT')}
    next_port = next((str(p) for p in range(8000, 9000) if str(p) not in used_ports), '8000')
    # Alle echten IPs via `hostname -I` (ohne loopback, ohne Manager-Hostname)
    try:
        raw = _sp.check_output(['hostname', '-I'], text=True).strip()
        all_ips = [ip for ip in raw.split() if not ip.startswith('127.')]
    except Exception:
        all_ips = []
    if not all_ips:
        try:
            all_ips = [socket.gethostbyname(socket.gethostname())]
        except Exception:
            all_ips = []
    allowed_suggestion = ','.join(all_ips) if all_ips else ''
    defaults = {
        'next_port': next_port,
        'server_ips': all_ips,
        'allowed_hosts_suggestion': allowed_suggestion,
    }
    return render(request, 'control/install_form.html', defaults)


@admin_required
@require_POST
def install_run(request):
    """Receive install form, launch background install, redirect to progress page."""
    data = request.POST

    # Build env-var dict for NONINTERACTIVE mode
    params = {
        'PROJECTNAME':        data.get('projectname', '').strip(),
        'APPUSER':            data.get('appuser', '').strip(),
        'MODESEL':            data.get('modesel', '1'),
        'GITHUB_REPO_URL':    data.get('github_repo_url', '').strip(),
        'GUNICORN_PORT':      data.get('gunicorn_port', '').strip(),
        'GUNICORN_WORKERS':   data.get('gunicorn_workers', '').strip(),
        'ALLOWED_HOSTS':      data.get('allowed_hosts', '').strip(),
        'DBTYPE_SEL':         data.get('dbtype_sel', '1'),
        'DBMODE':             data.get('dbmode', '2'),
        'DBNAME':             data.get('dbname', '').strip(),
        'DBUSER':             data.get('dbuser', '').strip(),
        'DBPASS':             data.get('dbpass', '').strip(),
        'DBHOST':             data.get('dbhost', 'localhost').strip(),
        'DBPORT':             data.get('dbport', '5432').strip(),
        'APPUSER_PASS':        data.get('appuser_pass', '').strip(),
        'DJANGO_ADMIN_USER':  data.get('django_admin_user', 'admin').strip(),
        'DJANGO_ADMIN_EMAIL': data.get('django_admin_email', 'admin@localhost').strip(),
        'DJANGO_ADMIN_PASS':  data.get('django_admin_pass', '').strip(),
        'DJKEY':              data.get('djkey', '').strip(),
        'LANGUAGE_CODE':      data.get('language_code', 'de-de').strip(),
        'TIME_ZONE':          data.get('time_zone', 'Europe/Berlin').strip(),
        'EMAIL_HOST':         data.get('email_host', '').strip(),
        'EMAIL_PORT':         data.get('email_port', '587').strip(),
        'EMAIL_HOST_USER':    data.get('email_host_user', '').strip(),
        'EMAIL_HOST_PASSWORD':data.get('email_host_password', '').strip(),
        'EMAIL_USE_TLS':      'True' if data.get('email_use_tls') else 'False',
        'DEFAULT_FROM_EMAIL': data.get('default_from_email', '').strip(),
        '_BACKUP_TIME':       data.get('backup_time', '02:00').strip(),
        '_INSTALL_SEL':       '1',  # always install project from web UI
        'UPGRADE':            'n',  # skip system upgrade prompt
        'INSTALL_FAIL2BAN':   'n',  # optional; can add checkbox later
    }

    # Remove empty values so defaults kick in
    params = {k: v for k, v in params.items() if v != ''}

    project = params.get('PROJECTNAME', '')
    if not project:
        return render(request, 'control/install_form.html',
                      {'error': 'Projektname darf nicht leer sein.'})

    run_id = str(uuid.uuid4())[:8]
    log_dir = settings.INSTALL_LOG_DIR
    log_name = f'{project}_{run_id}.log'
    log_path = os.path.join(log_dir, log_name)

    env = os.environ.copy()
    env.update(params)
    env['NONINTERACTIVE'] = 'true'

    # setsid: startet den Installer in einer neuen Prozessgruppe.
    # Wenn Django/Manager wegen OOM gekillt wird, läuft die Installation weiter.
    # start_new_session=True ersetzt das externe setsid-Kommando (Python 3.2+)
    os.makedirs(log_dir, exist_ok=True)
    with open(log_path, 'w') as log_f:
        subprocess.Popen(
            ['bash', settings.INSTALL_SCRIPT],
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,   # = setsid: eigene Prozessgruppe / Session
            close_fds=True,
        )

    return redirect('install_progress', project=project, run_id=run_id)


@login_required
def install_progress(request, project, run_id):
    log_name = f'{project}_{run_id}.log'
    return render(request, 'control/install_progress.html', {
        'project': project,
        'run_id': run_id,
        'log_name': log_name,
    })


@login_required
def install_poll(request, log_name):
    """Polling endpoint: returns new log lines since offset as JSON.
    Replaces SSE stream — short-lived requests, no EIO on LXC overlayfs."""
    log_path = os.path.join(settings.INSTALL_LOG_DIR, log_name)
    try:
        offset = int(request.GET.get('offset', 0))
    except (ValueError, TypeError):
        offset = 0

    if not os.path.exists(log_path):
        return JsonResponse({'lines': [], 'offset': 0, 'done': False, 'waiting': True})

    lines = []
    new_offset = offset
    done = False
    try:
        with open(log_path, 'rb') as f:
            f.seek(offset)
            chunk = f.read(65536)   # max 64 KB pro Poll
            new_offset = offset + len(chunk)
        if chunk:
            text = chunk.decode('utf-8', errors='replace')
            lines = [l.rstrip('\r') for l in text.splitlines()]
        # Abgeschlossen?
        done = _install_finished(log_path, new_offset) and not chunk
    except OSError:
        # EIO: einfach leere Antwort → nächster Poll versucht es erneut
        pass

    return JsonResponse({'lines': lines, 'offset': new_offset, 'done': done, 'waiting': False})


def _install_finished(log_path, current_size):
    """Heuristic: log file hasn't grown and contains finish marker."""
    try:
        size = os.path.getsize(log_path)
        if size != current_size:
            return False
        with open(log_path, 'rb') as f:
            content = f.read().decode('utf-8', errors='replace')
        return 'INSTALLATION FERTIG' in content or 'ABBRUCH' in content or 'FEHLER' in content
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# SSH Key display + download
# ──────────────────────────────────────────────────────────────────────────────

def ssh_key_display(request, project):
    key_content, error = get_ssh_key(project)
    conf = get_project(project)
    return render(request, 'control/ssh_key.html', {
        'project': project,
        'key_content': key_content,
        'error': error,
        'conf': conf,
    })


def ssh_key_download(request, project):
    key_content, error = get_ssh_key(project)
    if error:
        raise Http404(error)
    response = HttpResponse(key_content, content_type='application/octet-stream')
    response['Content-Disposition'] = f'attachment; filename="id_ed25519_{project}"'
    return response


@require_POST
def ssh_key_confirm(request, project):
    """Called by web UI after user has saved the SSH key. Creates the confirm file."""
    confirm_file = f'/tmp/djmanager_installs/{project}_github_confirm'
    os.makedirs('/tmp/djmanager_installs', exist_ok=True)
    Path(confirm_file).touch()
    return JsonResponse({'ok': True})


# ──────────────────────────────────────────────────────────────────────────────
# Global GitHub Deploy Key
# ──────────────────────────────────────────────────────────────────────────────

@admin_required
def global_deploy_key(request):
    pub_key, error = get_global_deploy_key()
    return render(request, 'control/deploy_key.html', {
        'pub_key': pub_key,
        'error': error,
    })


@admin_required
def global_deploy_key_download(request):
    pub_key, error = get_global_deploy_key()
    if error:
        raise Http404(error)
    response = HttpResponse(pub_key + '\n', content_type='text/plain')
    response['Content-Disposition'] = 'attachment; filename="djmanager_github_ed25519.pub"'
    return response


# ──────────────────────────────────────────────────────────────────────────────
# Project detail + actions
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def project_detail(request, name):
    conf = get_project(name)
    if not conf:
        raise Http404(f'Projekt "{name}" nicht gefunden.')
    backups = list_backups(name)
    allowed_hosts = get_allowed_hosts(name)
    nginx_names = get_nginx_server_names(name)
    return render(request, 'control/project_detail.html', {
        'conf': conf,
        'name': name,
        'backups': backups,
        'allowed_hosts': allowed_hosts,
        'nginx_names': nginx_names,
    })


@admin_required
@require_POST
def project_allowed_hosts(request, name):
    """Add or remove ALLOWED_HOSTS entries for a project."""
    action = request.POST.get('action', 'add')
    current = get_allowed_hosts(name)

    if action == 'add':
        new_host = request.POST.get('new_host', '').strip()
        if not new_host:
            return JsonResponse({'ok': False, 'error': 'Kein Host angegeben'})
        if new_host not in current:
            current.append(new_host)
    elif action == 'remove':
        host = request.POST.get('host', '').strip()
        current = [h for h in current if h != host]
    elif action == 'save':
        raw = request.POST.get('hosts', '')
        current = [h.strip() for h in raw.split(',') if h.strip()]

    ok, msg = update_allowed_hosts(name, current)
    return JsonResponse({'ok': ok, 'message': msg, 'hosts': current})


@admin_required
@require_POST
def project_action(request, name):
    action = request.POST.get('action', '')
    message = ''
    error = ''

    if action in ('start', 'stop', 'restart'):
        ok, output = service_action(name, action)
        if ok:
            message = f'Aktion "{action}" erfolgreich ausgeführt.'
        else:
            error = f'Fehler bei "{action}": {output}'

    elif action == 'update':
        ok, output = run_update(name)
        if ok:
            message = 'Update erfolgreich abgeschlossen.'
        else:
            error = f'Update fehlgeschlagen:\n{output}'

    elif action == 'backup':
        ok, output = run_backup(name)
        if ok:
            message = 'Backup erfolgreich erstellt.'
        else:
            error = f'Backup fehlgeschlagen:\n{output}'

    conf = get_project(name)
    backups = list_backups(name)
    return render(request, 'control/project_detail.html', {
        'conf': conf,
        'name': name,
        'backups': backups,
        'message': message,
        'error': error,
    })


# ──────────────────────────────────────────────────────────────────────────────
# Log viewer
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def log_viewer(request, name):
    conf = get_project(name)
    if not conf:
        raise Http404(f'Projekt "{name}" nicht gefunden.')

    log_type = request.GET.get('type', 'journal')
    lines = int(request.GET.get('lines', 200))

    if log_type == 'journal':
        logs = get_journal_logs(name, lines)
    elif log_type == 'nginx_access':
        logs = get_nginx_log(name, 'access', lines)
    elif log_type == 'nginx_error':
        logs = get_nginx_log(name, 'error', lines)
    else:
        logs = 'Unbekannter Log-Typ'

    return render(request, 'control/log_viewer.html', {
        'name': name,
        'conf': conf,
        'logs': logs,
        'log_type': log_type,
        'lines': lines,
    })


# ──────────────────────────────────────────────────────────────────────────────
# Remove wizard
# ──────────────────────────────────────────────────────────────────────────────

@admin_required
def remove_confirm(request, name):
    conf = get_project(name)
    if not conf:
        raise Http404(f'Projekt "{name}" nicht gefunden.')
    return render(request, 'control/remove_confirm.html', {
        'name': name,
        'conf': conf,
    })


@admin_required
@require_POST
def remove_run(request, name):
    """Execute remove script with selected options."""
    opts = {
        'remove_appdir':  bool(request.POST.get('remove_appdir')),
        'remove_db':      bool(request.POST.get('remove_db')),
        'remove_user':    bool(request.POST.get('remove_user')),
        'remove_backups': bool(request.POST.get('remove_backups')),
        'remove_logs':    bool(request.POST.get('remove_logs')),
    }
    # Stop service first
    service_action(name, 'stop')

    # Run remove script (NONINTERACTIVE with opt env vars)
    from .utils import remove_project
    ok, output = remove_project(name, opts)

    return render(request, 'control/remove_done.html', {
        'name': name,
        'ok': ok,
        'output': output,
        'opts': opts,
    })


# ──────────────────────────────────────────────────────────────────────────────
# Auth: Login / Logout
# ──────────────────────────────────────────────────────────────────────────────

def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    error = None
    if request.method == 'POST':
        user = authenticate(
            request,
            username=request.POST.get('username', ''),
            password=request.POST.get('password', ''),
        )
        if user:
            login(request, user)
            return redirect(request.GET.get('next', 'dashboard'))
        error = 'Ungültiger Benutzername oder Passwort.'
    return render(request, 'control/login.html', {'error': error})


def logout_view(request):
    logout(request)
    return redirect('login')


# ──────────────────────────────────────────────────────────────────────────────
# Update (admin only)
# ──────────────────────────────────────────────────────────────────────────────

@admin_required
@require_POST
def project_update(request, name):
    from .utils import run_update
    ok, output = run_update(name)
    return JsonResponse({'ok': ok, 'output': output})
