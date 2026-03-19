"""
DjangoMultiDeploy Manager — Views
"""
import os
import json
import time
import uuid
import logging
import subprocess
from pathlib import Path
from functools import wraps

from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.http import HttpResponse, JsonResponse, Http404
from django.views.decorators.http import require_POST
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.conf import settings

from .models import UserProfile, AuditLog, SecuritySettings, ProjectPermission, FavoriteCommand

logger = logging.getLogger('djmanager.views')
from .utils import (
    get_all_projects, get_project, get_service_status,
    service_action, get_journal_logs, get_nginx_log,
    list_backups, run_update, run_backup, delete_backup, get_ssh_key, start_install,
    run_management_command,
    get_global_deploy_key, get_project_deploy_key,
    create_deploy_key, list_deploy_keys, get_deploy_key_pubkey,
    delete_deploy_key, assign_project_deploy_key, KEYS_DIR,
    get_allowed_hosts, get_nginx_server_names,
    update_allowed_hosts, get_ufw_status, get_server_stats, get_last_backup,
    get_nginx_stats, get_service_restarts,
    extract_project_zip, update_project_from_zip,
    run_pip_audit, run_django_deploy_check,
    run_manager_pip_audit, run_manager_deploy_check,
    sync_env_to_conf,
    get_ufw_port_rules, ufw_toggle_port,
    run_migration_status, run_pip_outdated, run_pip_upgrade,
)


# ──────────────────────────────────────────────────────────────────────────────
# Role helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_role(user):
    """Return the role string for a user. Superusers are always 'admin'."""
    if user.is_superuser:
        return UserProfile.ROLE_ADMIN
    try:
        return user.userprofile.role
    except Exception:
        return UserProfile.ROLE_VIEWER


def role_required(*roles):
    """Decorator: require user to have one of the given roles."""
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect(f'/login/?next={request.path}')
            if _get_role(request.user) not in roles:
                return render(request, 'control/403.html', status=403)
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


# Convenience aliases
admin_required    = role_required(UserProfile.ROLE_ADMIN)
operator_required = role_required(UserProfile.ROLE_ADMIN, UserProfile.ROLE_OPERATOR)


def _allowed_projects(user):
    """
    Returns a set of project names the user may access, or None if unrestricted.
    Admin / superuser → None (all projects).
    Operator / Viewer → set of assigned project names (may be empty).
    """
    if user.is_superuser or _get_role(user) == UserProfile.ROLE_ADMIN:
        return None
    return set(
        ProjectPermission.objects.filter(user=user).values_list('project_name', flat=True)
    )


def _check_project_access(user, name):
    """Returns True when the user may access this project."""
    allowed = _allowed_projects(user)
    return allowed is None or name in allowed


# ──────────────────────────────────────────────────────────────────────────────
# Manager self-info helper
# ──────────────────────────────────────────────────────────────────────────────

def _get_manager_info():
    """Build a pseudo-project dict for the djmanager service itself."""
    service  = getattr(settings, 'MANAGER_SERVICE_NAME', 'djmanager')
    mgr_dir  = str(settings.BASE_DIR)
    env_path = Path(settings.BASE_DIR) / '.env'

    env = {}
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, _, v = line.partition('=')
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                        v = v[1:-1]
                    env[k.strip()] = v
    except OSError:
        pass

    git_branch = git_hash = github_url = ''
    try:
        git_hash   = subprocess.check_output(
            ['git', '-C', mgr_dir, 'rev-parse', '--short', 'HEAD'],
            text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        git_branch = subprocess.check_output(
            ['git', '-C', mgr_dir, 'rev-parse', '--abbrev-ref', 'HEAD'],
            text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        github_url = subprocess.check_output(
            ['git', '-C', mgr_dir, 'remote', 'get-url', 'origin'],
            text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
    except Exception:
        pass

    debug_val = env.get('DEBUG', 'False')
    mode = 'dev' if debug_val.lower() in ('true', '1', 'yes') else 'prod'

    return {
        'PROJECTNAME':     service,
        'APPDIR':          mgr_dir,
        'MODE':            mode,
        'DEBUG':           debug_val,
        'DBTYPE':          'sqlite',
        'GUNICORN_PORT':   env.get('MANAGER_PORT', '8888'),
        'GITHUB_REPO_URL': github_url,
        'git_branch':      git_branch,
        'git_hash':        git_hash,
        'last_backup':     get_last_backup(service),
        'status':          get_service_status(service),
        '_is_manager':     True,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Auth: Login / Logout
# ──────────────────────────────────────────────────────────────────────────────

def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    error = None
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        # Check if account exists and is rate-limited
        try:
            profile = User.objects.get(username=username).userprofile
            if profile.is_locked():
                error = 'Konto vorübergehend gesperrt (zu viele fehlgeschlagene Versuche). Bitte 15 Minuten warten.'
                AuditLog.log(request, 'Login blockiert (Rate Limit)', details=username, success=False)
                return render(request, 'control/login.html', {'error': error})
        except (User.DoesNotExist, UserProfile.DoesNotExist):
            pass

        user = authenticate(request, username=username, password=password)

        if user:
            try:
                profile = user.userprofile
                profile.record_successful_login()
                profile.last_login_ip = request.META.get('REMOTE_ADDR', '')
                profile.save(update_fields=['last_login_ip'])
            except Exception:
                pass

            # Apply session timeout from SecuritySettings
            try:
                sec = SecuritySettings.get()
                if sec.session_timeout_hours > 0:
                    request.session.set_expiry(sec.session_timeout_hours * 3600)
                else:
                    request.session.set_expiry(0)  # browser session
            except Exception:
                pass

            # If 2FA is enabled → don't log in yet, redirect to verify
            try:
                if user.userprofile.totp_enabled:
                    request.session['2fa_user_id'] = user.pk
                    request.session['2fa_next']    = request.GET.get('next', '/dashboard/')
                    AuditLog.log(request, '2FA-Verify angefordert', details=username)
                    return redirect('two_factor_verify')
            except Exception:
                pass

            login(request, user)
            AuditLog.log(request, 'Login erfolgreich', details=username)
            return redirect(request.GET.get('next', 'dashboard'))

        # Failed login
        try:
            fail_user = User.objects.get(username=username)
            fail_user.userprofile.record_failed_login()
        except Exception:
            pass
        AuditLog.log(request, 'Login fehlgeschlagen', details=username, success=False)
        error = 'Ungültiger Benutzername oder Passwort.'

    return render(request, 'control/login.html', {'error': error})


def logout_view(request):
    AuditLog.log(request, 'Logout')
    request.session.pop('2fa_verified', None)
    logout(request)
    return redirect('login')


# ──────────────────────────────────────────────────────────────────────────────
# 2FA: Setup
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def two_factor_setup(request):
    """Show QR code to set up TOTP. Confirm with a valid code."""
    import pyotp, qrcode, base64
    from io import BytesIO

    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    error = None
    backup_codes = None
    qr_b64 = None
    tmp_secret = None

    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'generate':
            tmp_secret = pyotp.random_base32()
            request.session['totp_tmp_secret'] = tmp_secret

        elif action == 'confirm':
            tmp_secret = request.session.get('totp_tmp_secret', '')
            code = request.POST.get('code', '').strip().replace(' ', '')
            if not tmp_secret:
                error = 'Sitzung abgelaufen. Bitte neu starten.'
            elif not pyotp.TOTP(tmp_secret).verify(code, valid_window=1):
                error = 'Ungültiger Code. Bitte erneut versuchen.'
            else:
                profile.totp_secret = tmp_secret
                profile.totp_enabled = True
                backup_codes = profile.generate_backup_codes()
                profile.save()
                request.session['2fa_verified'] = True
                request.session.pop('totp_tmp_secret', None)
                AuditLog.log(request, '2FA aktiviert')
                return render(request, 'control/2fa_setup.html', {
                    'profile': profile,
                    'backup_codes': backup_codes,
                    'done': True,
                })

        elif action == 'disable':
            if not _get_role(request.user) == UserProfile.ROLE_ADMIN and \
               request.user != request.user:  # only self or admin
                pass
            profile.totp_enabled = False
            profile.totp_secret = ''
            profile.totp_backup_codes = ''
            profile.save()
            request.session.pop('2fa_verified', None)
            AuditLog.log(request, '2FA deaktiviert')
            messages.success(request, '2FA wurde deaktiviert.')
            return redirect('profile_view')

    else:
        action = 'generate'
        tmp_secret = pyotp.random_base32()
        request.session['totp_tmp_secret'] = tmp_secret

    # Generate QR code
    if not tmp_secret:
        tmp_secret = request.session.get('totp_tmp_secret', pyotp.random_base32())
    totp = pyotp.TOTP(tmp_secret)
    uri  = totp.provisioning_uri(
        name=request.user.username,
        issuer_name='DjangoMultiDeploy Manager',
    )
    img = qrcode.make(uri)
    buf = BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return render(request, 'control/2fa_setup.html', {
        'profile':     profile,
        'qr_b64':      qr_b64,
        'tmp_secret':  tmp_secret,
        'error':       error,
        'done':        False,
    })


# ──────────────────────────────────────────────────────────────────────────────
# 2FA: Verify (called after password login when 2FA is enabled)
# ──────────────────────────────────────────────────────────────────────────────

def two_factor_verify(request):
    import pyotp
    user_id = request.session.get('2fa_user_id')
    # Already logged in and verified
    if request.user.is_authenticated and request.session.get('2fa_verified'):
        return redirect('dashboard')

    error = None
    if request.method == 'POST':
        code = request.POST.get('code', '').strip().replace(' ', '')
        use_backup = request.POST.get('use_backup', '')

        # Determine the user to verify
        if user_id:
            try:
                verify_user = User.objects.get(pk=user_id)
            except User.DoesNotExist:
                return redirect('login')
        elif request.user.is_authenticated:
            verify_user = request.user
        else:
            return redirect('login')

        try:
            profile = verify_user.userprofile
        except Exception:
            return redirect('login')

        verified = False
        if use_backup:
            verified = profile.use_backup_code(code)
        else:
            verified = pyotp.TOTP(profile.totp_secret).verify(code, valid_window=1)

        if verified:
            request.session['2fa_verified'] = True
            if user_id:
                # Complete the login
                login(request, verify_user,
                      backend='django.contrib.auth.backends.ModelBackend')
                del request.session['2fa_user_id']
            AuditLog.log(request, '2FA verifiziert', details=verify_user.username)
            next_url = request.session.pop('2fa_next', '/dashboard/')
            return redirect(next_url)
        else:
            error = 'Ungültiger Code. Bitte erneut versuchen.'
            AuditLog.log(request, '2FA fehlgeschlagen', success=False,
                         details=verify_user.username if user_id else '')

    return render(request, 'control/2fa_verify.html', {'error': error})


# ──────────────────────────────────────────────────────────────────────────────
# Profile
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def profile_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    error = None
    success_msg = None

    if request.method == 'POST':
        action = request.POST.get('action', '')
        if action == 'change_password':
            old_pw  = request.POST.get('old_password', '')
            new_pw  = request.POST.get('new_password', '')
            new_pw2 = request.POST.get('new_password2', '')
            if not request.user.check_password(old_pw):
                error = 'Aktuelles Passwort falsch.'
            elif new_pw != new_pw2:
                error = 'Neue Passwörter stimmen nicht überein.'
            elif len(new_pw) < 10:
                error = 'Passwort muss mindestens 10 Zeichen haben.'
            else:
                request.user.set_password(new_pw)
                request.user.save()
                # Re-auth so session stays valid
                updated = authenticate(
                    request,
                    username=request.user.username,
                    password=new_pw,
                )
                if updated:
                    login(request, updated,
                          backend='django.contrib.auth.backends.ModelBackend')
                AuditLog.log(request, 'Passwort geändert')
                success_msg = 'Passwort erfolgreich geändert.'

    return render(request, 'control/profile.html', {
        'profile':     profile,
        'error':       error,
        'success_msg': success_msg,
        'role_label':  dict(UserProfile.ROLE_CHOICES).get(profile.role, profile.role),
    })


# ──────────────────────────────────────────────────────────────────────────────
# User management (admin only)
# ──────────────────────────────────────────────────────────────────────────────

@admin_required
def user_list(request):
    users = User.objects.all().select_related('userprofile').order_by('username')
    return render(request, 'control/user_list.html', {'users': users})


@admin_required
def user_create(request):
    import logging as _log, traceback as _tb
    _logger = _log.getLogger(__name__)
    error = None
    if request.method == 'POST':
        username  = request.POST.get('username', '').strip()
        email     = request.POST.get('email', '').strip()
        password  = request.POST.get('password', '')
        password2 = request.POST.get('password2', '')
        role      = request.POST.get('role', UserProfile.ROLE_VIEWER)

        assigned_projects = request.POST.getlist('projects')
        if not username:
            error = 'Benutzername darf nicht leer sein.'
        elif User.objects.filter(username=username).exists():
            error = f'Benutzer "{username}" existiert bereits.'
        elif password != password2:
            error = 'Passwörter stimmen nicht überein.'
        elif len(password) < 10:
            error = 'Passwort muss mindestens 10 Zeichen haben.'
        elif role not in [r[0] for r in UserProfile.ROLE_CHOICES]:
            error = 'Ungültige Rolle.'
        else:
            new_user = User.objects.create_user(
                username=username, email=email, password=password,
                is_staff=(role == UserProfile.ROLE_ADMIN),
            )
            profile, _ = UserProfile.objects.get_or_create(user=new_user)
            profile.role = role
            profile.save()
            for pname in assigned_projects:
                ProjectPermission.objects.get_or_create(user=new_user, project_name=pname)
            AuditLog.log(request, f'Benutzer erstellt: {username}',
                         details=f'Rolle: {role}, Projekte: {assigned_projects}')
            messages.success(request, f'Benutzer "{username}" erfolgreich erstellt.')
            return redirect('user_list')

    try:
        all_projects = get_all_projects()
        return render(request, 'control/user_form.html', {
            'action':          'create',
            'error':           error,
            'role_choices':    UserProfile.ROLE_CHOICES,
            'all_projects':    all_projects,
            'assigned_names':  set(request.POST.getlist('projects')),
        })
    except Exception:
        _logger.error('user_create render crashed:\n%s', _tb.format_exc())
        raise


@admin_required
def user_edit(request, uid):
    import logging as _log, traceback as _tb
    _logger = _log.getLogger(__name__)
    edit_user = get_object_or_404(User, pk=uid)
    try:
        profile, _ = UserProfile.objects.get_or_create(user=edit_user)
    except Exception:
        _logger.error('user_edit get_or_create crashed:\n%s', _tb.format_exc())
        raise
    error = None

    if request.method == 'POST':
        action = request.POST.get('action', 'save')

        if action == 'save':
            email     = request.POST.get('email', '').strip()
            role      = request.POST.get('role', profile.role)
            password  = request.POST.get('password', '')
            password2 = request.POST.get('password2', '')
            # Project assignments (list of project names from checkboxes)
            assigned_projects = request.POST.getlist('projects')

            if role not in [r[0] for r in UserProfile.ROLE_CHOICES]:
                error = 'Ungültige Rolle.'
            elif password and password != password2:
                error = 'Passwörter stimmen nicht überein.'
            elif password and len(password) < 10:
                error = 'Passwort muss mindestens 10 Zeichen haben.'
            else:
                edit_user.email    = email
                edit_user.is_staff = (role == UserProfile.ROLE_ADMIN)
                edit_user.save()
                profile.role = role
                profile.save()
                if password:
                    edit_user.set_password(password)
                    edit_user.save()
                # Update project permissions (only meaningful for non-admin)
                ProjectPermission.objects.filter(user=edit_user).delete()
                for pname in assigned_projects:
                    ProjectPermission.objects.get_or_create(
                        user=edit_user, project_name=pname)
                AuditLog.log(request, f'Benutzer bearbeitet: {edit_user.username}',
                             details=f'Rolle: {role}, Projekte: {assigned_projects}')
                messages.success(request, f'Benutzer "{edit_user.username}" gespeichert.')
                return redirect('user_list')

        elif action == 'disable_2fa':
            profile.totp_enabled = False
            profile.totp_secret  = ''
            profile.totp_backup_codes = ''
            profile.save()
            AuditLog.log(request, f'2FA zurückgesetzt für: {edit_user.username}')
            messages.success(request, '2FA zurückgesetzt.')
            return redirect('user_edit', uid=uid)

        elif action == 'unlock':
            profile.failed_logins = 0
            profile.locked_until  = None
            profile.save()
            AuditLog.log(request, f'Konto entsperrt: {edit_user.username}')
            messages.success(request, 'Konto entsperrt.')
            return redirect('user_edit', uid=uid)

    try:
        all_projects     = get_all_projects()
        assigned_names   = set(
            ProjectPermission.objects.filter(user=edit_user).values_list('project_name', flat=True)
        )
        return render(request, 'control/user_form.html', {
            'action':          'edit',
            'edit_user':       edit_user,
            'profile':         profile,
            'role_choices':    UserProfile.ROLE_CHOICES,
            'error':           error,
            'all_projects':    all_projects,
            'assigned_names':  assigned_names,
        })
    except Exception:
        _logger.error('user_edit render crashed:\n%s', _tb.format_exc())
        raise


@require_POST
@admin_required
def user_delete(request, uid):
    target = get_object_or_404(User, pk=uid)
    if target == request.user:
        messages.error(request, 'Sie können sich nicht selbst löschen.')
        return redirect('user_list')
    name = target.username
    target.delete()
    AuditLog.log(request, f'Benutzer gelöscht: {name}')
    messages.success(request, f'Benutzer "{name}" gelöscht.')
    return redirect('user_list')


# ──────────────────────────────────────────────────────────────────────────────
# Audit Log
# ──────────────────────────────────────────────────────────────────────────────

@admin_required
def audit_log_view(request):
    logs = AuditLog.objects.select_related('user').all()[:500]
    return render(request, 'control/audit_log.html', {'logs': logs})


# ──────────────────────────────────────────────────────────────────────────────
# Security Settings (admin only)
# ──────────────────────────────────────────────────────────────────────────────

@admin_required
def security_settings_view(request):
    from .middleware import invalidate_whitelist_cache
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


# ──────────────────────────────────────────────────────────────────────────────
# Manager-Einstellungen: ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS (admin only)
# ──────────────────────────────────────────────────────────────────────────────

@admin_required
def manager_settings_view(request):
    """Read/write ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS in manager .env.
    Changes take effect immediately in-memory (no restart required)."""
    import re
    from pathlib import Path
    from django.conf import settings as djsettings

    env_path = Path(djsettings.BASE_DIR) / '.env'

    def _read_env():
        result = {}
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, _, v = line.partition('=')
                        v = v.strip()
                        # Strip matching outer quotes (same as python-dotenv)
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
        """Sync server_name in /etc/nginx/sites-available/djmanager with current hosts."""
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
            pass  # non-fatal

    error = None
    success_msg = None

    if request.method == 'POST':
        action = request.POST.get('action', '')
        host = request.POST.get('host', '').strip().strip('/')

        if host and not re.match(r'^[\w.\-:\[\]*]+$', host):
            error = 'Ungültiger Hostname — erlaubt: Buchstaben, Ziffern, .-:[]'
            host = ''

        if not error and host:
            env = _read_env()
            cur_hosts = [h.strip() for h in env.get('ALLOWED_HOSTS', '').split(',') if h.strip()]
            cur_csrf  = [c.strip() for c in env.get('CSRF_TRUSTED_ORIGINS', '').split(',') if c.strip()]

            def _schedule_restart():
                _svc = getattr(djsettings, 'MANAGER_SERVICE_NAME', 'djmanager')
                subprocess.Popen(
                    ['bash', '-c', f'sleep 2 && systemctl restart {_svc}'],
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
                    env['ALLOWED_HOSTS'] = ','.join(cur_hosts)
                    env['CSRF_TRUSTED_ORIGINS'] = ','.join(cur_csrf)
                    _write_env(env)
                    _update_nginx_server_names(cur_hosts)
                    # Kein in-memory Update — bei mehreren Gunicorn-Workern würde
                    # nur der aktuelle Worker aktualisiert. Service-Neustart sorgt
                    # dafür, dass alle Worker die neue .env einlesen.
                    _schedule_restart()
                    AuditLog.log(request, 'Manager: Host hinzugefügt', details=host)
                    success_msg = f'Host "{host}" hinzugefügt. Service wird neu gestartet…'

            elif action == 'remove':
                if host not in cur_hosts:
                    error = f'"{host}" nicht gefunden.'
                else:
                    cur_hosts = [h for h in cur_hosts if h != host]
                    # Remove ALL CSRF entries for this host (http/https, with/without port)
                    host_esc = re.escape(host)
                    cur_csrf  = [c for c in cur_csrf
                                 if not re.match(rf'^https?://{host_esc}(?:[:/]|$)', c)]
                    env['ALLOWED_HOSTS'] = ','.join(cur_hosts)
                    env['CSRF_TRUSTED_ORIGINS'] = ','.join(cur_csrf)
                    _write_env(env)
                    _update_nginx_server_names(cur_hosts)
                    _schedule_restart()
                    AuditLog.log(request, 'Manager: Host entfernt', details=host)
                    success_msg = f'Host "{host}" entfernt. Service wird neu gestartet…'

    env = _read_env()
    allowed_hosts = [h.strip() for h in env.get('ALLOWED_HOSTS', '').split(',') if h.strip()]
    csrf_origins  = [c.strip() for c in env.get('CSRF_TRUSTED_ORIGINS', '').split(',') if c.strip()]

    return render(request, 'control/manager_settings.html', {
        'allowed_hosts': allowed_hosts,
        'csrf_origins':  csrf_origins,
        'error':         error,
        'success_msg':   success_msg,
    })



# ──────────────────────────────────────────────────────────────────────────────
# .env Editor (admin only)
# ──────────────────────────────────────────────────────────────────────────────

@admin_required
def manager_env_view(request):
    """Read/write the manager's .env file directly via the web UI."""
    from django.conf import settings as djsettings

    env_path = Path(djsettings.BASE_DIR) / '.env'
    error = None
    success_msg = None

    if request.method == 'POST':
        new_content = request.POST.get('env_content', '')
        try:
            with open(env_path, 'w') as f:
                f.write(new_content)
            os.chmod(env_path, 0o600)
            svc = getattr(djsettings, 'MANAGER_SERVICE_NAME', 'djmanager')
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
        'page_title':    f'{name} — .env bearbeiten',
        'env_content':   env_content,
        'back_url':      'project_detail',
        'back_url_kwarg': name,
        'back_label':    f'← {name}',
        'save_url':      'project_env',
        'save_url_kwarg': name,
        'error':         error,
        'success_msg':   success_msg,
    })


# ──────────────────────────────────────────────────────────────────────────────
# Firewall (ufw) Port-Verwaltung (admin only)
# ──────────────────────────────────────────────────────────────────────────────

_SERVICE_FILE = '/etc/systemd/system/djmanager.service'

def _set_manager_bind(host):
    """Switch Gunicorn bind address in djmanager.service and restart the service."""
    import re as _re
    try:
        with open(_SERVICE_FILE, 'r') as fh:
            content = fh.read()
        new_content = _re.sub(
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
            # Benutzer trägt Port manuell ein
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
                # Port 8888 (Manager-Gunicorn): Bind-Adresse anpassen damit direkter
                # Zugriff über IP:8888 möglich ist wenn der Port geöffnet wird.
                if port == '8888' and proto == 'tcp':
                    _set_manager_bind('0.0.0.0' if action == 'allow' else '127.0.0.1')
            else:
                error = msg

    ufw_status   = get_ufw_status()
    port_rules   = get_ufw_port_rules()
    known_ports  = [
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


# ──────────────────────────────────────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def dashboard(request):
    import logging, traceback
    _log = logging.getLogger(__name__)
    try:
        all_projects = get_all_projects()
        allowed      = _allowed_projects(request.user)
        if allowed is not None:
            all_projects = [p for p in all_projects if p.get('PROJECTNAME') in allowed]
        for p in all_projects:
            p['last_backup'] = get_last_backup(p.get('PROJECTNAME', ''))
        ufw          = get_ufw_status()
        server_stats = get_server_stats()
        role         = _get_role(request.user)
        allowed_hosts = [h for h in settings.ALLOWED_HOSTS if h != '*']
        manager_info  = _get_manager_info() if role in (
            UserProfile.ROLE_ADMIN, UserProfile.ROLE_OPERATOR) else None
        # Attach favorite commands to each project dict
        project_names = [p.get('PROJECTNAME') for p in all_projects if p.get('PROJECTNAME')]
        fav_qs = FavoriteCommand.objects.filter(project_name__in=project_names)
        fav_by_project = {}
        for fav in fav_qs:
            fav_by_project.setdefault(fav.project_name, []).append(fav)
        for p in all_projects:
            p['favorite_commands'] = fav_by_project.get(p.get('PROJECTNAME', ''), [])
        return render(request, 'control/dashboard.html', {
            'projects':      all_projects,
            'ufw':           ufw,
            'server_stats':  server_stats,
            'role':          role,
            'allowed_hosts': allowed_hosts,
            'manager_info':  manager_info,
        })
    except Exception:
        _log.error('dashboard() crashed:\n%s', traceback.format_exc())
        raise


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
    with open(log_path, 'w') as log_f:
        subprocess.Popen(
            ['bash', settings.INSTALL_SCRIPT],
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

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


# ──────────────────────────────────────────────────────────────────────────────
# Deploy Key Registry (global list + CRUD)
# ──────────────────────────────────────────────────────────────────────────────

@admin_required
def deploy_keys_list(request):
    keys = list_deploy_keys()
    all_projects = get_all_projects()
    return render(request, 'control/deploy_keys_list.html', {
        'keys': keys,
        'all_projects': all_projects,
    })


@admin_required
def deploy_key_detail(request, key_id):
    """Show the public key for a registry entry."""
    from .utils import _load_key_registry
    pub_key, error = get_deploy_key_pubkey(key_id)
    meta = _load_key_registry().get(key_id, {'id': key_id, 'label': key_id})
    if request.GET.get('download') and pub_key:
        label = meta.get('label', key_id).replace(' ', '_')
        resp  = HttpResponse(pub_key + '\n', content_type='text/plain')
        resp['Content-Disposition'] = f'attachment; filename="{label}_{key_id}.pub"'
        return resp
    return render(request, 'control/deploy_key_detail.html', {
        'key_id':  key_id,
        'meta':    meta,
        'pub_key': pub_key,
        'error':   error,
    })


@require_POST
@admin_required
def deploy_key_create(request):
    from datetime import datetime
    label = request.POST.get('label', '').strip()
    if not label:
        label = f'Key {datetime.now().strftime("%Y-%m-%d")}'
    key_id, _pub, error = create_deploy_key(label)
    if error:
        return JsonResponse({'ok': False, 'error': error}, status=400)
    # If a project was requested, auto-assign and redirect to its key page
    project = request.POST.get('project', '').strip()
    if project and key_id:
        assign_project_deploy_key(project, key_id)
        return redirect('project_deploy_key', project=project)
    return redirect('deploy_keys_list')


@require_POST
@admin_required
def deploy_key_delete(request, key_id):
    ok, error = delete_deploy_key(key_id)
    if not ok:
        # Return to list with an error message via query param
        from urllib.parse import urlencode
        return redirect(f"{reverse('deploy_keys_list')}?error={error}")
    return redirect('deploy_keys_list')


@require_POST
@login_required
def project_assign_key(request, project):
    if not _check_project_access(request.user, project):
        return render(request, 'control/403.html', status=403)
    key_id = request.POST.get('key_id', '').strip()
    ok, error = assign_project_deploy_key(project, key_id)
    if not ok:
        return JsonResponse({'ok': False, 'error': error}, status=400)
    return redirect('project_deploy_key', project=project)


# ──────────────────────────────────────────────────────────────────────────────
# Per-Project GitHub Deploy Key
# ──────────────────────────────────────────────────────────────────────────────

def _github_keys_url(conf):
    """Return direct URL to GitHub repo deploy keys page, or None."""
    import re as _re
    url = conf.get('GITHUB_REPO_URL') or conf.get('GITHUB', '')
    if not url:
        return None
    m = _re.search(r'[:/]([^/:]+/[^/]+?)(?:\.git)?$', url)
    return f'https://github.com/{m.group(1)}/settings/keys' if m else None


@login_required
def project_deploy_key(request, project):
    if not _check_project_access(request.user, project):
        return render(request, 'control/403.html', status=403)
    conf = get_project(project)
    pub_key, error = get_project_deploy_key(project)
    all_keys = list_deploy_keys()
    current_key_id = (conf or {}).get('DEPLOY_KEY_ID', '').strip()
    return render(request, 'control/project_deploy_key.html', {
        'project':         project,
        'pub_key':         pub_key,
        'error':           error,
        'conf':            conf,
        'github_keys_url': _github_keys_url(conf or {}),
        'all_keys':        all_keys,
        'current_key_id':  current_key_id,
    })


@login_required
def project_deploy_key_download(request, project):
    if not _check_project_access(request.user, project):
        raise Http404()
    pub_key, error = get_project_deploy_key(project)
    if error:
        raise Http404(error)
    response = HttpResponse(pub_key + '\n', content_type='text/plain')
    response['Content-Disposition'] = f'attachment; filename="deploy_{project}_ed25519.pub"'
    return response


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
    return render(request, 'control/project_detail.html', {
        'conf':              conf,
        'name':              name,
        'backups':           backups,
        'allowed_hosts':     allowed_hosts,
        'nginx_names':       nginx_names,
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
    return render(request, 'control/project_detail.html', {
        'conf':              conf,
        'name':              name,
        'backups':           backups,
        'allowed_hosts':     allowed_hosts,
        'nginx_names':       nginx_names,
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
def manager_security_scan(request):
    """Run pip-audit + manage.py check --deploy on the manager itself."""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)
    pip_results   = run_manager_pip_audit()
    deploy_issues = run_manager_deploy_check()
    return JsonResponse({
        'pip_audit':    pip_results,
        'deploy_check': deploy_issues,
    })


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
    from .utils import remove_project
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


# ──────────────────────────────────────────────────────────────────────────────
# Manager self-management (action + update)
# ──────────────────────────────────────────────────────────────────────────────

@require_POST
@operator_required
def manager_action(request):
    """Start / stop / restart the djmanager service itself."""
    action  = request.POST.get('action', '')
    if action not in ('start', 'stop', 'restart'):
        return JsonResponse({'ok': False, 'message': 'Ungültige Aktion'})
    service = getattr(settings, 'MANAGER_SERVICE_NAME', 'djmanager')
    # Use a delayed restart so the response is sent before the service dies
    if action in ('restart', 'stop'):
        subprocess.Popen(
            ['bash', '-c', f'sleep 1 && systemctl {action} {service}'],
            close_fds=True, start_new_session=True,
        )
        AuditLog.log(request, f'Manager-Service {action}', success=True)
        return JsonResponse({'ok': True, 'message': f'Service wird {action}ed…'})
    ok, output = service_action(service, action)
    AuditLog.log(request, f'Manager-Service {action}', success=ok)
    return JsonResponse({'ok': ok, 'message': output or ('OK' if ok else 'Fehler')})


@require_POST
@admin_required
def manager_update(request):
    """Run djmanager_update.sh asynchronously (git pull + service restart)."""
    service = getattr(settings, 'MANAGER_SERVICE_NAME', 'djmanager')
    script  = f'/usr/local/bin/{service}_update.sh'
    if not os.path.exists(script):
        return JsonResponse({'ok': False, 'output': f'Update-Skript nicht gefunden: {script}'})

    # Sicherstellen dass rsync verfügbar ist; falls nicht, cp-Fallback einpatchen
    _patch_update_script_rsync_fallback(script)

    log_path = f'/tmp/{service}_update_{int(time.time())}.log'
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
    1. pull.rebase false + --ff-only Fallback (kein divergent-branches-Fehler)
    2. git stash vor git pull (lokale Änderungen blockieren nicht)
    3. rsync → cp (kein rsync nötig)
    """
    try:
        with open(script_path) as f:
            content = f.read()

        changed = False

        # Patch 1: pull.rebase false konfigurieren
        if 'pull.rebase false' not in content:
            old = '  git config --global --add safe.directory "$SCRIPT_DIR" 2>/dev/null || true\n'
            new = (
                '  git config --global --add safe.directory "$SCRIPT_DIR" 2>/dev/null || true\n'
                '  git config --global pull.rebase false 2>/dev/null || true\n'
            )
            if old in content:
                content = content.replace(old, new, 1)
                changed = True

        # Patch 2: git stash vor git pull
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

        # Patch 3: rsync → cp (rsync nicht überall verfügbar)
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
