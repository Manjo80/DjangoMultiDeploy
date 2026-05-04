"""
pip and Django management command utility functions for DjangoMultiDeploy.
"""
import os
import json
import shutil
import subprocess
import shlex
from pathlib import Path

_BASH = shutil.which('bash') or '/bin/bash'
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


def run_bandit(project):
    """
    Run bandit static analysis against the project source (excludes .venv).
    Returns {'ok': bool, 'findings': [...], 'metrics': {...}, 'error': str}
    """
    appdir = f'/srv/{project}'
    venv_pip = f'{appdir}/.venv/bin/pip'
    if not os.path.exists(venv_pip):
        return {'ok': False, 'findings': [], 'metrics': {}, 'error': 'venv nicht gefunden'}

    # Install bandit into the project venv if missing
    bandit_bin = f'{appdir}/.venv/bin/bandit'
    if not os.path.exists(bandit_bin):
        subprocess.run([venv_pip, 'install', '--quiet', 'bandit'],
                       capture_output=True, timeout=60)

    if not os.path.exists(bandit_bin):
        return {'ok': False, 'findings': [], 'metrics': {}, 'error': 'bandit konnte nicht installiert werden'}

    try:
        result = subprocess.run(
            [bandit_bin, '-r', appdir, '--exclude', f'{appdir}/.venv',
             '-f', 'json', '-q'],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout.strip()
        if not output:
            return {'ok': True, 'findings': [], 'metrics': {}, 'error': ''}
        data = json.loads(output)
        findings = []
        for r in data.get('results', []):
            filepath = r.get('filename', '')
            # Show path relative to appdir
            rel = filepath.replace(appdir + '/', '') if filepath.startswith(appdir) else filepath
            findings.append({
                'file':       rel,
                'line':       r.get('line_number', ''),
                'severity':   r.get('issue_severity', '').upper(),
                'confidence': r.get('issue_confidence', '').upper(),
                'text':       r.get('issue_text', ''),
                'test_id':    r.get('test_id', ''),
            })
        metrics = data.get('metrics', {}).get('_totals', {})
        return {'ok': True, 'findings': findings, 'metrics': metrics, 'error': ''}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'findings': [], 'metrics': {}, 'error': 'Timeout (bandit)'}
    except json.JSONDecodeError:
        return {'ok': False, 'findings': [], 'metrics': {}, 'error': 'Ungültige bandit Ausgabe'}
    except Exception as e:
        return {'ok': False, 'findings': [], 'metrics': {}, 'error': str(e)}


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


def run_manager_bandit():
    """Run bandit static analysis against the manager source code."""
    appdir   = str(Path(settings.BASE_DIR))
    pip_bin  = os.path.join(settings.MANAGER_VENV, 'bin', 'pip')
    if not os.path.exists(pip_bin):
        return {'ok': False, 'findings': [], 'metrics': {}, 'error': 'Manager-venv nicht gefunden'}

    bandit_bin = os.path.join(settings.MANAGER_VENV, 'bin', 'bandit')
    if not os.path.exists(bandit_bin):
        subprocess.run([pip_bin, 'install', '--quiet', 'bandit'],
                       capture_output=True, timeout=60)

    if not os.path.exists(bandit_bin):
        return {'ok': False, 'findings': [], 'metrics': {}, 'error': 'bandit konnte nicht installiert werden'}

    try:
        # Exclude venvs and legacy monolithic files (superseded by control/utils/ package)
        exclude = ','.join([
            os.path.join(appdir, 'venv'),
            os.path.join(appdir, '.venv'),
            os.path.join(appdir, 'control', 'utils_legacy.py'),
            os.path.join(appdir, 'control', 'utils.py'),
            os.path.join(appdir, 'control', 'views.py'),
        ])
        result = subprocess.run(
            [bandit_bin, '-r', appdir,
             '--exclude', exclude,
             '--skip', 'B404,B603,B110,B105,B112',
             '-f', 'json', '-q'],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout.strip()
        if not output:
            return {'ok': True, 'findings': [], 'metrics': {}, 'error': ''}
        data = json.loads(output)
        findings = []
        for r in data.get('results', []):
            filepath = r.get('filename', '')
            rel = filepath.replace(appdir + '/', '') if filepath.startswith(appdir) else filepath
            findings.append({
                'severity': r.get('issue_severity', ''),
                'confidence': r.get('issue_confidence', ''),
                'text': r.get('issue_text', ''),
                'file': rel,
                'line': r.get('line_number', ''),
            })
        return {'ok': True, 'findings': findings, 'metrics': data.get('metrics', {}), 'error': ''}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'findings': [], 'metrics': {}, 'error': 'Timeout (bandit)'}
    except Exception as e:
        return {'ok': False, 'findings': [], 'metrics': {}, 'error': str(e)}


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
                if appuser else [_BASH, '-c', full_cmd])
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
                if appuser else [_BASH, '-c', full_cmd])
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
                if appuser else [_BASH, '-c', full_cmd])
    try:
        result = subprocess.run(run_args, capture_output=True, text=True, timeout=180)
        output = (result.stdout + result.stderr).strip()
        return {'ok': result.returncode == 0, 'output': output or '(keine Ausgabe)'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'output': 'Timeout nach 180 Sekunden'}
    except Exception as e:
        return {'ok': False, 'output': str(e)}


def run_manager_pip_outdated():
    """List outdated packages in the manager venv."""
    pip_bin = os.path.join(settings.MANAGER_VENV, 'bin', 'pip')
    if not os.path.exists(pip_bin):
        return {'ok': False, 'packages': [], 'error': 'Manager-venv nicht gefunden'}
    try:
        result = subprocess.run(
            [pip_bin, 'list', '--outdated', '--format=json'],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(result.stdout.strip() or '[]')
        packages = [{'name': p['name'], 'current': p['version'], 'latest': p['latest_version']} for p in data]
        return {'ok': True, 'packages': packages, 'error': ''}
    except Exception as e:
        return {'ok': False, 'packages': [], 'error': str(e)}


def run_manager_pip_upgrade(package_name):
    """Upgrade a single package in the manager venv."""
    import re as _re
    if not package_name or not _re.match(r'^[A-Za-z0-9_.\-]+$', package_name):
        return {'ok': False, 'output': 'Ungültiger Paketname'}
    pip_bin = os.path.join(settings.MANAGER_VENV, 'bin', 'pip')
    if not os.path.exists(pip_bin):
        return {'ok': False, 'output': 'Manager-venv nicht gefunden'}
    try:
        result = subprocess.run(
            [pip_bin, 'install', '--upgrade', package_name],
            capture_output=True, text=True, timeout=180,
        )
        output = (result.stdout + result.stderr).strip()
        return {'ok': result.returncode == 0, 'output': output or '(keine Ausgabe)'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'output': 'Timeout nach 180 Sekunden'}
    except Exception as e:
        return {'ok': False, 'output': str(e)}
