"""
Database inventory + cleanup for DjangoMultiDeploy.

Lists the PostgreSQL / MySQL databases present on the server, cross-references
them with the project registry so orphaned (no longer used by any project)
databases are visible, and allows dropping an orphaned database from the web
UI. System databases and databases still in use by a project can never be
dropped here.

SQLite databases are per-project files (``<appdir>/db.sqlite3``) and are removed
together with the project, so they are not part of this server-level inventory.
"""
import shutil
import subprocess

from .registry import get_all_projects
from .validators import is_valid_db_identifier

_PSQL  = shutil.which('psql')  or '/usr/bin/psql'
_MYSQL = shutil.which('mysql') or '/usr/bin/mysql'
_SU    = shutil.which('su')    or '/bin/su'

# Databases that must never be offered for deletion.
_SYSTEM_DBS = {
    'postgresql': {'postgres', 'template0', 'template1'},
    'mysql':      {'information_schema', 'performance_schema', 'mysql', 'sys'},
}


def _run(*cmd, timeout=20):
    try:
        r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or ''), (r.stderr or '')
    except Exception as e:
        return 1, '', str(e)


def _human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


def _project_db_map():
    """Map (engine, dbname) -> project name for all registered projects."""
    used = {}
    for p in get_all_projects():
        engine = (p.get('DBTYPE') or '').lower()
        dbname = (p.get('DBNAME') or '').strip()
        if engine and dbname:
            used[(engine, dbname)] = p.get('PROJECTNAME', '')
    return used


def _list_postgresql():
    """Return list of PostgreSQL database dicts, or [] if unavailable."""
    if not shutil.which('psql'):
        return []
    query = (
        'SELECT d.datname, pg_database_size(d.datname), '
        'pg_catalog.pg_get_userbyid(d.datdba) '
        'FROM pg_database d WHERE d.datistemplate = false ORDER BY d.datname;'
    )
    rc, out, _err = _run(_SU, '-s', '/bin/bash', 'postgres', '-c',
                         f'psql -tAF"|" -c "{query}"')
    if rc != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split('|')
        if len(parts) < 2 or not parts[0].strip():
            continue
        name = parts[0].strip()
        size = parts[1].strip()
        owner = parts[2].strip() if len(parts) > 2 else ''
        rows.append({'engine': 'postgresql', 'name': name,
                     'size_bytes': size, 'owner': owner})
    return rows


def _list_mysql():
    """Return list of MySQL/MariaDB database dicts, or [] if unavailable."""
    if not shutil.which('mysql'):
        return []
    query = (
        'SELECT s.schema_name, COALESCE(SUM(t.data_length + t.index_length), 0) '
        'FROM information_schema.schemata s '
        'LEFT JOIN information_schema.tables t '
        'ON t.table_schema = s.schema_name '
        'GROUP BY s.schema_name ORDER BY s.schema_name;'
    )
    rc, out, _err = _run(_MYSQL, '-u', 'root', '-N', '-B', '-e', query)
    if rc != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split('\t')
        if not parts or not parts[0].strip():
            continue
        name = parts[0].strip()
        size = parts[1].strip() if len(parts) > 1 else '0'
        rows.append({'engine': 'mysql', 'name': name,
                     'size_bytes': size, 'owner': ''})
    return rows


def list_databases():
    """
    Return a list of database dicts across PostgreSQL and MySQL:
      {engine, name, size_bytes, size_human, owner, project, in_use,
       is_system, deletable}
    Sorted: deletable orphans first, then in-use, then system.
    """
    used = _project_db_map()
    dbs = _list_postgresql() + _list_mysql()

    for db in dbs:
        engine = db['engine']
        name = db['name']
        db['size_human'] = _human_size(db.get('size_bytes'))
        db['is_system'] = name in _SYSTEM_DBS.get(engine, set())
        db['project'] = used.get((engine, name))
        db['in_use'] = db['project'] is not None
        db['deletable'] = not db['is_system'] and not db['in_use']

    def _sort_key(d):
        # orphaned/deletable (0) first, in-use (1), system (2)
        if d['is_system']:
            grp = 2
        elif d['in_use']:
            grp = 1
        else:
            grp = 0
        return (grp, d['engine'], d['name'])

    return sorted(dbs, key=_sort_key)


def drop_database(dbtype, dbname):
    """
    Drop an orphaned database. Returns (ok, message).
    Refuses system databases and any database still referenced by a project.
    """
    dbtype = (dbtype or '').lower()
    if dbtype not in ('postgresql', 'mysql'):
        return False, f'Nicht unterstützter DB-Typ: {dbtype!r}'
    if not is_valid_db_identifier(dbname):
        return False, f'Ungültiger DB-Name: {dbname!r}'
    if dbname in _SYSTEM_DBS.get(dbtype, set()):
        return False, f'System-Datenbank "{dbname}" kann nicht gelöscht werden.'

    # Never drop a database that is still used by a registered project.
    used = _project_db_map()
    if (dbtype, dbname) in used:
        return False, (f'Datenbank "{dbname}" wird noch von Projekt '
                       f'"{used[(dbtype, dbname)]}" genutzt — Löschen abgelehnt.')

    if dbtype == 'postgresql':
        rc, out, err = _run(_SU, '-s', '/bin/bash', 'postgres', '-c',
                            f'psql -c "DROP DATABASE IF EXISTS \\"{dbname}\\";"')
    else:
        rc, out, err = _run(_MYSQL, '-u', 'root', '-e',
                            f'DROP DATABASE IF EXISTS `{dbname}`;')

    if rc == 0:
        return True, f'Datenbank "{dbname}" ({dbtype}) gelöscht.'
    return False, (err or out or 'Löschen fehlgeschlagen').strip()[:400]
