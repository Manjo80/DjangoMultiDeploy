"""User management views (admin only)."""
import logging
import traceback

from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.contrib.auth.models import User

from ..models import UserProfile, AuditLog, ProjectPermission
from ..utils import get_all_projects
from ._helpers import admin_required

logger = logging.getLogger('djmanager.views.users')


@admin_required
def user_list(request):
    users = User.objects.all().select_related('userprofile').order_by('username')
    return render(request, 'control/user_list.html', {'users': users})


@admin_required
def user_create(request):
    error = None
    if request.method == 'POST':
        username          = request.POST.get('username', '').strip()
        email             = request.POST.get('email', '').strip()
        password          = request.POST.get('password', '')
        password2         = request.POST.get('password2', '')
        role              = request.POST.get('role', UserProfile.ROLE_VIEWER)
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
            'action':         'create',
            'error':          error,
            'role_choices':   UserProfile.ROLE_CHOICES,
            'all_projects':   all_projects,
            'assigned_names': set(request.POST.getlist('projects')),
        })
    except Exception:
        logger.error('user_create render crashed:\n%s', traceback.format_exc())
        raise


@admin_required
def user_edit(request, uid):
    edit_user = get_object_or_404(User, pk=uid)
    try:
        profile, _ = UserProfile.objects.get_or_create(user=edit_user)
    except Exception:
        logger.error('user_edit get_or_create crashed:\n%s', traceback.format_exc())
        raise
    error = None

    if request.method == 'POST':
        action = request.POST.get('action', 'save')

        if action == 'save':
            email             = request.POST.get('email', '').strip()
            role              = request.POST.get('role', profile.role)
            password          = request.POST.get('password', '')
            password2         = request.POST.get('password2', '')
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
                ProjectPermission.objects.filter(user=edit_user).delete()
                for pname in assigned_projects:
                    ProjectPermission.objects.get_or_create(
                        user=edit_user, project_name=pname)
                AuditLog.log(request, f'Benutzer bearbeitet: {edit_user.username}',
                             details=f'Rolle: {role}, Projekte: {assigned_projects}')
                messages.success(request, f'Benutzer "{edit_user.username}" gespeichert.')
                return redirect('user_list')

        elif action == 'disable_2fa':
            profile.totp_enabled      = False
            profile.totp_secret       = ''
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
        all_projects   = get_all_projects()
        assigned_names = set(
            ProjectPermission.objects.filter(user=edit_user).values_list('project_name', flat=True)
        )
        return render(request, 'control/user_form.html', {
            'action':         'edit',
            'edit_user':      edit_user,
            'profile':        profile,
            'role_choices':   UserProfile.ROLE_CHOICES,
            'error':          error,
            'all_projects':   all_projects,
            'assigned_names': assigned_names,
        })
    except Exception:
        logger.error('user_edit render crashed:\n%s', traceback.format_exc())
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
