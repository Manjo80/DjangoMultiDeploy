"""
Backup utility functions for DjangoMultiDeploy.
"""
import os
import glob
import subprocess
import datetime

from .registry import get_project


def list_backups(project):
    """Return sorted list of backup file paths for a project."""
    backup_dir = f'/var/backups/{project}'
    if not os.path.isdir(backup_dir):
        return []
    files = sorted(
        glob.glob(os.path.join(backup_dir, '*.tar.gz')),
        reverse=True
    )
    import datetime
    result = []
    for f in files:
        stat = os.stat(f)
        result.append({
            'path': f,
            'name': os.path.basename(f),
            'size_mb': round(stat.st_size / 1024 / 1024, 2),
            'mtime': stat.st_mtime,
            'mtime_str': datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%d.%m.%Y %H:%M'),
        })
    return result


def run_backup(name):
    """Run the project backup script. Returns (ok, output)."""
    script = f'/usr/local/bin/{name}_backup.sh'
    if not os.path.exists(script):
        return False, f'Backup script not found: {script}'
    try:
        result = subprocess.run(
            [script],
            capture_output=True, text=True, timeout=300
        )
        return result.returncode == 0, (result.stdout + result.stderr)
    except Exception as e:
        return False, str(e)


def delete_backup(project, filename):
    """
    Delete a single backup file for a project.
    Returns (ok, message). Path-traversal protected.
    """
    # Reject any filename with path components
    if not filename or os.path.basename(filename) != filename or '/' in filename or '..' in filename:
        return False, 'Ungültiger Dateiname'
    if not filename.endswith('.tar.gz'):
        return False, 'Nur .tar.gz Dateien können gelöscht werden'
    backup_dir = f'/var/backups/{project}'
    full_path = os.path.join(backup_dir, filename)
    # Resolve and verify the path stays inside backup_dir
    try:
        real_path = os.path.realpath(full_path)
        real_dir = os.path.realpath(backup_dir)
        if not real_path.startswith(real_dir + os.sep):
            return False, 'Zugriff verweigert'
    except Exception as e:
        return False, str(e)
    if not os.path.isfile(real_path):
        return False, f'Datei nicht gefunden: {filename}'
    try:
        os.remove(real_path)
        return True, f'{filename} gelöscht'
    except OSError as e:
        return False, str(e)


def get_last_backup(project):
    """Return mtime of most recent backup, or None."""
    backups = list_backups(project)
    if backups:
        import datetime
        ts = backups[0]['mtime']
        return datetime.datetime.fromtimestamp(ts).strftime('%d.%m.%Y %H:%M')
    return None
