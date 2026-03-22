"""
DjangoMultiDeploy Manager — Project views.
Contains: project_detail, project_allowed_hosts, project_action, backup_delete,
          project_upload_zip, project_stats, project_security_scan, project_http_scan,
          log_viewer, remove_confirm, remove_run, remove_done, project_update,
          project_favorite_commands, project_migrations, project_pip_outdated,
          project_pip_upgrade, project_clone_form, project_clone_run
"""
import os
import logging
import ipaddress
import uuid
import zipfile
import secrets
import string
import re
import subprocess
from pathlib import Path

from django.shortcuts import render, redirect
from django.http import JsonResponse, Http404
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.conf import settings

from ..models import AuditLog, UserProfile, FavoriteCommand
from ..utils import (
    get_all_projects, get_project, get_service_status,
    service_action, get_journal_logs, get_nginx_log,
    list_backups, run_update, run_backup, delete_backup,
    run_management_command, get_allowed_hosts, get_nginx_server_names,
    update_allowed_hosts, get_ufw_status, get_nginx_stats, get_service_restarts,
    extract_project_zip, update_project_from_zip,
    run_pip_audit, run_django_deploy_check,
    run_migration_status, run_pip_outdated, run_pip_upgrade,
    run_http_security_scan, KEYS_DIR, GLOBAL_DEPLOY_KEY,
    remove_project,
)
from ._helpers import (
    admin_required, operator_required, _get_role,
    _check_project_access, _build_extern_scan_hosts,
)

logger = logging.getLogger('djmanager.views.projects')


# ──────────────────────────────────────────────────────────────────────────────
# Project detail + actions
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def project_detail(request, name):
    if not _check_project_access(request.user, name):
        return render(request, 'control/403.html', status=403)
    conf = get_project(name)
    if not conf:
        raise Http404(f'Projekt "{name}" nicht gefunden.')
    backups           = list_backups(name)
    allowed_hosts     = get_allowed_hosts(name)
    nginx_names       = get_nginx_server_names(name)
    ufw               = get_ufw_status(conf.get('GUNICORN_PORT'))
    role              = _get_role(request.user)
    favorite_commands = list(FavoriteCommand.objects.filter(project_name=name))
    extern_scan_hosts = _build_extern_scan_hosts(nginx_names, allowed_hosts)
    return render(request, 'control/project_detail.html', {
        'conf':              conf,
        'name':              name,
        'backups':           backups,
        'allowed_hosts':     allowed_hosts,
        'nginx_names':       nginx_names,
        'extern_scan_hosts': extern_scan_hosts,
        'ufw':               ufw,
        'role':              role,
        'favorite_commands': favorite_commands,
    })


@require_POST
@operator_required
def project_allowed_hosts(request, name):
    if not _check_project_access(request.user, name):
        return JsonResponse({'ok': False, 'error': 'Zugriff verweigert'}, status=403)
    action  = request.POST.get('action', 'add')
    current = get_allowed_hosts(name)

    if action == 'add':
        new_host = request.POST.get('new_host', '').strip()
        if not new_host:
            return JsonResponse({'ok': False, 'error': 'Kein Host angegeben'})
        if new_host not in current:
            current.append(new_host)
    elif action == 'remove':
        host    = request.POST.get('host', '').strip()
        current = [h for h in current if h != host]
    elif action == 'save':
        raw     = request.POST.get('hosts', '')
        current = [h.strip() for h in raw.split(',') if h.strip()]

    ok, msg = update_allowed_hosts(name, current)
    AuditLog.log(request, f'ALLOWED_HOSTS geändert', project=name,
                 details=f'Aktion: {action}', success=ok)
    return JsonResponse({'ok': ok, 'message': msg, 'hosts': current})


@require_POST
@operator_required
def project_action(request, name):
    if not _check_project_access(request.user, name):
        return render(request, 'control/403.html', status=403)
    action  = request.POST.get('action', '')
    logger.warning('project_action: name=%r action=%r user=%s', name, action, request.user)
    message = ''
    error   = ''

    if action in ('start', 'stop', 'restart'):
        ok, output = service_action(name, action)
        if ok:
            message = f'Aktion "{action}" erfolgreich ausgeführt.'
        else:
            error = f'Fehler bei "{action}": {output}'
        AuditLog.log(request, f'Service {action}: {name}', project=name, success=ok)

    elif action == 'update':
        ok, output = run_update(name)
        if ok:
            message = 'Update erfolgreich abgeschlossen.'
        else:
            error = f'Update fehlgeschlagen:\n{output}'
        AuditLog.log(request, f'Update: {name}', project=name, success=ok)

    elif action == 'backup':
        ok, output = run_backup(name)
        if ok:
            message = 'Backup erfolgreich erstellt.'
        else:
            error = f'Backup fehlgeschlagen:\n{output}'
        AuditLog.log(request, f'Backup: {name}', project=name, success=ok)

    elif action == 'manage_command':
        raw_cmd = request.POST.get('manage_cmd', '').strip()
        try:
            ok, output = run_management_command(name, raw_cmd)
        except Exception as exc:
            logger.exception('run_management_command failed: project=%s cmd=%r', name, raw_cmd)
            ok, output = False, f'Interner Fehler: {exc}'
        AuditLog.log(request, f'manage.py {raw_cmd}: {name}', project=name, success=ok)
        return JsonResponse({'ok': ok, 'output': output})

    conf              = get_project(name)
    backups           = list_backups(name)
    allowed_hosts     = get_allowed_hosts(name)
    nginx_names       = get_nginx_server_names(name)
    ufw               = get_ufw_status(conf.get('GUNICORN_PORT') if conf else None)
    role              = _get_role(request.user)
    favorite_commands = list(FavoriteCommand.objects.filter(project_name=name))
    extern_scan_hosts = _build_extern_scan_hosts(nginx_names, allowed_hosts)
    return render(request, 'control/project_detail.html', {
        'conf':              conf,
        'name':              name,
        'backups':           backups,
        'allowed_hosts':     allowed_hosts,
        'nginx_names':       nginx_names,
        'extern_scan_hosts': extern_scan_hosts,
        'ufw':               ufw,
        'message':           message,
        'error':             error,
        'role':              role,
        'favorite_commands': favorite_commands,
    })


@require_POST
@operator_required
def backup_delete(request, name):
    if not _check_project_access(request.user, name):
        return JsonResponse({'ok': False, 'message': 'Zugriff verweigert'}, status=403)
    filename = request.POST.get('filename', '').strip()
    ok, msg  = delete_backup(name, filename)
    AuditLog.log(request, f'Backup gelöscht: {filename}', project=name, success=ok)
    backups  = list_backups(name)
    return JsonResponse({'ok': ok, 'message': msg, 'backups': [
        {'name': b['name'], 'size_mb': b['size_mb'], 'mtime': b['mtime'],
         'mtime_str': b.get('mtime_str', '')}
        for b in backups
    ]})


@require_POST
@operator_required
def project_upload_zip(request, name):
    if not _check_project_access(request.user, name):
        return JsonResponse({'ok': False, 'output': 'Zugriff verweigert'}, status=403)
    zip_file = request.FILES.get('zip_file')
    if not zip_file:
        return JsonResponse({'ok': False, 'output': 'Keine Datei empfangen.'})
    if not zip_file.name.endswith('.zip'):
        return JsonResponse({'ok': False, 'output': 'Nur .zip Dateien erlaubt.'})
    if zip_file.size > 200 * 1024 * 1024:
        return JsonResponse({'ok': False, 'output': 'ZIP-Datei zu groß (max 200 MB).'})
    ok, output = update_project_from_zip(name, zip_file)
    AuditLog.log(request, f'ZIP-Update: {name}', project=name,
                 details=zip_file.name, success=ok)
    return JsonResponse({'ok': ok, 'output': output})


@login_required
def project_stats(request, name):
    if not _check_project_access(request.user, name):
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)
    nginx    = get_nginx_stats(name)
    restarts = get_service_restarts(name)
    return JsonResponse({'nginx': nginx, 'restarts': restarts})


@login_required
def project_security_scan(request, name):
    if not _check_project_access(request.user, name):
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)
    """Run pip-audit + manage.py check --deploy (lazy-loaded from frontend)."""
    pip_results   = run_pip_audit(name)
    deploy_issues = run_django_deploy_check(name)
    return JsonResponse({
        'pip_audit':    pip_results,
        'deploy_check': deploy_issues,
    })


@login_required
def project_http_scan(request, name):
    """
    HTTP/TLS security scan for a project.
    GET params:
      target=internal|<hostname>   (default: internal)
    """
    if not _check_project_access(request.user, name):
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)

    conf = get_project(name)
    if not conf:
        return JsonResponse({'error': 'Projekt nicht gefunden'}, status=404)

    target = request.GET.get('target', 'internal')

    if target == 'internal':
        port = conf.get('GUNICORN_PORT', '')
        if not port:
            return JsonResponse({'error': 'Kein Gunicorn-Port konfiguriert'}, status=400)
        url = f'http://127.0.0.1:{port}/'
        result = run_http_security_scan(url, hostname=None, check_tls=False)
    else:
        # target is a hostname or IP — use the project's configured nginx port
        hostname = target.lower()  # normalise: DNS is case-insensitive
        # IPv6 addresses must be wrapped in brackets in URLs
        try:
            addr = ipaddress.ip_address(hostname)
            url_host = f'[{hostname}]' if isinstance(addr, ipaddress.IPv6Address) else hostname
        except ValueError:
            url_host = hostname
        # For external hostname scans always use standard ports (443/80).
        # NGINX_PORT is the internal nginx listener port — the external port
        # is always the standard HTTPS (443) or HTTP (80) port, since a reverse
        # proxy (or the same nginx) handles TLS termination on 443 externally.
        nginx_port = str(conf.get('NGINX_PORT', '443')).strip()
        if nginx_port == '80':
            url = f'http://{url_host}/'
            check_tls = False
        else:
            url = f'https://{url_host}/'
            check_tls = True
        # Use local nginx port to avoid hairpin-NAT timeouts (server cannot reach
        # its own public domain from within). Only bypass when port is non-standard
        # (i.e. the per-project nginx port, not 80/443 which are the public ports).
        _local_port = int(nginx_port) if nginx_port not in ('80', '443') else None
        result = run_http_security_scan(url, hostname=hostname, check_tls=check_tls,
                                        local_port=_local_port)

    return JsonResponse(result)


# ──────────────────────────────────────────────────────────────────────────────
# Log viewer
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def log_viewer(request, name):
    if not _check_project_access(request.user, name):
        return render(request, 'control/403.html', status=403)
    conf = get_project(name)
    if not conf:
        # Allow viewing logs for the manager service itself
        _svc = getattr(settings, 'MANAGER_SERVICE_NAME', 'djmanager')
        if name == _svc:
            conf = {'PROJECTNAME': name, '_is_manager': True}
        else:
            raise Http404(f'Projekt "{name}" nicht gefunden.')

    log_type = request.GET.get('type', 'journal')
    lines    = int(request.GET.get('lines', 200))

    if log_type == 'journal':
        logs = get_journal_logs(name, lines)
    elif log_type == 'nginx_access':
        logs = get_nginx_log(name, 'access', lines)
    elif log_type == 'nginx_error':
        logs = get_nginx_log(name, 'error', lines)
    else:
        logs = 'Unbekannter Log-Typ'

    return render(request, 'control/log_viewer.html', {
        'name': name, 'conf': conf, 'logs': logs,
        'log_type': log_type, 'lines': lines,
        'is_manager': bool(conf.get('_is_manager')),
    })


# ──────────────────────────────────────────────────────────────────────────────
# Remove wizard
# ──────────────────────────────────────────────────────────────────────────────

@admin_required
def remove_confirm(request, name):
    # Admins can always remove; project access check not needed here
    conf = get_project(name)
    if not conf:
        raise Http404(f'Projekt "{name}" nicht gefunden.')
    return render(request, 'control/remove_confirm.html', {'name': name, 'conf': conf})


@require_POST
@admin_required
def remove_run(request, name):
    opts = {
        'remove_appdir':  bool(request.POST.get('remove_appdir')),
        'remove_db':      bool(request.POST.get('remove_db')),
        'remove_user':    bool(request.POST.get('remove_user')),
        'remove_backups': bool(request.POST.get('remove_backups')),
        'remove_logs':    bool(request.POST.get('remove_logs')),
    }
    ok, output = remove_project(name, opts)
    AuditLog.log(request, f'Projekt entfernt: {name}', project=name,
                 details=str(opts), success=ok)
    # PRG pattern: store result in session, redirect to GET → avoids
    # "resubmit form?" on mobile and makes "Zurück" safe.
    request.session['remove_result'] = {
        'name': name, 'ok': ok, 'output': output, 'opts': opts,
    }
    return redirect('remove_done')


@login_required
def remove_done(request):
    result = request.session.pop('remove_result', None)
    if not result:
        return redirect('dashboard')
    return render(request, 'control/remove_done.html', result)


# ──────────────────────────────────────────────────────────────────────────────
# Update (operator+)
# ──────────────────────────────────────────────────────────────────────────────

@require_POST
@operator_required
def project_update(request, name):
    if not _check_project_access(request.user, name):
        return JsonResponse({'ok': False, 'output': 'Zugriff verweigert'}, status=403)
    ok, output = run_update(name)
    AuditLog.log(request, f'Git-Update: {name}', project=name, success=ok)
    return JsonResponse({'ok': ok, 'output': output})


# ──────────────────────────────────────────────────────────────────────────────
# Favorite Commands — per-project quick-access management command buttons
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def project_favorite_commands(request, name):
    """GET: list as JSON. POST add/delete favorite commands."""
    if not _check_project_access(request.user, name):
        return JsonResponse({'ok': False, 'error': 'Zugriff verweigert'}, status=403)

    if request.method == 'GET':
        cmds = list(FavoriteCommand.objects.filter(project_name=name).values(
            'id', 'label', 'command', 'order'))
        return JsonResponse({'ok': True, 'commands': cmds})

    # POST: add or delete
    role = _get_role(request.user)
    if role not in (UserProfile.ROLE_ADMIN, UserProfile.ROLE_OPERATOR):
        return JsonResponse({'ok': False, 'error': 'Nur Admins/Operators erlaubt'}, status=403)

    action = request.POST.get('action', '')

    if action == 'add':
        import re as _re
        label   = request.POST.get('label', '').strip()[:80]
        command = request.POST.get('command', '').strip()[:500]
        # Strip leading 'python manage.py' / 'manage.py'
        command = _re.sub(r'^(python\s+)?(\./)?manage\.py\s*', '', command).strip()
        if not label or not command:
            return JsonResponse({'ok': False, 'error': 'Label und Befehl erforderlich'})
        if _re.search(r'[;&|`$<>]', command):
            return JsonResponse({'ok': False, 'error': 'Ungültige Zeichen im Befehl'})
        obj, created = FavoriteCommand.objects.get_or_create(
            project_name=name, command=command,
            defaults={'label': label},
        )
        if not created:
            return JsonResponse({'ok': False, 'error': f'Befehl "{command}" bereits vorhanden'})
        AuditLog.log(request, f'Favorit hinzugefügt: {command}', project=name)
        cmds = list(FavoriteCommand.objects.filter(project_name=name).values(
            'id', 'label', 'command', 'order'))
        return JsonResponse({'ok': True, 'commands': cmds})

    elif action == 'delete':
        pk = request.POST.get('pk', '')
        deleted, _ = FavoriteCommand.objects.filter(project_name=name, pk=pk).delete()
        if not deleted:
            return JsonResponse({'ok': False, 'error': 'Eintrag nicht gefunden'})
        AuditLog.log(request, f'Favorit entfernt (pk={pk})', project=name)
        cmds = list(FavoriteCommand.objects.filter(project_name=name).values(
            'id', 'label', 'command', 'order'))
        return JsonResponse({'ok': True, 'commands': cmds})

    return JsonResponse({'ok': False, 'error': 'Unbekannte Aktion'})


# ──────────────────────────────────────────────────────────────────────────────
# Migration Status
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def project_migrations(request, name):
    if not _check_project_access(request.user, name):
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)
    result = run_migration_status(name)
    return JsonResponse(result)


# ──────────────────────────────────────────────────────────────────────────────
# Outdated Packages
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def project_pip_outdated(request, name):
    if not _check_project_access(request.user, name):
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)
    result = run_pip_outdated(name)
    return JsonResponse(result)


# ──────────────────────────────────────────────────────────────────────────────
# Pip Upgrade (single package)
# ──────────────────────────────────────────────────────────────────────────────

@require_POST
@operator_required
def project_pip_upgrade(request, name):
    if not _check_project_access(request.user, name):
        return JsonResponse({'ok': False, 'output': 'Zugriff verweigert'}, status=403)
    package = request.POST.get('package', '').strip()
    if not package:
        return JsonResponse({'ok': False, 'output': 'Kein Paketname angegeben'})
    result = run_pip_upgrade(name, package)
    AuditLog.log(request, f'pip upgrade {package}: {name}', project=name, success=result['ok'])
    return JsonResponse(result)


# ──────────────────────────────────────────────────────────────────────────────
# Staging Clone — create a test copy of a project
# ──────────────────────────────────────────────────────────────────────────────

@admin_required
def project_clone_form(request, name):
    """Show a form to clone a project as a staging environment."""
    conf = get_project(name)
    if not conf:
        raise Http404(f'Projekt "{name}" nicht gefunden.')

    import secrets, string
    def _rand_pw(n=16):
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(n))

    # Suggest defaults
    staging_name = f'{name}stg'
    used_ports   = {p.get('GUNICORN_PORT') for p in get_all_projects() if p.get('GUNICORN_PORT')}
    next_port    = next((str(p) for p in range(8000, 9000) if str(p) not in used_ports), '8010')

    return render(request, 'control/project_clone.html', {
        'conf':         conf,
        'name':         name,
        'staging_name': staging_name,
        'next_port':    next_port,
        'db_pass':      _rand_pw(),
        'app_pass':     _rand_pw(),
        'admin_pass':   _rand_pw(),
    })


@require_POST
@admin_required
def project_clone_run(request, name):
    """Start installing a staging clone of the project."""
    conf = get_project(name)
    if not conf:
        raise Http404(f'Projekt "{name}" nicht gefunden.')

    staging_name = request.POST.get('staging_name', f'{name}stg').strip()
    # Basic validation: only alphanumeric + underscore, no spaces
    import re as _re
    if not _re.match(r'^[a-zA-Z][a-zA-Z0-9_]{1,29}$', staging_name):
        return render(request, 'control/project_clone.html', {
            'conf': conf, 'name': name, 'staging_name': staging_name,
            'error': 'Ungültiger Name (nur Buchstaben, Ziffern, _, 2-30 Zeichen, muss mit Buchstabe beginnen)',
        })
    if get_project(staging_name):
        return render(request, 'control/project_clone.html', {
            'conf': conf, 'name': name, 'staging_name': staging_name,
            'error': f'Projekt "{staging_name}" existiert bereits.',
        })

    gunicorn_port = request.POST.get('gunicorn_port', '').strip()
    db_pass       = request.POST.get('db_pass', '').strip()
    app_pass      = request.POST.get('app_pass', '').strip()
    admin_pass    = request.POST.get('admin_pass', '').strip()

    github_url = conf.get('GITHUB_REPO_URL', '').strip()

    params = {
        'PROJECTNAME':        staging_name,
        'APPUSER':            staging_name,
        'MODESEL':            '1',    # DEV mode for staging
        'GUNICORN_PORT':      gunicorn_port,
        'GUNICORN_WORKERS':   '2',
        'ALLOWED_HOSTS':      '*',
        'DBTYPE_SEL':         {'postgresql': '2', 'mysql': '3', 'sqlite': '1'}.get(
                                  conf.get('DBTYPE', 'sqlite'), '1'),
        'DBMODE':             '2',    # local DB
        'DBNAME':             staging_name,
        'DBUSER':             staging_name,
        'DBPASS':             db_pass,
        'APPUSER_PASS':       app_pass,
        'DJANGO_ADMIN_USER':  'admin',
        'DJANGO_ADMIN_EMAIL': 'admin@localhost',
        'DJANGO_ADMIN_PASS':  admin_pass,
        'LANGUAGE_CODE':      conf.get('LANGUAGE_CODE', 'de-de'),
        'TIME_ZONE':          conf.get('TIME_ZONE', 'Europe/Berlin'),
        '_INSTALL_SEL':       '1',
        'UPGRADE':            'n',
        'INSTALL_FAIL2BAN':   'n',
    }

    if github_url:
        params['SOURCE_TYPE']       = 'github'
        params['GITHUB_REPO_URL']   = github_url
        # Reuse project's deploy key if available
        deploy_key_id = conf.get('DEPLOY_KEY_ID', '').strip()
        if deploy_key_id:
            key_path = os.path.join(KEYS_DIR, f'{deploy_key_id}_ed25519')
            if os.path.exists(key_path):
                params['GITHUB_DEPLOY_KEY'] = key_path
                params['DEPLOY_KEY_ID']     = deploy_key_id
        else:
            # Fall back to legacy per-project key, then global key
            legacy_key = f'/root/.ssh/deploy_{name}_ed25519'
            if os.path.exists(legacy_key):
                params['GITHUB_DEPLOY_KEY'] = legacy_key
            elif os.path.exists(GLOBAL_DEPLOY_KEY):
                params['GITHUB_DEPLOY_KEY'] = GLOBAL_DEPLOY_KEY
    else:
        # No GitHub → create a ZIP of the existing project
        appdir   = conf.get('APPDIR', f'/srv/{name}')
        zip_path = f'/tmp/djmanager_clone_{staging_name}.zip'
        try:
            import zipfile
            excludes = {'.venv', 'venv', '__pycache__', 'staticfiles', 'media', '.git'}
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                base = Path(appdir)
                for fpath in base.rglob('*'):
                    # Skip excluded directories
                    parts = fpath.relative_to(base).parts
                    if any(p in excludes for p in parts):
                        continue
                    if fpath.name in ('.env',):
                        continue
                    if fpath.is_file():
                        zf.write(fpath, fpath.relative_to(base))
            params['SOURCE_TYPE']       = 'zip'
            params['UPLOAD_ZIP_PATH']   = zip_path
        except Exception as e:
            return render(request, 'control/project_clone.html', {
                'conf': conf, 'name': name, 'staging_name': staging_name,
                'error': f'Fehler beim Erstellen des ZIP-Archivs: {e}',
            })

    run_id   = str(uuid.uuid4())[:8]
    log_dir  = settings.INSTALL_LOG_DIR
    log_name = f'{staging_name}_{run_id}.log'
    log_path = os.path.join(log_dir, log_name)

    env = os.environ.copy()
    env.update({k: v for k, v in params.items() if v})
    env['NONINTERACTIVE'] = 'true'

    os.makedirs(log_dir, exist_ok=True)
    with open(log_path, 'w') as log_f:
        subprocess.Popen(
            ['bash', settings.INSTALL_SCRIPT],
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    AuditLog.log(request, f'Staging-Klon gestartet: {staging_name} (von {name})',
                 project=name, details=f'Port: {gunicorn_port}')

    # Store credentials in session so we can show them on progress page
    request.session[f'clone_creds_{run_id}'] = {
        'staging_name': staging_name,
        'db_pass':      db_pass,
        'app_pass':     app_pass,
        'admin_pass':   admin_pass,
    }
    return redirect('install_progress', project=staging_name, run_id=run_id)


