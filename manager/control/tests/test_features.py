"""Tests for the new features: health history, notifications, TLS validators."""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, Client

from control.models import UserProfile, HealthSample, NotificationSettings, UpdateCommand
from control.utils.validators import is_valid_hostname
from control.utils import notify


class HostnameValidatorTests(TestCase):
    def test_valid_hostnames(self):
        for h in ('example.com', 'sub.example.com', 'a-b.example.co.uk'):
            self.assertTrue(is_valid_hostname(h), h)

    def test_invalid_hostnames(self):
        for h in ('', 'no-tld', '*.example.com', 'a b.com', 'http://example.com',
                  'example.com/path', '-bad.com', 'evil;rm.com'):
            self.assertFalse(is_valid_hostname(h), h)


class HealthSampleTests(TestCase):
    def test_record_and_prune(self):
        HealthSample.record({'mem_percent': 42, 'disk_percent': 70, 'load1': '0.5'})
        self.assertEqual(HealthSample.objects.count(), 1)
        s = HealthSample.objects.first()
        self.assertEqual(s.mem_percent, 42)
        self.assertEqual(s.load1, 0.5)

    def test_record_tolerates_bad_load(self):
        HealthSample.record({'mem_percent': None, 'disk_percent': None, 'load1': None})
        self.assertEqual(HealthSample.objects.count(), 1)

    def test_history_endpoint(self):
        user = User.objects.create_user('viewer', password='Corr3ct-Horse-99')
        self.client.force_login(user)
        HealthSample.record({'mem_percent': 10, 'disk_percent': 20, 'load1': 0.1})
        r = self.client.get('/dashboard/health-history/?hours=24')
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(len(data['samples']), 1)
        self.assertEqual(data['samples'][0]['mem'], 10)


class NotificationDispatchTests(TestCase):
    def _enable(self, **extra):
        cfg = NotificationSettings.get()
        cfg.enabled = True
        for k, v in extra.items():
            setattr(cfg, k, v)
        cfg.save()
        return cfg

    def test_disabled_sends_nothing(self):
        res = notify.send_notification('s', 'b')
        self.assertEqual(res, {'email': None, 'webhook': None})

    def test_event_toggle_suppresses(self):
        self._enable(webhook_enabled=True, webhook_url='https://x.test/hook',
                     notify_backup_failure=False)
        with mock.patch.object(notify, '_send_webhook') as m:
            res = notify.send_notification('s', 'b', event_type=notify.EVENT_BACKUP_FAILURE)
            m.assert_not_called()
        self.assertIsNone(res['webhook'])

    def test_webhook_sent_when_enabled(self):
        self._enable(webhook_enabled=True, webhook_url='https://x.test/hook')
        with mock.patch.object(notify, '_send_webhook', return_value=True) as m:
            res = notify.send_notification('s', 'b', event_type=notify.EVENT_VULNERABILITY)
            m.assert_called_once()
        self.assertTrue(res['webhook'])

    def test_email_sent_when_enabled(self):
        self._enable(email_enabled=True, smtp_host='smtp.test', email_to='a@test.com')
        with mock.patch.object(notify, '_send_email', return_value=True) as m:
            res = notify.send_notification('s', 'b')
            m.assert_called_once()
        self.assertTrue(res['email'])


class NotificationSettingsViewTests(TestCase):
    def setUp(self):
        admin = User.objects.create_user('admin', password='Corr3ct-Horse-99')
        admin.userprofile.role = UserProfile.ROLE_ADMIN
        admin.userprofile.save()
        self.client.force_login(admin)

    def test_password_not_wiped_when_blank(self):
        cfg = NotificationSettings.get()
        cfg.smtp_password = 'sekret'
        cfg.save()
        self.client.post('/notifications/', {
            'action': 'save', 'enabled': 'on', 'smtp_host': 'smtp.test',
            'smtp_port': '587', 'email_to': 'a@test.com', 'smtp_password': '',
        })
        cfg.refresh_from_db()
        self.assertEqual(cfg.smtp_password, 'sekret')

    def test_viewer_forbidden(self):
        viewer = User.objects.create_user('v', password='Corr3ct-Horse-99')
        c = Client()
        c.force_login(viewer)
        r = c.get('/notifications/')
        self.assertEqual(r.status_code, 403)


class UpdateCommandViewTests(TestCase):
    """CRUD + permission tests for the configurable update pipeline."""

    URL = '/project/demo/update-commands/'

    def setUp(self):
        admin = User.objects.create_user('admin', password='Corr3ct-Horse-99')
        admin.userprofile.role = UserProfile.ROLE_ADMIN
        admin.userprofile.save()
        self.client.force_login(admin)

    def test_add_list_toggle_delete(self):
        # add
        r = self.client.post(self.URL, {'action': 'add', 'label': 'Glossar',
                                         'command': 'load_glossary'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['ok'])
        obj = UpdateCommand.objects.get(project_name='demo')
        self.assertEqual(obj.command, 'load_glossary')
        self.assertTrue(obj.enabled)

        # leading 'python manage.py' is stripped on add
        r = self.client.post(self.URL, {'action': 'add', 'label': 'Seed',
                                         'command': 'python manage.py loaddata seed.json'})
        self.assertEqual(UpdateCommand.objects.get(label='Seed').command, 'loaddata seed.json')

        # list
        r = self.client.get(self.URL)
        self.assertEqual(len(r.json()['commands']), 2)

        # toggle off
        r = self.client.post(self.URL, {'action': 'toggle', 'pk': obj.pk})
        obj.refresh_from_db()
        self.assertFalse(obj.enabled)

        # delete
        r = self.client.post(self.URL, {'action': 'delete', 'pk': obj.pk})
        self.assertTrue(r.json()['ok'])
        self.assertFalse(UpdateCommand.objects.filter(pk=obj.pk).exists())

    def test_rejects_shell_metacharacters(self):
        r = self.client.post(self.URL, {'action': 'add', 'label': 'evil',
                                        'command': 'migrate; rm -rf /'})
        self.assertFalse(r.json()['ok'])
        self.assertEqual(UpdateCommand.objects.count(), 0)

    def test_duplicate_rejected(self):
        self.client.post(self.URL, {'action': 'add', 'label': 'a', 'command': 'migrate'})
        r = self.client.post(self.URL, {'action': 'add', 'label': 'b', 'command': 'migrate'})
        self.assertFalse(r.json()['ok'])
        self.assertEqual(UpdateCommand.objects.count(), 1)

    def test_viewer_cannot_modify(self):
        viewer = User.objects.create_user('v', password='Corr3ct-Horse-99')
        c = Client()
        c.force_login(viewer)
        r = c.post(self.URL, {'action': 'add', 'label': 'x', 'command': 'migrate'})
        self.assertEqual(r.status_code, 403)


class CustomUpdateCommandRunnerTests(TestCase):
    """Unit tests for the helper that runs the configurable update steps."""

    def _conf(self):
        return {'APPDIR': '/srv/demo', 'APPUSER': 'demo'}

    def test_noop_when_no_commands(self):
        from control.utils.deployment import _run_custom_update_commands
        with mock.patch('control.utils.deployment.subprocess.run') as m:
            ok, out = _run_custom_update_commands('demo', self._conf())
        self.assertTrue(ok)
        self.assertEqual(out, '')
        m.assert_not_called()  # not even a restart when nothing is configured

    def test_runs_enabled_skips_disabled_then_restarts(self):
        from control.utils.deployment import _run_custom_update_commands
        UpdateCommand.objects.create(project_name='demo', label='A',
                                     command='load_glossary', order=0, enabled=True)
        UpdateCommand.objects.create(project_name='demo', label='B',
                                     command='clearsessions', order=1, enabled=False)

        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout='ok', stderr='')

        with mock.patch('control.utils.deployment.subprocess.run', side_effect=fake_run):
            ok, out = _run_custom_update_commands('demo', self._conf())

        self.assertTrue(ok)
        # one su-run for the enabled command + one systemctl restart
        self.assertEqual(len(calls), 2)
        joined = ' '.join(' '.join(c) for c in calls)
        self.assertIn('load_glossary', joined)
        self.assertNotIn('clearsessions', joined)
        self.assertIn('restart', joined)

    def test_invalid_appuser_aborts(self):
        from control.utils.deployment import _run_custom_update_commands
        UpdateCommand.objects.create(project_name='demo', label='A',
                                     command='migrate', enabled=True)
        with mock.patch('control.utils.deployment.subprocess.run') as m:
            ok, out = _run_custom_update_commands('demo', {'APPDIR': '/srv/demo',
                                                           'APPUSER': 'bad user!'})
        self.assertFalse(ok)
        m.assert_not_called()


class ZapAuthEncodingTests(TestCase):
    """The form-based auth login data must encode placeholders exactly once."""

    def test_login_request_data_placeholders_not_double_encoded(self):
        import urllib.parse as up
        from control.utils import scanning

        captured = {}

        def fake_api(path, params=None):
            captured[path] = params or {}
            # newUser must return a userId for the rest of the flow
            return {'userId': '0', 'contextId': '1'}

        auth = {
            'login_url': 'https://app.example.com/login/',
            'username_field': 'username', 'password_field': 'password',
            'username': 'admin', 'password': 'secret',
            'logged_in_indicator': '/logout/',
        }
        with mock.patch.object(scanning, '_zap_api', side_effect=fake_api):
            scanning._zap_setup_auth('1', 'djmanager', 'https://app.example.com/', auth)

        # includeInContext must be keyed by contextName (contextId → 400)
        inc = captured['context/action/includeInContext']
        self.assertEqual(inc.get('contextName'), 'djmanager')
        self.assertNotIn('contextId', inc)

        cfg = captured['authentication/action/setAuthenticationMethod']
        params = cfg['authMethodConfigParams']
        # Pull loginRequestData out and decode once (as ZAP does)
        sub = dict(up.parse_qsl(params))
        login_data = up.unquote(sub['loginRequestData'])
        # Correct, single-encoded placeholders — NOT the %2525 double-encoded form
        self.assertIn('username={%username%}', login_data)
        self.assertIn('password={%password%}', login_data)
        # The old bug produced "%2525" (percent double-encoded). A single "%25"
        # is correct (it is the encoded "%" of the {%username%} placeholder).
        self.assertNotIn('%2525', params)

    def test_registers_django_csrf_token(self):
        from control.utils import scanning
        calls = []

        def fake_api(path, params=None):
            calls.append((path, params or {}))
            return {'userId': '0'}

        auth = {'login_url': 'https://a/login/', 'username': 'u', 'password': 'p'}
        with mock.patch.object(scanning, '_zap_api', side_effect=fake_api):
            scanning._zap_setup_auth('1', 'djmanager', 'https://a/', auth)

        acsrf = [p for path, p in calls if path == 'acsrf/action/addOptionToken']
        self.assertEqual(acsrf, [{'String': 'csrfmiddlewaretoken'}])


class ZapApiErrorTests(TestCase):
    """A ZAP API HTTP error must surface the endpoint + ZAP's error body."""

    def test_http_error_includes_endpoint_and_body(self):
        import io
        import urllib.error
        from control.utils import scanning

        def raise_400(url, **kw):
            raise urllib.error.HTTPError(
                url, 400, 'Bad Request', {},
                io.BytesIO(b'{"code":"url_not_found","message":"No such URL"}'))

        with mock.patch.object(scanning, '_safe_urlopen', side_effect=raise_400):
            with self.assertRaises(RuntimeError) as cm:
                scanning._zap_api('spider/action/scan', {'url': 'https://x/'})

        msg = str(cm.exception)
        self.assertIn('spider/action/scan', msg)   # which call failed
        self.assertIn('400', msg)
        self.assertIn('url_not_found', msg)         # ZAP's actual reason


class NucleiUpdateTests(TestCase):
    """update_nuclei must force a fresh GitHub download, not re-copy a stale binary."""

    def test_update_forces_github_download(self):
        from control.utils import scanning
        with mock.patch.object(scanning, '_download_nuclei_release', return_value=True) as dl, \
             mock.patch.object(scanning, '_nuclei_installed_version', return_value='3.9.0'), \
             mock.patch.object(scanning._shutil, 'which', return_value='/usr/local/bin/nuclei') as which, \
             mock.patch.object(scanning._shutil, 'copy2') as copy2:
            res = scanning.update_nuclei()
        self.assertTrue(res['ok'])
        self.assertEqual(res['version'], '3.9.0')
        dl.assert_called_once()        # forced release download was used
        copy2.assert_not_called()      # did NOT re-copy an existing binary

    def test_force_skips_path_copy_shortcut(self):
        from control.utils import scanning
        with mock.patch.object(scanning, '_download_nuclei_release', return_value=True) as dl, \
             mock.patch.object(scanning._shutil, 'which', return_value='/usr/bin/nuclei') as which, \
             mock.patch.object(scanning._shutil, 'copy2') as copy2:
            ok = scanning._install_nuclei(force=True)
        self.assertTrue(ok)
        dl.assert_called_once()
        copy2.assert_not_called()
