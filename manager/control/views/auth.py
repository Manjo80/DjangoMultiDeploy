"""Authentication, 2FA setup / verify, and profile views."""
import logging

from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages

from ..models import UserProfile, AuditLog, SecuritySettings
from ._helpers import _get_role

logger = logging.getLogger('djmanager.views.auth')


# ── Login / Logout ────────────────────────────────────────────────────────────

def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    error = None
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

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

            try:
                sec = SecuritySettings.get()
                if sec.session_timeout_hours > 0:
                    request.session.set_expiry(sec.session_timeout_hours * 3600)
                else:
                    request.session.set_expiry(0)
            except Exception:
                pass

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

        verified = (
            profile.use_backup_code(code)
            if use_backup
            else pyotp.TOTP(profile.totp_secret).verify(code, valid_window=1)
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
            elif len(new_pw) < 10:
                error = 'Passwort muss mindestens 10 Zeichen haben.'
            else:
                request.user.set_password(new_pw)
                request.user.save()
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
