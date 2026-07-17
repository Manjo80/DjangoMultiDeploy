"""
Linux (app) user inventory + cleanup for DjangoMultiDeploy.

Lists regular Linux accounts and cross-references them with the project
registry (each project has an APPUSER). Leftover app users from removed
projects can be removed here.

Safety: removal is only ever offered/performed for accounts that were almost
certainly created by this tool — i.e. they carry the per-app-user deploy key
(``~/.ssh/id_ed25519``) AND are not the APPUSER of any current project. Regular
human login accounts (no deploy key) and system accounts (UID < 1000) are shown
for reference but never deletable, so the admin's own account can't be removed
by accident.
"""
import os
import pwd
import shutil
import subprocess

from django.conf import settings

from .registry import get_all_projects
from .validators import is_valid_linux_user

_DELUSER = shutil.which('deluser') or '/usr/sbin/deluser'
_USERDEL = shutil.which('userdel') or '/usr/sbin/userdel'

_UID_MIN = 1000
_UID_MAX = 60000


def _manager_user():
    return getattr(settings, 'MANAGER_SERVICE_NAME', 'djmanager')


def _appuser_project_map():
    """Map Linux APPUSER -> project name for all registered projects."""
    m = {}
    for p in get_all_projects():
        user = (p.get('APPUSER') or '').strip()
        if user:
            m[user] = p.get('PROJECTNAME', '')
    return m


def _has_deploy_key(home):
    return bool(home) and os.path.exists(os.path.join(home, '.ssh', 'id_ed25519'))


def list_linux_users():
    """
    Return regular Linux account dicts:
      {name, uid, home, shell, in_use, project, looks_app, protected, deletable}
    Deletable = tool-created app user (has deploy key) that is not a current
    project's APPUSER. Orphaned/deletable accounts sort first.
    """
    app_map = _appuser_project_map()
    mgr = _manager_user()
    rows = []
    try:
        entries = list(pwd.getpwall())
    except Exception:
        entries = []
    for e in entries:
        if not (_UID_MIN <= e.pw_uid <= _UID_MAX) or e.pw_name == 'nobody':
            continue
        name = e.pw_name
        home = e.pw_dir or ''
        in_use = name in app_map
        looks_app = _has_deploy_key(home)
        protected = name in ('root', mgr)
        rows.append({
            'name':      name,
            'uid':       e.pw_uid,
            'home':      home,
            'shell':     e.pw_shell,
            'in_use':    in_use,
            'project':   app_map.get(name),
            'looks_app': looks_app,
            'protected': protected,
            'deletable': (not in_use) and (not protected) and looks_app,
        })

    def _sort_key(u):
        grp = 0 if u['deletable'] else (1 if u['in_use'] else 2)
        return (grp, u['name'])

    return sorted(rows, key=_sort_key)


def remove_linux_user(username):
    """
    Remove a leftover tool-created app user (and its home). Returns (ok, message).

    Hard guards — refuses when any is true:
      - invalid username
      - root or the manager service user
      - UID outside the regular range (system account)
      - still the APPUSER of a registered project
      - no tool deploy key present (i.e. not clearly a tool app user)
    """
    if not is_valid_linux_user(username):
        return False, f'Ungültiger Benutzername: {username!r}'
    if username in ('root', _manager_user()):
        return False, 'Geschützter Benutzer — Löschen abgelehnt.'
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        return False, 'Benutzer nicht gefunden.'
    if not (_UID_MIN <= pw.pw_uid <= _UID_MAX):
        return False, 'System-Benutzer (UID < 1000) — Löschen abgelehnt.'
    if username in _appuser_project_map():
        return False, f'Benutzer "{username}" ist App-User eines Projekts — Löschen abgelehnt.'
    if not _has_deploy_key(pw.pw_dir or ''):
        return False, ('Kein tool-erzeugter App-User (kein Deploy-Key gefunden) — '
                       'aus Sicherheitsgründen abgelehnt. Bitte manuell entfernen.')

    try:
        r1 = subprocess.run([_DELUSER, '--remove-home', username],
                            capture_output=True, text=True, timeout=60)
        if r1.returncode != 0:
            r2 = subprocess.run([_USERDEL, '-r', username],
                                capture_output=True, text=True, timeout=60)
            if r2.returncode != 0:
                return False, (r2.stderr or r1.stderr or 'Löschen fehlgeschlagen').strip()[:400]
    except Exception as e:
        return False, str(e)
    return True, f'Linux-Benutzer "{username}" entfernt (inkl. Home-Verzeichnis).'
