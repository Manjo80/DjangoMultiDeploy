"""
Firewall (UFW) utility functions for DjangoMultiDeploy.
"""
import subprocess


def get_ufw_status(gunicorn_port=None):
    """
    Returns dict with ufw status and relevant rules.
    {enabled, rules: [{num, action, to, from, comment}], port_blocked}
    """
    result = {'enabled': False, 'rules': [], 'port_blocked': None, 'available': False}
    try:
        r = subprocess.run(['ufw', 'status', 'numbered'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0 and 'not found' in r.stderr:
            return result
        result['available'] = True
        output = r.stdout
        result['enabled'] = 'Status: active' in output
        for line in output.splitlines():
            # Format: [ 1] 80/tcp                     ALLOW IN    Anywhere
            import re
            m = re.match(r'\[\s*(\d+)\]\s+(\S+)\s+(ALLOW|DENY|REJECT)\s+(\S+)\s*(.*)', line)
            if m:
                result['rules'].append({
                    'num': m.group(1),
                    'to': m.group(2),
                    'action': m.group(3),
                    'frm': m.group(4),
                    'comment': m.group(5).strip(),
                })
        if gunicorn_port:
            port_str = str(gunicorn_port)
            for rule in result['rules']:
                if port_str in rule['to'] and rule['action'] == 'DENY':
                    result['port_blocked'] = True
                    break
            if result['port_blocked'] is None and result['enabled']:
                result['port_blocked'] = False

        # Prüfe wichtige Ports für den Dashboard-Banner
        if result['enabled'] and result['rules']:
            import re as _re

            def _check_port(port_num):
                """'allow' | 'deny' | None (kein Regel)"""
                p = str(port_num)
                for rule in result['rules']:
                    to = rule['to']
                    # Matches: "22", "22/tcp", "22/udp", "22 (v6)"
                    if _re.match(r'^' + p + r'(/\w+)?(\s|$)', to):
                        return rule['action'].lower()
                return None

            def _check_range(lo, hi):
                """'deny' wenn ein DENY-Regel den Bereich abdeckt, sonst 'allow'/'none'"""
                pat = _re.compile(r'^(\d+):(\d+)')
                for rule in result['rules']:
                    m = pat.match(rule['to'])
                    if m and int(m.group(1)) <= lo and int(m.group(2)) >= hi:
                        return rule['action'].lower()
                return None

            result['ports'] = {
                'ssh':      _check_port(22),
                'http':     _check_port(80),
                'https':    _check_port(443),
                'manager':  _check_port(8888),
                'gunicorn': _check_range(8000, 8999),
            }
        else:
            result['ports'] = {}

    except FileNotFoundError:
        pass
    except Exception:
        pass
    return result


def get_ufw_port_rules():
    """
    Returns a list of all current ufw rules with port info.
    [{'port': '8888', 'proto': 'tcp', 'action': 'ALLOW'|'DENY', 'comment': '...'}]
    """
    import re
    rules = []
    try:
        r = subprocess.run(['ufw', 'status', 'verbose'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return rules
        for line in r.stdout.splitlines():
            # e.g. "8888/tcp                   DENY IN     Anywhere"
            # or   "80/tcp (v6)                ALLOW IN    Anywhere (v6)"
            m = re.match(r'(\d+)(?:/(tcp|udp))?\s+(ALLOW|DENY|REJECT)\s+(?:IN\s+)?Anywhere', line)
            if m:
                port  = m.group(1)
                proto = m.group(2) or 'tcp'
                action = m.group(3)
                # extract comment from numbered output if present
                comment_match = re.search(r'#\s*(.+)', line)
                comment = comment_match.group(1).strip() if comment_match else ''
                # avoid duplicates (IPv4/IPv6)
                if not any(r['port'] == port and r['proto'] == proto for r in rules):
                    rules.append({'port': port, 'proto': proto, 'action': action, 'comment': comment})
    except Exception:
        pass
    return rules


def ufw_toggle_port(port, proto, action):
    """
    Open or close a port via ufw.
    action: 'allow' or 'deny'
    Returns (success: bool, message: str)
    """
    import re
    port = str(port).strip()
    proto = proto.strip().lower()
    action = action.strip().lower()

    if not re.match(r'^\d{1,5}$', port) or int(port) > 65535:
        return False, f'Ungültige Port-Nummer: {port}'
    if proto not in ('tcp', 'udp'):
        return False, f'Ungültiges Protokoll: {proto}'
    if action not in ('allow', 'deny'):
        return False, f'Ungültige Aktion: {action}'

    try:
        r = subprocess.run(
            ['ufw', action, f'{port}/{proto}'],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            subprocess.run(['ufw', 'reload'], capture_output=True, timeout=10)
            verb = 'geöffnet' if action == 'allow' else 'gesperrt'
            return True, f'Port {port}/{proto} {verb}.'
        return False, (r.stderr or r.stdout).strip()
    except Exception as e:
        return False, str(e)
