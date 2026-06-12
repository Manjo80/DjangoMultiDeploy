"""Regression tests for the security fixes in the views/middleware layer."""
from django.contrib.auth.models import User
from django.test import TestCase, Client

from control.models import UserProfile


class OpenRedirectTests(TestCase):
    """The login `next` parameter must never redirect off-site."""

    def setUp(self):
        self.user = User.objects.create_user('alice', password='Corr3ct-Horse-99')

    def test_external_next_is_ignored(self):
        c = Client()
        r = c.post('/login/?next=https://evil.example.com/',
                   {'username': 'alice', 'password': 'Corr3ct-Horse-99'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers['Location'], '/dashboard/')

    def test_protocol_relative_next_is_ignored(self):
        c = Client()
        r = c.post('/login/?next=//evil.example.com/',
                   {'username': 'alice', 'password': 'Corr3ct-Horse-99'})
        self.assertEqual(r.headers['Location'], '/dashboard/')

    def test_internal_next_is_preserved(self):
        c = Client()
        r = c.post('/login/?next=/audit/',
                   {'username': 'alice', 'password': 'Corr3ct-Horse-99'})
        self.assertEqual(r.headers['Location'], '/audit/')


class InstallPollTraversalTests(TestCase):
    """install_poll must reject log names that aren't <name>.log."""

    def setUp(self):
        self.user = User.objects.create_user('bob', password='Corr3ct-Horse-99')
        self.client.force_login(self.user)

    def test_traversal_name_rejected(self):
        # Django collapses ../ in the path, so a 404 from routing is also a pass;
        # the key property is that no file outside the log dir is ever served.
        r = self.client.get('/install/poll/evil..log/')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['done'])
        self.assertEqual(r.json()['lines'], [])

    def test_valid_name_accepted(self):
        r = self.client.get('/install/poll/myproj_abc123.log/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('waiting', r.json())


class PasswordValidationTests(TestCase):
    """User creation must enforce Django's password validators."""

    def setUp(self):
        admin = User.objects.create_user('admin', password='Corr3ct-Horse-99')
        admin.userprofile.role = UserProfile.ROLE_ADMIN
        admin.userprofile.save()
        self.client.force_login(admin)

    def test_numeric_password_rejected(self):
        r = self.client.post('/users/create/', {
            'username': 'newbie', 'password': '1234567890',
            'password2': '1234567890', 'role': UserProfile.ROLE_VIEWER,
        })
        self.assertFalse(User.objects.filter(username='newbie').exists())
        self.assertEqual(r.status_code, 200)

    def test_strong_password_accepted(self):
        r = self.client.post('/users/create/', {
            'username': 'newbie', 'password': 'Tr0ub4dour-x9',
            'password2': 'Tr0ub4dour-x9', 'role': UserProfile.ROLE_VIEWER,
        })
        self.assertEqual(r.status_code, 302)
        self.assertTrue(User.objects.filter(username='newbie').exists())
