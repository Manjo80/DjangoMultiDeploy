"""Authentication, 2FA setup / verify, and profile views."""
import logging
from datetime import timedelta

from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.models import User
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import UserProfile, AuditLog, SecuritySettings
from ..middleware import get_client_ip
from ._helpers import _get_role

logger = logging.getLogger('djmanager.views.auth')

# Generic message for any throttling case — does NOT reveal whether the
# account exists or is specifically locked (prevents username enumeration).
_GENERIC_THROTTLE_MSG = (
    'Zu viele fehlgeschlagene Anmeldeversuche. Bitte einige Minuten warten.'
)
# Per-IP throttle: block after this many failed logins from one IP in the window.
_IP_FAIL_LIMIT = 15
_IP_FAIL_WINDOW_MIN = 15


# ── Login / Logout ────────────────────────────────────────────────────────────

def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    error = None
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        # Per-IP throttle (shared across workers via the audit log in the DB).
        client_ip = get_client_ip(request)
        if client_ip:
            since = timezone.now() - timedelta(minutes=_IP_FAIL_WINDOW_MIN)
            recent_ip_fails = AuditLog.objects.filter(
                action='Login fehlgeschlagen', success=False,
                ip_address=client_ip, timestamp__gte=since,
            ).count()
            if recent_ip_fails >= _IP_FAIL_LIMIT:
                AuditLog.log(request, 'Login blockiert (IP Rate Limit)',
                             details=username, success=False)
                return render(request, 'control/login.html',
                              {'error': _GENERIC_THROTTLE_MSG})

        # Per-account lock. Use the SAME generic message as the IP throttle so a
        # locked account is indistinguishable from a wrong password.
        try:
            profile = User.objects.get(username=username).userprofile
            if profile.is_locked():
                AuditLog.log(request, 'Login blockiert (Rate Limit)', details=username, success=False)
                return render(request, 'control/login.html', {'error': _GENERIC_THROTTLE_MSG})
        except (User.DoesNotExist, UserProfile.DoesNotExist):
            pass

        user = authenticate(request, username=username, password=password)

        if user:
            # Resolve the profile up front; we must know the 2FA status reliably.
            try:
                profile = user.userprofile
            except Exception:
                profile = None

            if profile is not None:
                try:
                    profile.record_successful_login()
                    profile.last_login_ip = request.META.get('REMOTE_ADDR', '')
                    profile.save(update_fields=['last_login_ip'])
                except Exception:
                    pass

            try:
                sec = SecuritySettings.get()
                if sec.session_timeout_hours > 0:
                    request.session.set_expiry(sec.session_timeout_hours * 3600)
                else:
                    request.session.set_expiry(0)
            except Exception:
                pass

            # 2FA gate — fail CLOSED. The redirect is NOT wrapped in a bare
            # try/except: a 2FA-enabled account must never fall through to
            # login() without completing the second factor.
            if profile is not None and profile.totp_enabled:
                request.session['2fa_user_id'] = user.pk
                request.session['2fa_next']    = request.GET.get('next', '/dashboard/')
                request.session.pop('2fa_verified', None)
                AuditLog.log(request, '2FA-Verify angefordert', details=username)
                return redirect('two_factor_verify')

            login(request, user)
            AuditLog.log(request, 'Login erfolgreich', details=username)
            return redirect(request.GET.get('next', 'dashboard'))

        try:
            fail_user = User.objects.get(username=username)
            fail_user.userprofile.record_failed_login()
        except Exception:
            pass
        AuditLog.log(request, 'Login fehlgeschlagen', details=username, success=False)
        error = 'Ungültiger Benutzername oder Passwort.'

    return render(request, 'control/login.html', {'error': error})


@require_POST
def logout_view(request):
    AuditLog.log(request, 'Logout')
    request.session.pop('2fa_verified', None)
    logout(request)
    return redirect('login')


# ── 2FA Setup ─────────────────────────────────────────────────────────────────

@login_required
def two_factor_setup(request):
    """Show QR code to set up TOTP. Confirm with a valid code."""
    import pyotp, qrcode, base64
    from io import BytesIO

    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    error = None
    backup_codes = None
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
                profile.set_totp_secret(tmp_secret)
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
            # Nur der eigene Account darf 2FA deaktivieren; Admins können es
            # für beliebige Nutzer über die Benutzerverwaltung zurücksetzen.
            profile.totp_enabled = False
            profile.totp_secret = ''
            profile.totp_backup_codes = ''
            profile.save()
            request.session.pop('2fa_verified', None)
            AuditLog.log(request, '2FA deaktiviert')
            messages.success(request, '2FA wurde deaktiviert.')
            return redirect('profile_view')

    else:
        tmp_secret = pyotp.random_base32()
        request.session['totp_tmp_secret'] = tmp_secret

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
        'profile':    profile,
        'qr_b64':     qr_b64,
        'tmp_secret': tmp_secret,
        'error':      error,
        'done':       False,
    })


# ── 2FA Verify ────────────────────────────────────────────────────────────────

def two_factor_verify(request):
    import pyotp
    user_id = request.session.get('2fa_user_id')
    if request.user.is_authenticated and request.session.get('2fa_verified'):
        return redirect('dashboard')

    error = None
    if request.method == 'POST':
        code       = request.POST.get('code', '').strip().replace(' ', '')
        use_backup = request.POST.get('use_backup', '')

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

        # Fail closed: never verify against a disabled or empty TOTP config.
        secret = profile.get_totp_secret()
        if not profile.totp_enabled or (not use_backup and not secret):
            AuditLog.log(request, '2FA-Verify ohne aktives 2FA abgelehnt',
                         success=False, details=verify_user.username)
            return redirect('login')

        verified = (
            profile.use_backup_code(code)
            if use_backup
            else pyotp.TOTP(secret).verify(code, valid_window=1)
        )

        if verified:
            request.session['2fa_verified'] = True
            if user_id:
                login(request, verify_user,
                      backend='django.contrib.auth.backends.ModelBackend')
                del request.session['2fa_user_id']
            AuditLog.log(request, '2FA verifiziert', details=verify_user.username)
            next_url = request.session.pop('2fa_next', '/dashboard/')
            return redirect(next_url)

        error = 'Ungültiger Code. Bitte erneut versuchen.'
        AuditLog.log(request, '2FA fehlgeschlagen', success=False,
                     details=verify_user.username if user_id else '')

    return render(request, 'control/2fa_verify.html', {'error': error})


# ── Profile ───────────────────────────────────────────────────────────────────

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
            else:
                try:
                    validate_password(new_pw, user=request.user)
                except ValidationError as exc:
                    error = ' '.join(exc.messages)
                else:
                    request.user.set_password(new_pw)
                    request.user.save()
                    # Keep the current session valid after the password hash
                    # change; other existing sessions are invalidated by Django's
                    # SessionAuthenticationMiddleware on their next request.
                    update_session_auth_hash(request, request.user)
                    AuditLog.log(request, 'Passwort geändert')
                    success_msg = 'Passwort erfolgreich geändert.'

    return render(request, 'control/profile.html', {
        'profile':     profile,
        'error':       error,
        'success_msg': success_msg,
        'role_label':  dict(UserProfile.ROLE_CHOICES).get(profile.role, profile.role),
    })
