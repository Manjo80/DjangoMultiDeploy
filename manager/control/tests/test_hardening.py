"""Regression tests for the security audit fixes (roles, 2FA, client IP, hosts)."""
from unittest import mock

import pyotp
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from control.middleware import get_client_ip
from control.models import ProjectPermission, UserProfile
from control.utils import pip_utils
from control.views import projects as project_views

PW = 'Corr3ct-Horse-99'


def _user(name, role):
    user = User.objects.create_user(name, password=PW)
    user.userprofile.role = role
    user.userprofile.save()
    return user


class AdminOnlyEndpointsTests(TestCase):
    """Private SSH keys, install logs and manager control are admin-only."""

    def test_viewer_and_operator_are_refused(self):
        for role in (UserProfile.ROLE_VIEWER, UserProfile.ROLE_OPERATOR):
            self.client.force_login(_user(f'u_{role}', role))
            for method, url in (
                ('get', '/install/ssh-key/demo/'),
                ('get', '/install/ssh-key/demo/download/'),
                ('post', '/install/ssh-key/demo/confirm/'),
                ('get', '/install/poll/demo_abc123.log/'),
                ('get', '/install/progress/demo/abc123/'),
                ('post', '/manager/action/'),
            ):
                r = getattr(self.client, method)(url, {'action': 'stop'} if method == 'post' else {})
                self.assertEqual(r.status_code, 403, f'{role} {url}')


class ClientIpTests(TestCase):
    def test_client_supplied_headers_cannot_spoof_the_ip(self):
        rf = RequestFactory()
        req = rf.get('/', REMOTE_ADDR='127.0.0.1', HTTP_X_REAL_IP='203.0.113.9',
                     HTTP_CF_CONNECTING_IP='10.0.0.1', HTTP_X_FORWARDED_FOR='10.0.0.2, 203.0.113.9')
        self.assertEqual(get_client_ip(req), '203.0.113.9')
        direct = rf.get('/', REMOTE_ADDR='198.51.100.4', HTTP_X_REAL_IP='10.0.0.1')
        self.assertEqual(get_client_ip(direct), '198.51.100.4')


class TwoFactorHardeningTests(TestCase):
    def setUp(self):
        self.user = _user('totp', UserProfile.ROLE_ADMIN)
        self.secret = pyotp.random_base32()
        profile = self.user.userprofile
        profile.set_totp_secret(self.secret)
        profile.totp_enabled = True
        profile.save()

    def test_second_factor_locks_after_five_failures(self):
        self.client.post('/login/', {'username': 'totp', 'password': PW})
        for _ in range(5):
            self.client.post('/2fa/verify/', {'code': '000000'})
        self.user.userprofile.refresh_from_db()
        self.assertTrue(self.user.userprofile.is_locked())
        good = pyotp.TOTP(self.secret).now()
        r = self.client.post('/2fa/verify/', {'code': good})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('2fa_verified', self.client.session)

    def test_unverified_session_cannot_disable_2fa(self):
        self.client.force_login(self.user)  # session without second factor
        r = self.client.post('/2fa/setup/', {'action': 'disable'})
        self.assertEqual(r.status_code, 302)
        self.user.userprofile.refresh_from_db()
        self.assertTrue(self.user.userprofile.totp_enabled)


class AllowedHostsValidationTests(TestCase):
    def test_only_plain_hosts_are_accepted(self):
        ok = ['example.com', '.example.com', 'localhost', '*', '192.168.1.10', '::1']
        bad = ['a.com; } server { listen 1', 'a.com\nDEBUG=True', 'a b', 'x/../y', '$host', '']
        for host in ok:
            self.assertTrue(project_views._is_safe_allowed_host(host), host)
        for host in bad:
            self.assertFalse(project_views._is_safe_allowed_host(host), host)

    def test_injection_is_rejected_before_writing(self):
        self.client.force_login(_user('op', UserProfile.ROLE_ADMIN))
        with mock.patch.object(project_views, 'get_allowed_hosts', return_value=['a.com']), \
             mock.patch.object(project_views, 'update_allowed_hosts') as write:
            r = self.client.post('/project/demo/allowed-hosts/',
                                 {'action': 'add', 'new_host': 'b.com; } server { listen 9'})
        self.assertFalse(r.json()['ok'])
        write.assert_not_called()


class ScanTargetTests(TestCase):
    def setUp(self):
        self.viewer = _user('viewer', UserProfile.ROLE_VIEWER)
        ProjectPermission.objects.create(user=self.viewer, project_name='demo')
        patches = [
            mock.patch.object(project_views, 'get_project', return_value={'NGINX_PORT': '9000'}),
            mock.patch.object(project_views, 'get_allowed_hosts', return_value=['demo.example.com']),
            mock.patch.object(project_views, 'get_nginx_server_names', return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_foreign_target_is_refused(self):
        self.client.force_login(self.viewer)
        with mock.patch.object(project_views, 'run_http_security_scan') as scan:
            r = self.client.get('/project/demo/http-scan/', {'target': '10.0.0.5'})
        self.assertEqual(r.status_code, 400)
        scan.assert_not_called()

    def test_viewer_cannot_start_active_scans(self):
        self.client.force_login(self.viewer)
        with mock.patch.object(project_views, 'start_job') as job:
            for url in ('/project/demo/nuclei-scan/', '/project/demo/zap-scan/'):
                r = self.client.get(url, {'target': 'demo.example.com'})
                self.assertEqual(r.status_code, 403, url)
        job.assert_not_called()


class ProjectCodeNeverRunsAsRootTests(TestCase):
    def test_commands_drop_to_the_app_user_with_clean_env(self):
        conf = {'APPUSER': 'demo', 'APPDIR': '/srv/demo'}
        with mock.patch.object(pip_utils, 'get_project', return_value=conf), \
             mock.patch.object(pip_utils.subprocess, 'run') as run:
            pip_utils._run_as_project_user('demo', '/srv/demo/.venv/bin/python -V', timeout=5)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:4], ['su', 'demo', '-s', '/bin/bash'])
        self.assertIn('env -i', argv[-1])
        self.assertNotIn('env', run.call_args[1])

    def test_refuses_without_unprivileged_user(self):
        for appuser in ('', 'root', 'a b; id'):
            with mock.patch.object(pip_utils, 'get_project', return_value={'APPUSER': appuser}):
                with self.assertRaises(pip_utils.ProjectUserError):
                    pip_utils._run_as_project_user('demo', 'true', timeout=5)
