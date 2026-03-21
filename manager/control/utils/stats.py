"""
Statistics utility functions for DjangoMultiDeploy.
"""
import os
import subprocess
import datetime
from collections import defaultdict


def get_nginx_stats(name, max_lines=20000):
    """
    Parse per-project nginx access log.
    Format: IP - user [DD/Mon/YYYY:time] "METHOD /path HTTP/x" STATUS bytes "ref" "ua" [req_time]
    Returns dict: total, by_status, by_day (last 7), top_urls, top_ips, avg_rt, has_rt
    """
    import re
    import datetime
    from collections import defaultdict

    log_path = f'/var/log/nginx/{name}.access.log'
    fallback = '/var/log/nginx/access.log'
    result = {
        'available': False,
        'log_path': log_path,
        'total': 0,
        'by_status': {},
        'by_day': [],
        'top_urls': [],
        'top_ips': [],
        'avg_rt': None,
        'has_rt': False,
    }
    path = log_path if os.path.isfile(log_path) else fallback if os.path.isfile(fallback) else None
    if not path:
        return result
    result['available'] = True
    result['log_path'] = path

    pat = re.compile(
        r'(?P<ip>\S+) - \S+ \[(?P<day>\d{2}/\w{3}/\d{4}):[^\]]+\] '
        r'"(?P<method>\S+) (?P<url>\S+) [^"]*" '
        r'(?P<status>\d+) \S+ "[^"]*" "[^"]*"'
        r'(?: (?P<rt>[\d.]+))?'
    )
    _mo = {'Jan': '01', 'Feb': '02', 'Mar': '03', 'Apr': '04', 'May': '05', 'Jun': '06',
           'Jul': '07', 'Aug': '08', 'Sep': '09', 'Oct': '10', 'Nov': '11', 'Dec': '12'}

    today = datetime.date.today()
    last7_fmt = [(today - datetime.timedelta(days=i)).strftime('%d/%b/%Y') for i in range(6, -1, -1)]
    by_day = {d: 0 for d in last7_fmt}
    by_status = defaultdict(int)
    url_counts = defaultdict(int)
    ip_counts = defaultdict(int)
    rt_total, rt_count = 0.0, 0

    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            fsize = f.tell()
            seek_pos = max(0, fsize - max_lines * 250)
            f.seek(seek_pos)
            if seek_pos > 0:
                f.readline()
            raw = f.read()
        lines = raw.decode('utf-8', errors='replace').splitlines()
    except OSError:
        return result

    for line in lines:
        m = pat.match(line)
        if not m:
            continue
        result['total'] += 1
        status = m.group('status')
        by_status[status[0] + 'xx'] += 1
        day = m.group('day')
        if day in by_day:
            by_day[day] += 1
        url = m.group('url')
        if not any(url.startswith(p) for p in ('/static/', '/media/', '/favicon')):
            url_counts[url] += 1
        ip_counts[m.group('ip')] += 1
        if m.group('rt'):
            try:
                rt_total += float(m.group('rt'))
                rt_count += 1
            except ValueError:
                pass

    result['by_status'] = dict(sorted(by_status.items()))
    result['by_day'] = [
        {'label': d.split('/')[0] + '.' + _mo.get(d.split('/')[1], '?'), 'count': by_day[d]}
        for d in last7_fmt
    ]
    result['top_urls'] = sorted(url_counts.items(), key=lambda x: -x[1])[:10]
    result['top_ips'] = sorted(ip_counts.items(), key=lambda x: -x[1])[:10]
    if rt_count:
        result['has_rt'] = True
        result['avg_rt'] = round(rt_total / rt_count * 1000)  # ms
    return result


def get_service_restarts(name, days=14):
    """
    Query systemd journal for start/fail/stop events of a service in the last N days.
    Returns {'available': bool, 'events': [...], 'starts': int, 'failures': int}
    """
    import datetime
    since = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime('%Y-%m-%d')
    result = {'available': False, 'events': [], 'starts': 0, 'failures': 0, 'stops': 0}
    try:
        r = subprocess.run(
            ['journalctl', '-u', name, '--since', since, '--no-pager',
             '-o', 'short-iso', '--grep',
             'Started|Failed|Stopped|Restarting|Main process exited'],
            capture_output=True, text=True, timeout=10
        )
        result['available'] = True
        events = []
        for line in r.stdout.splitlines():
            if not line or line.startswith('--'):
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            ts, msg = parts[0], parts[1]
            if 'Started' in msg:
                kind = 'start'
                result['starts'] += 1
            elif 'Failed' in msg or 'exited' in msg or 'failed' in msg:
                kind = 'fail'
                result['failures'] += 1
            elif 'Stopped' in msg or 'Stopping' in msg:
                kind = 'stop'
                result['stops'] += 1
            else:
                kind = 'info'
            display = msg.split(': ', 1)[-1] if ': ' in msg else msg
            # Format timestamp: 2024-01-15T10:30:00+0100 -> 15.01. 10:30
            try:
                dt_str = ts[:16]  # 2024-01-15T10:30
                from datetime import datetime as dt
                dobj = dt.fromisoformat(dt_str)
                ts_disp = dobj.strftime('%d.%m. %H:%M')
            except Exception:
                ts_disp = ts[:16]
            events.append({'time': ts_disp, 'event': kind, 'msg': display[:120]})
        result['events'] = list(reversed(events))[-50:]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return result


def get_server_stats():
    """Return basic server resource stats: RAM, disk, load average."""
    stats = {}
    # Memory
    try:
        with open('/proc/meminfo') as f:
            mem = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(':')] = int(parts[1])
        total_kb = mem.get('MemTotal', 0)
        available_kb = mem.get('MemAvailable', 0)
        stats['mem_total_mb'] = round(total_kb / 1024)
        stats['mem_used_mb'] = round((total_kb - available_kb) / 1024)
        stats['mem_percent'] = round((total_kb - available_kb) / total_kb * 100) if total_kb else 0
    except Exception:
        stats['mem_total_mb'] = stats['mem_used_mb'] = stats['mem_percent'] = None
    # Disk (root filesystem)
    try:
        st = os.statvfs('/')
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
        used = total - free
        stats['disk_total_gb'] = round(total / 1024 ** 3, 1)
        stats['disk_used_gb'] = round(used / 1024 ** 3, 1)
        stats['disk_percent'] = round(used / total * 100) if total else 0
    except Exception:
        stats['disk_total_gb'] = stats['disk_used_gb'] = stats['disk_percent'] = None
    # Load average
    try:
        with open('/proc/loadavg') as f:
            parts = f.read().split()
        stats['load1'] = parts[0]
        stats['load5'] = parts[1]
        stats['load15'] = parts[2]
    except Exception:
        stats['load1'] = stats['load5'] = stats['load15'] = None
    return stats
