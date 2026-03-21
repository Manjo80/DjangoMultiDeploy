"""
pip and Django management command utility functions for DjangoMultiDeploy.
"""
import os
import json
import subprocess
import shlex
from pathlib import Path
from django.conf import settings

from .registry import get_project, _parse_conf


def run_pip_audit(project):
    """
    Run pip-audit against the project's venv.
    Returns a dict: {'ok': bool, 'vulnerabilities': [...], 'error': str}
    """
    venv_python = f'/srv/{project}/.venv/bin/python'
    if not os.path.exists(venv_python):
        return {'ok': False, 'vulnerabilities': [], 'error': 'venv nicht gefunden'}

    try:
        result = subprocess.run(
            [venv_python, '-m', 'pip_audit', '--format=json', '--progress-spinner=off'],
            capture_output=True, text=True, timeout=120,
        )
        # pip_audit exits with 1 when vulnerabilities are found — not an error
        output = result.stdout.strip()
        if not output:
            return {'ok': True, 'vulnerabilities': [], 'error': ''}
        data = json.loads(output)
        vulns = []
        for dep in data.get('dependencies', []):
            for vuln in dep.get('vulns', []):
                vulns.append({
                    'package': dep.get('name', ''),
                    'version': dep.get('version', ''),
                    'id':      vuln.get('id', ''),
                    'fix':     vuln.get('fix_versions', []),
                    'desc':    vuln.get('description', '')[:200],
                })
        return {'ok': True, 'vulnerabilities': vulns, 'error': ''}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'vulnerabilities': [], 'error': 'Timeout (pip-audit)'}
    except json.JSONDecodeError:
        return {'ok': False, 'vulnerabilities': [], 'error': 'Ungültige pip-audit Ausgabe'}
    except Exception as e:
        return {'ok': False, 'vulnerabilities': [], 'error': str(e)}


def run_django_deploy_check(project):
    """
    Run `manage.py check --deploy` and return parsed issues.
    Returns a dict: {'ok': bool, 'issues': [...], 'error': str}
    """
    venv_python = f'/srv/{project}/.venv/bin/python'
    manage_py   = f'/srv/{project}/manage.py'
    if not os.path.exists(venv_python) or not os.path.exists(manage_py):
        return {'ok': False, 'issues': [], 'error': 'Projekt-Dateien nicht gefunden'}

    conf = _parse_conf(f'/etc/django-servers.d/{project}.conf') if os.path.exists(
        f'/etc/django-servers.d/{project}.conf') else {}
    env_file = f'/srv/{project}/.env'

    try:
        env = os.environ.copy()
        # Remove manager's DJANGO_SETTINGS_MODULE so manage.py sets its own
        env.pop('DJANGO_SETTINGS_MODULE', None)
        env['PYTHONPATH'] = f'/srv/{project}'
        # Load env from .env file — direct assignment so project values always win
        # over anything the manager process may have inherited (e.g. DEBUG, MODE)
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, _, v = line.partition('=')
                        env[k.strip()] = v.strip().strip('"\'')  # overwrite, not setdefault

        result = subprocess.run(
            [venv_python, manage_py, 'check', '--deploy'],
            capture_output=True, text=True, timeout=30,
            cwd=f'/srv/{project}', env=env,
        )
        mode_used = env.get('MODE', '?')
        output = (result.stdout + result.stderr).strip()
        # Prepend detected MODE so it's visible in the output
        output = f'[deploy check] MODE={mode_used}\n\n' + output
        # Filter silenced checks from output (compatible with all Django versions)
        # W008=SECURE_SSL_REDIRECT: handled by nginx
        SILENCED = {'security.W008'}
        filtered_lines = []
        skip_block = False
        for line in output.splitlines():
            stripped = line.strip()
            if any(f'({c})' in stripped for c in SILENCED):
                skip_block = True  # skip this warning line
                continue
            if skip_block and stripped and not stripped.startswith('?:') and not stripped.startswith('System check'):
                continue  # skip continuation lines of silenced warning
            skip_block = False
            filtered_lines.append(line)
        output = '\n'.join(filtered_lines)
        issues = []
        for line in output.splitlines():
            line = line.strip()
            if line and (
                line.startswith('WARNINGS:') or
                line.startswith('System check') or
                ': (' in line
            ):
                issues.append(line)
        ok = result.returncode == 0
        # Keep last 5000 chars so the actual error at the end of the traceback is visible
        raw = output[-5000:] if len(output) > 5000 else output
        return {'ok': ok, 'issues': issues, 'raw': raw, 'error': ''}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'issues': [], 'raw': '', 'error': 'Timeout (deploy check)'}
    except Exception as e:
        return {'ok': False, 'issues': [], 'raw': '', 'error': str(e)}


def run_manager_pip_audit():
    """Run pip-audit against the manager's own venv."""
    venv_python = os.path.join(settings.MANAGER_VENV, 'bin', 'python')
    if not os.path.exists(venv_python):
        return {'ok': False, 'vulnerabilities': [], 'error': f'Manager-venv nicht gefunden: {venv_python}'}
    try:
        result = subprocess.run(
            [venv_python, '-m', 'pip_audit', '--format=json', '--progress-spinner=off'],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout.strip()
        if not output:
            return {'ok': True, 'vulnerabilities': [], 'error': ''}
        data = json.loads(output)
        vulns = []
        for dep in data.get('dependencies', []):
            for vuln in dep.get('vulns', []):
                vulns.append({
                    'package': dep.get('name', ''),
                    'version': dep.get('version', ''),
                    'id':      vuln.get('id', ''),
                    'fix':     vuln.get('fix_versions', []),
                    'desc':    vuln.get('description', '')[:200],
                })
        return {'ok': True, 'vulnerabilities': vulns, 'error': ''}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'vulnerabilities': [], 'error': 'Timeout (pip-audit)'}
    except json.JSONDecodeError:
        return {'ok': False, 'vulnerabilities': [], 'error': 'Ungültige pip-audit Ausgabe'}
    except Exception as e:
        return {'ok': False, 'vulnerabilities': [], 'error': str(e)}


def run_manager_deploy_check():
    """Run manage.py check --deploy on the manager itself."""
    venv_python = os.path.join(settings.MANAGER_VENV, 'bin', 'python')
    manage_py   = str(Path(settings.BASE_DIR) / 'manage.py')
    if not os.path.exists(venv_python) or not os.path.exists(manage_py):
        return {'ok': False, 'issues': [], 'raw': '', 'error': 'Manager-Dateien nicht gefunden'}
    try:
        env = os.environ.copy()
        env.pop('DJANGO_SETTINGS_MODULE', None)
        result = subprocess.run(
            [venv_python, manage_py, 'check', '--deploy'],
            capture_output=True, text=True, timeout=30,
            cwd=str(Path(manage_py).parent), env=env,
        )
        output = (result.stdout + result.stderr).strip()
        issues = []
        for line in output.splitlines():
            line = line.strip()
            if line and (
                line.startswith('WARNINGS:') or
                line.startswith('System check') or
                ': (' in line
            ):
                issues.append(line)
        ok = result.returncode == 0
        raw = output[-5000:] if len(output) > 5000 else output
        return {'ok': ok, 'issues': issues, 'raw': raw, 'error': ''}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'issues': [], 'raw': '', 'error': 'Timeout (deploy check)'}
    except Exception as e:
        return {'ok': False, 'issues': [], 'raw': '', 'error': str(e)}


def run_migration_status(name):
    """
    Run 'manage.py showmigrations --list' for the given project.
    Returns {'ok': bool, 'apps': [{'app': str, 'migrations': [{'name': str, 'applied': bool}]}], 'error': str}
    """
    import re as _re
    conf = get_project(name)
    if not conf:
        return {'ok': False, 'apps': [], 'error': 'Projekt nicht gefunden'}
    appdir      = conf.get('APPDIR', f'/srv/{name}')
    appuser     = conf.get('APPUSER', '')
    venv_python = os.path.join(appdir, '.venv', 'bin', 'python')
    manage_py   = os.path.join(appdir, 'manage.py')
    if not os.path.exists(venv_python):
        return {'ok': False, 'apps': [], 'error': f'venv nicht gefunden: {venv_python}'}
    if not os.path.exists(manage_py):
        return {'ok': False, 'apps': [], 'error': f'manage.py nicht gefunden: {manage_py}'}

    full_cmd = (
        f'cd {shlex.quote(appdir)} && '
        f'{shlex.quote(venv_python)} manage.py showmigrations --list'
    )
    run_args = (['su', '-', appuser, '-s', '/bin/bash', '-c', full_cmd]
                if appuser else ['bash', '-c', full_cmd])
    try:
        result = subprocess.run(run_args, capture_output=True, text=True, timeout=60)
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0 and not result.stdout.strip():
            return {'ok': False, 'apps': [], 'error': output[:500]}

        # Parse output: app lines have no leading space, migration lines start with ' [X]' or ' [ ]'
        apps = []
        current_app = None
        for line in result.stdout.splitlines():
            app_match = _re.match(r'^(\S+)$', line.strip())
            mig_match = _re.match(r'^\s+\[( |X)\]\s+(.+)$', line)
            if app_match and not line.startswith(' '):
                current_app = {'app': line.strip(), 'migrations': []}
                apps.append(current_app)
            elif mig_match and current_app is not None:
                current_app['migrations'].append({
                    'name':    mig_match.group(2).strip(),
                    'applied': mig_match.group(1) == 'X',
                })
        return {'ok': True, 'apps': apps, 'error': ''}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'apps': [], 'error': 'Timeout nach 60 Sekunden'}
    except Exception as e:
        return {'ok': False, 'apps': [], 'error': str(e)}


def run_pip_outdated(name):
    """
    Run 'pip list --outdated --format=json' in the project venv.
    Returns {'ok': bool, 'packages': [{'name', 'current', 'latest', 'type'}], 'error': str}
    """
    conf = get_project(name)
    if not conf:
        return {'ok': False, 'packages': [], 'error': 'Projekt nicht gefunden'}
    appdir      = conf.get('APPDIR', f'/srv/{name}')
    appuser     = conf.get('APPUSER', '')
    venv_python = os.path.join(appdir, '.venv', 'bin', 'python')
    if not os.path.exists(venv_python):
        return {'ok': False, 'packages': [], 'error': f'venv nicht gefunden: {venv_python}'}

    full_cmd = f'{shlex.quote(venv_python)} -m pip list --outdated --format=json'
    run_args = (['su', '-', appuser, '-s', '/bin/bash', '-c', full_cmd]
                if appuser else ['bash', '-c', full_cmd])
    try:
        result = subprocess.run(run_args, capture_output=True, text=True, timeout=120)
        raw = result.stdout.strip()
        if not raw:
            return {'ok': True, 'packages': [], 'error': ''}
        data = json.loads(raw)
        packages = [
            {
                'name':    p.get('name', ''),
                'current': p.get('version', ''),
                'latest':  p.get('latest_version', ''),
                'type':    p.get('latest_filetype', ''),
            }
            for p in data
        ]
        return {'ok': True, 'packages': packages, 'error': ''}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'packages': [], 'error': 'Timeout nach 120 Sekunden'}
    except json.JSONDecodeError:
        return {'ok': False, 'packages': [], 'error': 'Ungültige pip-Ausgabe'}
    except Exception as e:
        return {'ok': False, 'packages': [], 'error': str(e)}


def run_pip_upgrade(name, package_name):
    """
    Upgrade a single package in the project venv.
    Returns {'ok': bool, 'output': str}
    """
    import re as _re
    if not package_name or not _re.match(r'^[A-Za-z0-9_.\-]+$', package_name):
        return {'ok': False, 'output': 'Ungültiger Paketname'}
    conf = get_project(name)
    if not conf:
        return {'ok': False, 'output': 'Projekt nicht gefunden'}
    appdir      = conf.get('APPDIR', f'/srv/{name}')
    appuser     = conf.get('APPUSER', '')
    venv_python = os.path.join(appdir, '.venv', 'bin', 'python')
    if not os.path.exists(venv_python):
        return {'ok': False, 'output': f'venv nicht gefunden: {venv_python}'}

    full_cmd = f'{shlex.quote(venv_python)} -m pip install --upgrade {shlex.quote(package_name)}'
    run_args = (['su', '-', appuser, '-s', '/bin/bash', '-c', full_cmd]
                if appuser else ['bash', '-c', full_cmd])
    try:
        result = subprocess.run(run_args, capture_output=True, text=True, timeout=180)
        output = (result.stdout + result.stderr).strip()
        return {'ok': result.returncode == 0, 'output': output or '(keine Ausgabe)'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'output': 'Timeout nach 180 Sekunden'}
    except Exception as e:
        return {'ok': False, 'output': str(e)}
