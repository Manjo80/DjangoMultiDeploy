"""
Deploy key management utilities for DjangoMultiDeploy.
"""
import os
import json
import subprocess

from .registry import get_all_projects, get_project


GLOBAL_DEPLOY_KEY = '/root/.ssh/djmanager_github_ed25519'

# ── Deploy Key Registry ────────────────────────────────────────────────────────
KEYS_DIR      = '/root/.ssh/djmanager_keys'
KEYS_REGISTRY = '/root/.ssh/djmanager_keys/registry.json'


def _load_key_registry():
    """Return the key registry dict (id → metadata). Never raises."""
    import json
    try:
        with open(KEYS_REGISTRY) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_key_registry(registry):
    import json
    os.makedirs(KEYS_DIR, mode=0o700, exist_ok=True)
    with open(KEYS_REGISTRY, 'w') as f:
        json.dump(registry, f, indent=2)
    os.chmod(KEYS_REGISTRY, 0o600)


def create_deploy_key(label):
    """
    Create a new ed25519 deploy key pair, store in KEYS_DIR and registry.
    Returns (key_id, pub_key_content, error).
    """
    import uuid, json
    from datetime import datetime
    key_id  = uuid.uuid4().hex[:12]
    priv    = os.path.join(KEYS_DIR, f'{key_id}_ed25519')
    pub     = priv + '.pub'
    try:
        import socket
        comment = f'djmanager-{key_id}@{socket.getfqdn()}'
        os.makedirs(KEYS_DIR, mode=0o700, exist_ok=True)
        subprocess.run(
            ['ssh-keygen', '-t', 'ed25519', '-C', comment, '-f', priv, '-N', ''],
            check=True, capture_output=True,
        )
        os.chmod(priv, 0o600)
        os.chmod(pub,  0o644)
    except Exception as e:
        return None, None, f'Key konnte nicht erstellt werden: {e}'
    # Get fingerprint
    try:
        fp_result = subprocess.run(
            ['ssh-keygen', '-lf', pub], capture_output=True, text=True
        )
        fingerprint = fp_result.stdout.split()[1] if fp_result.returncode == 0 else ''
    except Exception:
        fingerprint = ''
    with open(pub) as f:
        pub_content = f.read().strip()
    # Add to registry
    registry = _load_key_registry()
    registry[key_id] = {
        'id':          key_id,
        'label':       label or key_id,
        'created_at':  datetime.now().isoformat(timespec='seconds'),
        'fingerprint': fingerprint,
    }
    _save_key_registry(registry)
    return key_id, pub_content, None


def list_deploy_keys():
    """
    Return list of key dicts, each with an extra 'projects' list of project
    names that currently use this key (via DEPLOY_KEY_ID in their .conf).
    """
    registry = _load_key_registry()
    # Build project → key_id map
    proj_key = {}
    for p in get_all_projects():
        kid = p.get('DEPLOY_KEY_ID', '').strip()
        if kid:
            proj_key.setdefault(kid, []).append(p['PROJECTNAME'])
    keys = []
    for key_id, meta in registry.items():
        pub = os.path.join(KEYS_DIR, f'{key_id}_ed25519.pub')
        keys.append({
            **meta,
            'projects':  proj_key.get(key_id, []),
            'pub_exists': os.path.exists(pub),
        })
    keys.sort(key=lambda k: k.get('created_at', ''))
    return keys


def get_deploy_key_pubkey(key_id):
    """Return (pub_key_content, error)."""
    pub = os.path.join(KEYS_DIR, f'{key_id}_ed25519.pub')
    if not os.path.exists(pub):
        return None, f'Public Key nicht gefunden (ID: {key_id})'
    try:
        with open(pub) as f:
            return f.read().strip(), None
    except OSError as e:
        return None, str(e)


def delete_deploy_key(key_id):
    """
    Delete key files and remove from registry.
    Returns (ok, error). Refuses if any project still uses this key.
    """
    # Check assignments
    for p in get_all_projects():
        if p.get('DEPLOY_KEY_ID', '').strip() == key_id:
            return False, f'Key wird noch von Projekt "{p["PROJECTNAME"]}" verwendet.'
    registry = _load_key_registry()
    if key_id not in registry:
        return False, 'Key nicht in Registry gefunden.'
    for suffix in ('_ed25519', '_ed25519.pub'):
        path = os.path.join(KEYS_DIR, key_id + suffix)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            return False, str(e)
    del registry[key_id]
    _save_key_registry(registry)
    return True, None


def assign_project_deploy_key(project, key_id):
    """
    Assign deploy key key_id to project:
    - writes DEPLOY_KEY_ID to the project's .conf
    - patches the update script to use the correct key path
    Returns (ok, error).
    """
    import re as _re
    from .registry import set_project_conf_value
    registry = _load_key_registry()
    if key_id and key_id not in registry:
        return False, f'Key ID "{key_id}" nicht in Registry.'
    ok, err = set_project_conf_value(project, 'DEPLOY_KEY_ID', key_id)
    if not ok:
        return False, err
    # Patch update script
    if key_id:
        key_path = os.path.join(KEYS_DIR, f'{key_id}_ed25519')
        script   = f'/usr/local/bin/{project}_update.sh'
        if os.path.exists(script):
            try:
                with open(script) as f:
                    content = f.read()
                new_content = _re.sub(
                    r'GITHUB_DEPLOY_KEY="[^"]*"',
                    f'GITHUB_DEPLOY_KEY="{key_path}"',
                    content,
                )
                if new_content != content:
                    with open(script, 'w') as f:
                        f.write(new_content)
            except OSError:
                pass
    return True, None


def get_project_deploy_key(project):
    """
    Return (pub_key_content, error) for the project's currently assigned key.
    Falls back to legacy /root/.ssh/deploy_{project}_ed25519 if no registry key.
    """
    conf = get_project(project)
    if not conf:
        return None, 'Projekt nicht gefunden'
    key_id = conf.get('DEPLOY_KEY_ID', '').strip()
    if key_id:
        return get_deploy_key_pubkey(key_id)
    # Legacy fallback
    legacy = f'/root/.ssh/deploy_{project}_ed25519.pub'
    if os.path.exists(legacy):
        try:
            with open(legacy) as f:
                return f.read().strip(), None
        except OSError as e:
            return None, str(e)
    return None, 'Kein Deploy Key zugewiesen.'


def get_global_deploy_key():
    """Return (pubkey_content, error). Creates the key if it doesn't exist."""
    pub_path = GLOBAL_DEPLOY_KEY + '.pub'
    if not os.path.exists(GLOBAL_DEPLOY_KEY):
        try:
            import socket
            comment = f'djmanager@{socket.getfqdn()}'
            os.makedirs('/root/.ssh', mode=0o700, exist_ok=True)
            subprocess.run(
                ['ssh-keygen', '-t', 'ed25519', '-C', comment,
                 '-f', GLOBAL_DEPLOY_KEY, '-N', ''],
                check=True, capture_output=True
            )
            os.chmod(GLOBAL_DEPLOY_KEY, 0o600)
            os.chmod(pub_path, 0o644)
        except Exception as e:
            return None, f'Key konnte nicht erstellt werden: {e}'
    if not os.path.exists(pub_path):
        return None, f'Public Key nicht gefunden: {pub_path}'
    try:
        with open(pub_path) as f:
            return f.read().strip(), None
    except OSError as e:
        return None, str(e)


def get_ssh_key(project):
    """Return the SSH private key content for a project's app user."""
    conf = get_project(project)
    if not conf:
        return None, 'Project not found'
    key_path = conf.get('SSH_KEY_PATH') or f"/home/{conf.get('APPUSER', '')}/.ssh/id_ed25519"
    if not os.path.exists(key_path):
        return None, f'Key not found: {key_path}'
    try:
        with open(key_path) as f:
            return f.read(), None
    except OSError as e:
        return None, str(e)
