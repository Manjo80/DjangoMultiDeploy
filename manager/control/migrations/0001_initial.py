from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='UserProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(
                    choices=[('admin', 'Administrator'), ('operator', 'Operator'), ('viewer', 'Viewer (read-only)')],
                    default='viewer', max_length=20)),
                ('totp_secret', models.CharField(blank=True, max_length=64)),
                ('totp_enabled', models.BooleanField(default=False)),
                ('totp_backup_codes', models.TextField(blank=True)),
                ('failed_logins', models.IntegerField(default=0)),
                ('locked_until', models.DateTimeField(blank=True, null=True)),
                ('last_login_ip', models.GenericIPAddressField(blank=True, null=True)),
                ('user', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='AuditLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('timestamp', models.DateTimeField(auto_now_add=True)),
                ('username', models.CharField(blank=True, max_length=150)),
                ('action', models.CharField(max_length=200)),
                ('project', models.CharField(blank=True, max_length=100)),
                ('details', models.TextField(blank=True)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('success', models.BooleanField(default=True)),
                ('user', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-timestamp']},
        ),
        migrations.CreateModel(
            name='SecuritySettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('ip_whitelist', models.TextField(
                    blank=True,
                    help_text='Erlaubte IPs/CIDRs, eine pro Zeile. Leer = alle erlaubt.')),
                ('require_2fa', models.BooleanField(
                    default=False,
                    help_text='Zwingt alle Nutzer zur 2FA-Einrichtung.')),
                ('session_timeout_hours', models.IntegerField(
                    default=8,
                    help_text='Session-Ablauf in Stunden (0 = Browser-Session).')),
            ],
            options={
                'verbose_name': 'Security Settings',
                'verbose_name_plural': 'Security Settings',
            },
        ),
        migrations.CreateModel(
            name='ProjectPermission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('project_name', models.CharField(max_length=100)),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='project_permissions',
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['project_name']},
        ),
        migrations.AlterUniqueTogether(
            name='projectpermission',
            unique_together={('user', 'project_name')},
        ),
    ]
