"""Tests for the new features: health history, notifications, TLS validators."""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, Client

from control.models import UserProfile, HealthSample, NotificationSettings
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
