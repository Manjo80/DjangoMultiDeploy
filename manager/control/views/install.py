"""
DjangoMultiDeploy Manager — Install views.
Contains: install_form, install_run, install_progress, install_poll,
          _install_finished, install_manager, install_kill,
          ssh_key_display, ssh_key_download, ssh_key_confirm,
          global_deploy_key, global_deploy_key_download
"""
import os
import glob
import signal
import logging
import uuid
import datetime
from pathlib import Path

from django.shortcuts import render, redirect
from django.http import HttpResponse, Http404, JsonResponse
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.conf import settings

from ..models import AuditLog
from ..utils import (
    get_all_projects, get_project, get_ssh_key,
    start_install, list_deploy_keys, create_deploy_key,
    assign_project_deploy_key, KEYS_DIR, GLOBAL_DEPLOY_KEY,
    get_global_deploy_key,
)
from ._helpers import admin_required

import subprocess

logger = logging.getLogger('djmanager.views.install')


# ──────────────────────────────────────────────────────────────────────────────
# Install wizard
# ──────────────────────────────────────────────────────────────────────────────

@admin_required
def install_form(request):
    import socket, subprocess as _sp
    used_ports = {p.get('GUNICORN_PORT') for p in get_all_projects() if p.get('GUNICORN_PORT')}
    next_port  = next((str(p) for p in range(8000, 9000) if str(p) not in used_ports), '8000')
    try:
        raw     = _sp.check_output(['hostname', '-I'], text=True).strip()
        all_ips = [ip for ip in raw.split() if not ip.startswith('127.')]
    except Exception:
        all_ips = []
    if not all_ips:
        try:
            all_ips = [socket.gethostbyname(socket.gethostname())]
        except Exception:
            all_ips = []
    allowed_suggestion = ','.join(all_ips) if all_ips else ''
    # Pass unused deploy keys for the optional "reuse existing key" dropdown
    all_keys    = list_deploy_keys()
    unused_keys = [k for k in all_keys if not k['projects']]
    return render(request, 'control/install_form.html', {
        'next_port':               next_port,
        'server_ips':              all_ips,
        'allowed_hosts_suggestion': allowed_suggestion,
        'unused_keys':             unused_keys,
    })


@require_POST
@admin_required
def install_run(request):
    data        = request.POST
    source_type = data.get('source_type', 'new').strip()

    params = {
        'PROJECTNAME':         data.get('projectname', '').strip(),
        'APPUSER':             data.get('appuser', '').strip(),
        'MODESEL':             data.get('modesel', '1'),
        'GITHUB_REPO_URL':     data.get('github_repo_url', '').strip() if source_type == 'github' else '',
        'SOURCE_TYPE':         source_type,
        'GUNICORN_PORT':       data.get('gunicorn_port', '').strip(),
        'GUNICORN_WORKERS':    data.get('gunicorn_workers', '').strip(),
        'ALLOWED_HOSTS':       data.get('allowed_hosts', '').strip(),
        'DBTYPE_SEL':          data.get('dbtype_sel', '1'),
        'DBMODE':              data.get('dbmode', '2'),
        'DBNAME':              data.get('dbname', '').strip(),
        'DBUSER':              data.get('dbuser', '').strip(),
        'DBPASS':              data.get('dbpass', '').strip(),
        'DBHOST':              data.get('dbhost', 'localhost').strip(),
        'DBPORT':              data.get('dbport', '5432').strip(),
        'APPUSER_PASS':        data.get('appuser_pass', '').strip(),
        'DJANGO_ADMIN_USER':   data.get('django_admin_user', 'admin').strip(),
        'DJANGO_ADMIN_EMAIL':  data.get('django_admin_email', 'admin@localhost').strip(),
        'DJANGO_ADMIN_PASS':   data.get('django_admin_pass', '').strip(),
        'DJKEY':               data.get('djkey', '').strip(),
        'LANGUAGE_CODE':       data.get('language_code', 'de-de').strip(),
        'TIME_ZONE':           data.get('time_zone', 'Europe/Berlin').strip(),
        'EMAIL_HOST':          data.get('email_host', '').strip(),
        'EMAIL_PORT':          data.get('email_port', '587').strip(),
        'EMAIL_HOST_USER':     data.get('email_host_user', '').strip(),
        'EMAIL_HOST_PASSWORD': data.get('email_host_password', '').strip(),
        'EMAIL_USE_TLS':       'True' if data.get('email_use_tls') else 'False',
        'DEFAULT_FROM_EMAIL':  data.get('default_from_email', '').strip(),
        '_BACKUP_TIME':        data.get('backup_time', '02:00').strip(),
        '_INSTALL_SEL':        '1',
        'UPGRADE':             'n',
        'INSTALL_FAIL2BAN':    'n',
    }
    params = {k: v for k, v in params.items() if v != ''}

    project = params.get('PROJECTNAME', '')
    if not project:
        return render(request, 'control/install_form.html',
                      {'error': 'Projektname darf nicht leer sein.'})

    if source_type == 'zip':
        zip_file = request.FILES.get('zip_file')
        if not zip_file:
            return render(request, 'control/install_form.html',
                          {'error': 'Bitte eine ZIP-Datei auswählen.'})
        if not zip_file.name.endswith('.zip'):
            return render(request, 'control/install_form.html',
                          {'error': 'Nur .zip Dateien erlaubt.'})
        upload_dir = '/tmp/dmd_uploads'
        os.makedirs(upload_dir, exist_ok=True)
        zip_path = os.path.join(upload_dir, f'{project}.zip')
        with open(zip_path, 'wb') as f:
            for chunk in zip_file.chunks():
                f.write(chunk)
        params['UPLOAD_ZIP_PATH'] = zip_path

    # Create a dedicated deploy key BEFORE the install script runs so it is
    # ready when git clone happens and can be written to the project's .conf.
    if source_type == 'github' and params.get('GITHUB_REPO_URL'):
        existing_key_id = data.get('EXISTING_DEPLOY_KEY_ID', '').strip()
        if existing_key_id:
            # Reuse an existing unused key
            key_path = os.path.join(KEYS_DIR, f'{existing_key_id}_ed25519')
            if os.path.exists(key_path):
                params['GITHUB_DEPLOY_KEY'] = key_path
                params['DEPLOY_KEY_ID']     = existing_key_id
            else:
                existing_key_id = ''  # fallback to create new
        if not existing_key_id:
            key_label = f'{project} deploy key'
            key_id, _pub, _key_err = create_deploy_key(key_label)
            if key_id:
                params['GITHUB_DEPLOY_KEY'] = os.path.join(KEYS_DIR, f'{key_id}_ed25519')
                params['DEPLOY_KEY_ID']     = key_id

    run_id   = str(uuid.uuid4())[:8]
    log_dir  = settings.INSTALL_LOG_DIR
    log_name = f'{project}_{run_id}.log'
    log_path = os.path.join(log_dir, log_name)

    env = os.environ.copy()
    env.update(params)
    env['NONINTERACTIVE'] = 'true'

    os.makedirs(log_dir, exist_ok=True)
    os.makedirs('/tmp/djmanager_installs', exist_ok=True)
    with open(log_path, 'w') as log_f:
        proc = subprocess.Popen(
            ['bash', settings.INSTALL_SCRIPT],
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    pid_path = f'/tmp/djmanager_installs/{project}_{run_id}.pid'
    Path(pid_path).write_text(str(proc.pid))

    AuditLog.log(request, f'Projekt-Installation gestartet: {project}',
                 project=project, details=f'Quelle: {source_type}')
    return redirect('install_progress', project=project, run_id=run_id)


@login_required
def install_progress(request, project, run_id):
    log_name = f'{project}_{run_id}.log'
    return render(request, 'control/install_progress.html', {
        'project':  project,
        'run_id':   run_id,
        'log_name': log_name,
    })


@login_required
def install_poll(request, log_name):
    log_path = os.path.join(settings.INSTALL_LOG_DIR, log_name)
    try:
        offset = int(request.GET.get('offset', 0))
    except (ValueError, TypeError):
        offset = 0

    if not os.path.exists(log_path):
        return JsonResponse({'lines': [], 'offset': 0, 'done': False, 'waiting': True})

    lines      = []
    new_offset = offset
    done       = False
    try:
        with open(log_path, 'rb') as f:
            f.seek(offset)
            chunk      = f.read(65536)
            new_offset = offset + len(chunk)
        if chunk:
            text  = chunk.decode('utf-8', errors='replace')
            lines = [l.rstrip('\r') for l in text.splitlines()]
        done = _install_finished(log_path, new_offset) and not chunk
    except OSError:
        pass

    return JsonResponse({'lines': lines, 'offset': new_offset, 'done': done, 'waiting': False})


def _install_finished(log_path, current_size):
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
# Installation Manager
# ──────────────────────────────────────────────────────────────────────────────

@admin_required
def install_manager(request):
    """Listet alle laufenden/abgeschlossenen Installationen auf."""
    log_dir = settings.INSTALL_LOG_DIR
    tmp_dir = '/tmp/djmanager_installs'
    installs = []

    try:
        log_files = sorted(
            glob.glob(os.path.join(log_dir, '*.log')),
            key=os.path.getmtime,
            reverse=True,
        )
    except Exception:
        log_files = []

    for log_path in log_files:
        log_name = os.path.basename(log_path)
        if not log_name.endswith('.log'):
            continue
        base = log_name[:-4]
        # Format: {project}_{run_id}.log — run_id ist immer 8 Zeichen
        if len(base) < 10 or '_' not in base:
            continue
        run_id  = base[-8:]
        project = base[:-9]  # project = base ohne _run_id

        pid_path      = os.path.join(tmp_dir, f'{project}_{run_id}.pid')
        pid           = None
        process_alive = False

        if os.path.exists(pid_path):
            try:
                pid = int(Path(pid_path).read_text().strip())
                os.kill(pid, 0)
                process_alive = True
            except (OSError, ValueError, ProcessLookupError):
                process_alive = False

        try:
            with open(log_path, 'rb') as f:
                content = f.read().decode('utf-8', errors='replace')
        except Exception:
            content = ''

        if 'INSTALLATION FERTIG' in content:
            status = 'fertig'
        elif 'ABBRUCH' in content or 'FEHLER' in content:
            status = 'fehler'
        elif process_alive:
            last_marker = content.rfind('##WAIT_GITHUB_CONFIRM##')
            if last_marker != -1:
                after = content[last_marker:]
                if 'successfully authenticated' in after or 'INSTALLATION FERTIG' in after:
                    status = 'laeuft'
                else:
                    status = 'wartet'
            else:
                status = 'laeuft'
        else:
            status = 'unbekannt'

        try:
            mtime   = os.path.getmtime(log_path)
            started = datetime.datetime.fromtimestamp(mtime).strftime('%d.%m.%Y %H:%M')
        except Exception:
            started = '–'

        installs.append({
            'project':       project,
            'run_id':        run_id,
            'log_name':      log_name,
            'status':        status,
            'pid':           pid,
            'process_alive': process_alive,
            'started':       started,
        })

    return render(request, 'control/install_manager.html', {'installs': installs})


@admin_required
@require_POST
def install_kill(request, project, run_id):
    """Bricht eine laufende Installation ab und räumt auf."""
    tmp_dir = '/tmp/djmanager_installs'
    log_dir = settings.INSTALL_LOG_DIR
    killed  = False

    pid_path = os.path.join(tmp_dir, f'{project}_{run_id}.pid')
    if os.path.exists(pid_path):
        try:
            pid = int(Path(pid_path).read_text().strip())
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                killed = True
            except ProcessLookupError:
                pass
        except (ValueError, OSError):
            pass
        try:
            os.unlink(pid_path)
        except OSError:
            pass

    for suffix in ['_github_confirm', '_github_wait']:
        f = os.path.join(tmp_dir, f'{project}{suffix}')
        try:
            os.unlink(f)
        except OSError:
            pass

    log_path = os.path.join(log_dir, f'{project}_{run_id}.log')
    try:
        os.unlink(log_path)
    except OSError:
        pass

    AuditLog.log(request, f'Installation abgebrochen/gelöscht: {project}', project=project)
    return JsonResponse({'ok': True, 'killed': killed})


# ──────────────────────────────────────────────────────────────────────────────
# SSH Key
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def ssh_key_display(request, project):
    key_content, error = get_ssh_key(project)
    conf = get_project(project)
    return render(request, 'control/ssh_key.html', {
        'project':     project,
        'key_content': key_content,
        'error':       error,
        'conf':        conf,
    })


@login_required
def ssh_key_download(request, project):
    key_content, error = get_ssh_key(project)
    if error:
        raise Http404(error)
    response = HttpResponse(key_content, content_type='application/octet-stream')
    response['Content-Disposition'] = f'attachment; filename="id_ed25519_{project}"'
    return response


@require_POST
@login_required
def ssh_key_confirm(request, project):
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
        'pub_key': pub_key, 'error': error,
    })


@admin_required
def global_deploy_key_download(request):
    pub_key, error = get_global_deploy_key()
    if error:
        raise Http404(error)
    response = HttpResponse(pub_key + '\n', content_type='text/plain')
    response['Content-Disposition'] = 'attachment; filename="djmanager_github_ed25519.pub"'
    return response


