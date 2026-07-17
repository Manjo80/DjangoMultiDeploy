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

        # A logged-out indicator (the login form's password field) is set so ZAP
        # re-authenticates mid-crawl instead of falling back to anonymous.
        out = captured['authentication/action/setLoggedOutIndicator']
        self.assertIn('password', out.get('loggedOutIndicatorRegex', ''))

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


class InterruptedInstallTests(TestCase):
    """Listing and clearing leftover installer checkpoints."""

    def _make_state(self, tmp, name, last_step='logdir_done'):
        import os
        path = os.path.join(tmp, f'django_install_{name}.state')
        with open(path, 'w') as f:
            f.write(f'PROJECTNAME="{name}"\nLAST_STEP="{last_step}"\n')
        return path

    def test_list_and_clear(self):
        import tempfile
        from control.utils import deployment
        with tempfile.TemporaryDirectory() as tmp:
            self._make_state(tmp, 'rfidclaude', 'logdir_done')
            self._make_state(tmp, 'other', 'db_setup')
            with mock.patch.object(deployment, '_INSTALL_STATE_DIR', tmp):
                items = deployment.list_interrupted_installs()
                names = {i['name'] for i in items}
                self.assertEqual(names, {'rfidclaude', 'other'})
                self.assertTrue(any(i['last_step'] == 'logdir_done' for i in items))

                ok, _ = deployment.clear_interrupted_install('rfidclaude')
                self.assertTrue(ok)
                self.assertEqual({i['name'] for i in deployment.list_interrupted_installs()},
                                 {'other'})

                # already gone / invalid name
                ok2, _ = deployment.clear_interrupted_install('rfidclaude')
                self.assertFalse(ok2)
                ok3, _ = deployment.clear_interrupted_install('bad;name')
                self.assertFalse(ok3)

    def test_clear_endpoint_requires_admin(self):
        viewer = User.objects.create_user('v3', password='Corr3ct-Horse-99')
        c = Client()
        c.force_login(viewer)
        r = c.post('/install/clear-interrupted/', {'name': 'x'})
        self.assertEqual(r.status_code, 403)

    def test_purge_reads_state_and_removes_artifacts(self):
        import tempfile, os
        from control.utils import deployment
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'django_install_rfidclaude.state')
            with open(path, 'w') as f:
                f.write('PROJECTNAME="rfidclaude"\nAPPDIR="/srv/rfidclaude"\n'
                        'APPUSER="rfidclaude"\nDBTYPE="postgresql"\n'
                        'DBNAME="rfidclaude"\nDBUSER="rfidclaude"\n')
            with mock.patch.object(deployment, '_INSTALL_STATE_DIR', tmp), \
                 mock.patch.object(deployment, 'get_all_projects', return_value=[]), \
                 mock.patch.object(deployment, 'remove_project', return_value=(True, 'ok')) as m:
                ok, _ = deployment.purge_interrupted_install('rfidclaude')
            self.assertTrue(ok)
            m.assert_called_once()
            args, kwargs = m.call_args
            self.assertEqual(args[0], 'rfidclaude')
            opts = args[1]
            self.assertTrue(opts['remove_appdir'])
            self.assertTrue(opts['remove_db'])
            self.assertTrue(opts['remove_user'])
            self.assertEqual(kwargs['conf'].get('DBNAME'), 'rfidclaude')

    def test_purge_spares_resources_of_other_project(self):
        import tempfile, os
        from control.utils import deployment
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'django_install_dup.state'), 'w') as f:
                f.write('PROJECTNAME="dup"\nAPPUSER="shared"\nDBNAME="shareddb"\n')
            others = [{'PROJECTNAME': 'real', 'APPUSER': 'shared', 'DBNAME': 'shareddb'}]
            with mock.patch.object(deployment, '_INSTALL_STATE_DIR', tmp), \
                 mock.patch.object(deployment, 'get_all_projects', return_value=others), \
                 mock.patch.object(deployment, 'remove_project', return_value=(True, 'ok')) as m:
                deployment.purge_interrupted_install('dup')
            opts = m.call_args[0][1]
            self.assertFalse(opts['remove_user'])  # 'shared' belongs to 'real'
            self.assertFalse(opts['remove_db'])    # 'shareddb' belongs to 'real'


class ProjectNotFoundPageTests(TestCase):
    def setUp(self):
        admin = User.objects.create_user('admin', password='Corr3ct-Horse-99')
        admin.userprofile.role = UserProfile.ROLE_ADMIN
        admin.userprofile.save()
        self.client.force_login(admin)

    def test_unknown_project_shows_friendly_404(self):
        r = self.client.get('/project/nonexistentproj/')
        self.assertEqual(r.status_code, 404)
        body = r.content.decode()
        self.assertIn('nicht gefunden', body)
        self.assertNotIn('The requested resource was not found', body)


class DatabaseInventoryTests(TestCase):
    """list_databases cross-references projects; drop_database is guarded."""

    def test_list_flags_orphan_inuse_system(self):
        from control.utils import db_admin
        pg = [
            {'engine': 'postgresql', 'name': 'gps', 'size_bytes': '1000', 'owner': 'gps_user'},
            {'engine': 'postgresql', 'name': 'old_stg', 'size_bytes': '500', 'owner': 'x'},
            {'engine': 'postgresql', 'name': 'postgres', 'size_bytes': '10', 'owner': 'postgres'},
        ]
        projects = [{'PROJECTNAME': 'gps', 'DBTYPE': 'postgresql', 'DBNAME': 'gps'}]
        with mock.patch.object(db_admin, '_list_postgresql', return_value=pg), \
             mock.patch.object(db_admin, '_list_mysql', return_value=[]), \
             mock.patch.object(db_admin, 'get_all_projects', return_value=projects):
            dbs = db_admin.list_databases()
        by = {d['name']: d for d in dbs}
        self.assertTrue(by['gps']['in_use'])
        self.assertEqual(by['gps']['project'], 'gps')
        self.assertFalse(by['gps']['deletable'])
        self.assertTrue(by['old_stg']['deletable'])
        self.assertTrue(by['postgres']['is_system'])
        self.assertFalse(by['postgres']['deletable'])
        # orphaned/deletable sorts first
        self.assertEqual(dbs[0]['name'], 'old_stg')

    def test_drop_refuses_system_and_invalid(self):
        from control.utils import db_admin
        with mock.patch.object(db_admin, '_run') as m, \
             mock.patch.object(db_admin, 'get_all_projects', return_value=[]):
            ok1, _ = db_admin.drop_database('postgresql', 'postgres')
            ok2, _ = db_admin.drop_database('postgresql', 'bad; DROP')
            ok3, _ = db_admin.drop_database('mongodb', 'x')
        self.assertFalse(ok1 or ok2 or ok3)
        m.assert_not_called()

    def test_drop_refuses_in_use(self):
        from control.utils import db_admin
        projects = [{'PROJECTNAME': 'gps', 'DBTYPE': 'postgresql', 'DBNAME': 'gps'}]
        with mock.patch.object(db_admin, '_run') as m, \
             mock.patch.object(db_admin, 'get_all_projects', return_value=projects):
            ok, msg = db_admin.drop_database('postgresql', 'gps')
        self.assertFalse(ok)
        self.assertIn('gps', msg)
        m.assert_not_called()

    def test_drop_orphan_runs_command(self):
        from control.utils import db_admin
        with mock.patch.object(db_admin, '_run', return_value=(0, '', '')) as m, \
             mock.patch.object(db_admin, 'get_all_projects', return_value=[]):
            ok, msg = db_admin.drop_database('postgresql', 'old_stg')
        self.assertTrue(ok)
        m.assert_called_once()
        self.assertIn('old_stg', ' '.join(m.call_args[0]))


class DbUserInventoryTests(TestCase):
    """list_db_users flags orphans; drop_db_user is guarded."""

    def test_list_flags_and_orphan_user_without_db(self):
        from control.utils import db_admin
        roles = [
            {'engine': 'postgresql', 'name': 'gps_user', 'is_super': False, 'can_login': True, 'owns_db': True},
            {'engine': 'postgresql', 'name': 'ghost_user', 'is_super': False, 'can_login': True, 'owns_db': False},
            {'engine': 'postgresql', 'name': 'postgres', 'is_super': True, 'can_login': True, 'owns_db': True},
        ]
        with mock.patch.object(db_admin, '_list_pg_roles', return_value=roles), \
             mock.patch.object(db_admin, '_list_mysql_users', return_value=[]), \
             mock.patch.object(db_admin, 'get_all_projects', return_value=[]), \
             mock.patch.object(db_admin, '_project_db_users', return_value={'gps_user'}):
            users = db_admin.list_db_users()
        by = {u['name']: u for u in users}
        self.assertFalse(by['gps_user']['deletable'])   # used by a project
        self.assertTrue(by['ghost_user']['deletable'])  # orphan, even without a DB
        self.assertTrue(by['postgres']['is_system'])
        self.assertFalse(by['postgres']['deletable'])

    def test_drop_dbuser_guards(self):
        from control.utils import db_admin
        with mock.patch.object(db_admin, '_run') as m, \
             mock.patch.object(db_admin, '_project_db_users', return_value={'gps_user'}):
            ok_sys, _ = db_admin.drop_db_user('postgresql', 'postgres')
            ok_used, _ = db_admin.drop_db_user('postgresql', 'gps_user')
            ok_bad, _ = db_admin.drop_db_user('postgresql', 'no; DROP')
        self.assertFalse(ok_sys or ok_used or ok_bad)
        m.assert_not_called()

    def test_drop_dbuser_refuses_role_owning_db(self):
        from control.utils import db_admin
        # count query returns "1" → role still owns a database → refuse
        with mock.patch.object(db_admin, '_run', return_value=(0, '1', '')) as m, \
             mock.patch.object(db_admin, '_project_db_users', return_value=set()):
            ok, msg = db_admin.drop_db_user('postgresql', 'ghost_user')
        self.assertFalse(ok)
        self.assertEqual(m.call_count, 1)  # only the ownership check ran, no DROP


class LinuxUserInventoryTests(TestCase):
    """list_linux_users only marks tool app users deletable; removal is guarded."""

    def _pw(self, name, uid, home):
        import pwd as _pwd
        return _pwd.struct_passwd((name, 'x', uid, uid, '', home, '/bin/bash'))

    def test_only_tool_appuser_orphan_is_deletable(self):
        from control.utils import system_users
        entries = [
            self._pw('gps', 1001, '/home/gps'),      # in use
            self._pw('oldproj', 1002, '/home/oldproj'),  # orphan app user (has key)
            self._pw('manjo', 1000, '/home/manjo'),  # human account (no key)
        ]
        has_key = {'/home/gps', '/home/oldproj'}
        with mock.patch.object(system_users.pwd, 'getpwall', return_value=entries), \
             mock.patch.object(system_users, '_appuser_project_map', return_value={'gps': 'gps'}), \
             mock.patch.object(system_users.os.path, 'exists',
                               side_effect=lambda p: any(p.startswith(h) for h in has_key)):
            users = system_users.list_linux_users()
        by = {u['name']: u for u in users}
        self.assertFalse(by['gps']['deletable'])       # in use
        self.assertTrue(by['oldproj']['deletable'])    # orphan tool app user
        self.assertFalse(by['manjo']['deletable'])     # human account, no deploy key

    def test_remove_refuses_human_account_without_key(self):
        from control.utils import system_users
        pw = self._pw('manjo', 1000, '/home/manjo')
        with mock.patch.object(system_users.pwd, 'getpwnam', return_value=pw), \
             mock.patch.object(system_users, '_appuser_project_map', return_value={}), \
             mock.patch.object(system_users.os.path, 'exists', return_value=False), \
             mock.patch.object(system_users.subprocess, 'run') as m:
            ok, msg = system_users.remove_linux_user('manjo')
        self.assertFalse(ok)
        m.assert_not_called()

    def test_remove_refuses_root_and_in_use(self):
        from control.utils import system_users
        with mock.patch.object(system_users, '_appuser_project_map', return_value={'gps': 'gps'}), \
             mock.patch.object(system_users.subprocess, 'run') as m:
            ok_root, _ = system_users.remove_linux_user('root')
            ok_used, _ = system_users.remove_linux_user('gps')
        self.assertFalse(ok_root or ok_used)
        m.assert_not_called()

    def test_remove_orphan_appuser_runs_deluser(self):
        from control.utils import system_users
        pw = self._pw('oldproj', 1002, '/home/oldproj')
        with mock.patch.object(system_users.pwd, 'getpwnam', return_value=pw), \
             mock.patch.object(system_users, '_appuser_project_map', return_value={}), \
             mock.patch.object(system_users.os.path, 'exists', return_value=True), \
             mock.patch.object(system_users.subprocess, 'run',
                               return_value=mock.Mock(returncode=0, stderr='', stdout='')) as m:
            ok, msg = system_users.remove_linux_user('oldproj')
        self.assertTrue(ok)
        m.assert_called_once()
        self.assertIn('oldproj', ' '.join(m.call_args[0][0]))


class DatabaseAdminViewTests(TestCase):
    def setUp(self):
        admin = User.objects.create_user('admin', password='Corr3ct-Horse-99')
        admin.userprofile.role = UserProfile.ROLE_ADMIN
        admin.userprofile.save()
        self.client.force_login(admin)

    def test_admin_can_view(self):
        from control.views import admin_views
        with mock.patch.object(admin_views, 'list_databases', return_value=[]), \
             mock.patch.object(admin_views, 'list_db_users', return_value=[]), \
             mock.patch.object(admin_views, 'list_linux_users', return_value=[]):
            r = self.client.get('/databases/')
        self.assertEqual(r.status_code, 200)

    def test_viewer_forbidden(self):
        viewer = User.objects.create_user('v2', password='Corr3ct-Horse-99')
        c = Client()
        c.force_login(viewer)
        r = c.get('/databases/')
        self.assertEqual(r.status_code, 403)


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
