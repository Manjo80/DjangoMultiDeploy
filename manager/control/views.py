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

from .utils import (
    get_all_projects, get_project, get_service_status,
    service_action, get_journal_logs, get_nginx_log,
    list_backups, run_update, run_backup, get_ssh_key, start_install,
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

def install_form(request):
    return render(request, 'control/install_form.html')


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
        'DJKEY':              data.get('djkey', '').strip(),
        'SSH_KEY_PASSPHRASE': data.get('ssh_key_passphrase', '').strip(),
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

    with open(log_path, 'w') as log_f:
        subprocess.Popen(
            ['bash', settings.INSTALL_SCRIPT],
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )

    return redirect('install_progress', project=project, run_id=run_id)


def install_progress(request, project, run_id):
    log_name = f'{project}_{run_id}.log'
    return render(request, 'control/install_progress.html', {
        'project': project,
        'run_id': run_id,
        'log_name': log_name,
    })


def install_stream(request, log_name):
    """SSE endpoint: stream install log file line by line."""
    log_path = os.path.join(settings.INSTALL_LOG_DIR, log_name)

    def event_stream():
        sent = 0
        idle_count = 0
        max_idle = 300  # 300 × 0.5s = 150s timeout
        while idle_count < max_idle:
            try:
                if not os.path.exists(log_path):
                    time.sleep(0.5)
                    idle_count += 1
                    continue
                with open(log_path) as f:
                    f.seek(sent)
                    chunk = f.read(4096)
                    if chunk:
                        idle_count = 0
                        for line in chunk.splitlines():
                            yield f'data: {line}\n\n'
                        sent += len(chunk.encode('utf-8', errors='replace'))
                    else:
                        idle_count += 1
                        time.sleep(0.5)
                        # Check if install finished
                        if _install_finished(log_path, sent):
                            yield 'data: __DONE__\n\n'
                            return
            except Exception as e:
                yield f'data: [stream error: {e}]\n\n'
                return
        yield 'data: __TIMEOUT__\n\n'

    response = StreamingHttpResponse(
        event_stream(),
        content_type='text/event-stream'
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


def _install_finished(log_path, current_size):
    """Heuristic: log file hasn't grown for 3+ seconds and contains finish marker."""
    try:
        size = os.path.getsize(log_path)
        if size != current_size:
            return False
        with open(log_path) as f:
            content = f.read()
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
# Project detail + actions
# ──────────────────────────────────────────────────────────────────────────────

def project_detail(request, name):
    conf = get_project(name)
    if not conf:
        raise Http404(f'Projekt "{name}" nicht gefunden.')
    backups = list_backups(name)
    return render(request, 'control/project_detail.html', {
        'conf': conf,
        'name': name,
        'backups': backups,
    })


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

def remove_confirm(request, name):
    conf = get_project(name)
    if not conf:
        raise Http404(f'Projekt "{name}" nicht gefunden.')
    return render(request, 'control/remove_confirm.html', {
        'name': name,
        'conf': conf,
    })


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
