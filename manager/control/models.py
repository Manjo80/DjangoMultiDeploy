"""
DjangoMultiDeploy Manager — Models
"""
import json
import secrets

from django.contrib.auth.models import User
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from datetime import timedelta


# ──────────────────────────────────────────────────────────────────────────────
# UserProfile — roles + 2FA + login rate limiting
# ──────────────────────────────────────────────────────────────────────────────

class UserProfile(models.Model):
    ROLE_ADMIN    = 'admin'
    ROLE_OPERATOR = 'operator'
    ROLE_VIEWER   = 'viewer'
    ROLE_CHOICES  = [
        (ROLE_ADMIN,    'Administrator'),
        (ROLE_OPERATOR, 'Operator'),
        (ROLE_VIEWER,   'Viewer (read-only)'),
    ]

    user              = models.OneToOneField(User, on_delete=models.CASCADE)
    role              = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_VIEWER)
    totp_secret       = models.CharField(max_length=64, blank=True)
    totp_enabled      = models.BooleanField(default=False)
    totp_backup_codes = models.TextField(blank=True)   # JSON list of unused one-time codes
    failed_logins     = models.IntegerField(default=0)
    locked_until      = models.DateTimeField(null=True, blank=True)
    last_login_ip     = models.GenericIPAddressField(null=True, blank=True)

    # ── 2FA helpers ──────────────────────────────────────────────────────────

    def get_backup_codes(self):
        if not self.totp_backup_codes:
            return []
        try:
            return json.loads(self.totp_backup_codes)
        except Exception:
            return []

    def generate_backup_codes(self, count=8):
        """Generate fresh backup codes, store them, and return the plain list."""
        codes = [secrets.token_hex(4).upper() for _ in range(count)]
        self.totp_backup_codes = json.dumps(codes)
        return codes

    def use_backup_code(self, code):
        """Consume a backup code. Returns True on success."""
        code = code.upper().replace('-', '').strip()
        codes = self.get_backup_codes()
        if code in codes:
            codes.remove(code)
            self.totp_backup_codes = json.dumps(codes)
            self.save(update_fields=['totp_backup_codes'])
            return True
        return False

    # ── Login rate limiting ───────────────────────────────────────────────────

    def is_locked(self):
        return bool(self.locked_until and self.locked_until > timezone.now())

    def record_failed_login(self):
        self.failed_logins = (self.failed_logins or 0) + 1
        if self.failed_logins >= 5:
            self.locked_until = timezone.now() + timedelta(minutes=15)
        self.save(update_fields=['failed_logins', 'locked_until'])

    def record_successful_login(self):
        self.failed_logins = 0
        self.locked_until = None
        self.save(update_fields=['failed_logins', 'locked_until'])

    def __str__(self):
        return f'{self.user.username} ({self.role})'


@receiver(post_save, sender=User)
def _auto_create_profile(sender, instance, created, **kwargs):
    if created:
        try:
            UserProfile.objects.get_or_create(user=instance)
        except Exception:
            # Table may not exist yet during initial migration
            pass


# ──────────────────────────────────────────────────────────────────────────────
# AuditLog — who did what when
# ──────────────────────────────────────────────────────────────────────────────

class AuditLog(models.Model):
    timestamp  = models.DateTimeField(auto_now_add=True)
    user       = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    username   = models.CharField(max_length=150, blank=True)  # kept even after user deletion
    action     = models.CharField(max_length=200)
    project    = models.CharField(max_length=100, blank=True)
    details    = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    success    = models.BooleanField(default=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f'[{self.timestamp:%Y-%m-%d %H:%M}] {self.username}: {self.action}'

    @classmethod
    def log(cls, request, action, project='', details='', success=True):
        import logging
        ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', ''))
        if ip and ',' in ip:
            ip = ip.split(',')[0].strip()
        user = request.user if request.user.is_authenticated else None
        try:
            cls.objects.create(
                user=user,
                username=user.username if user else 'anonymous',
                action=action,
                project=project,
                details=str(details)[:2000],
                ip_address=ip[:45] if ip else None,
                success=success,
            )
        except Exception as e:
            logging.getLogger(__name__).error('AuditLog.log failed: %s', e, exc_info=True)


# ──────────────────────────────────────────────────────────────────────────────
# SecuritySettings — singleton for manager-wide security config
# ──────────────────────────────────────────────────────────────────────────────

class SecuritySettings(models.Model):
    """Singleton row (pk=1). Never delete."""
    ip_whitelist          = models.TextField(
        blank=True,
        help_text='Erlaubte IPs/CIDRs, eine pro Zeile. Leer = alle erlaubt.',
    )
    require_2fa           = models.BooleanField(
        default=False,
        help_text='Zwingt alle Nutzer zur 2FA-Einrichtung.',
    )
    session_timeout_hours = models.IntegerField(
        default=8,
        help_text='Session-Ablauf in Stunden (0 = Browser-Session).',
    )

    class Meta:
        verbose_name        = 'Security Settings'
        verbose_name_plural = 'Security Settings'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def get_ip_whitelist(self):
        """Return list of IP strings (empty = no restriction)."""
        return [ip.strip() for ip in self.ip_whitelist.splitlines() if ip.strip()]

    def __str__(self):
        return 'Security Settings'


# ──────────────────────────────────────────────────────────────────────────────
# FavoriteCommand — per-project quick-access management commands
# ──────────────────────────────────────────────────────────────────────────────

class FavoriteCommand(models.Model):
    """
    A management command shortcut for a specific project.
    Shown as a quick-action button next to Start/Stop/Restart.
    """
    project_name = models.CharField(max_length=100)
    label        = models.CharField(max_length=80,
                                    help_text='Button-Beschriftung, z.B. "Migrate"')
    command      = models.CharField(max_length=500,
                                    help_text='manage.py Unterbefehl, z.B. "migrate" oder "load_glossary"')
    order        = models.IntegerField(default=0, help_text='Sortierung (aufsteigend)')

    class Meta:
        ordering        = ['project_name', 'order', 'label']
        unique_together = ('project_name', 'command')

    def __str__(self):
        return f'{self.project_name}: {self.label} ({self.command})'


# ──────────────────────────────────────────────────────────────────────────────
# ProjectPermission — per-project access for Operator/Viewer users
# ──────────────────────────────────────────────────────────────────────────────

class ProjectPermission(models.Model):
    """
    Restricts non-admin users to specific projects.
    Admin users always see all projects (no entry needed).
    If a non-admin user has NO entries here they see NO projects.
    """
    user         = models.ForeignKey(User, on_delete=models.CASCADE,
                                     related_name='project_permissions')
    project_name = models.CharField(max_length=100)

    class Meta:
        unique_together = ('user', 'project_name')
        ordering = ['project_name']

    def __str__(self):
        return f'{self.user.username} → {self.project_name}'
