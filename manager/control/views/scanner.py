"""
DjangoMultiDeploy Manager — Security scanner views.
Contains: security_scanner_view, security_scanner_run, port_scan_run,
          scan_log_view, clear_scan_log
"""
import ipaddress
import logging
import concurrent.futures

from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required

from ..utils import run_http_security_scan, get_public_ip, run_port_scan
from ..utils.scan_log import get_log_entries, clear_log

_SCAN_TIMEOUT = 90  # seconds — Gunicorn worker timeout is 120s

logger = logging.getLogger('djmanager.scanner')


# ──────────────────────────────────────────────────────────────────────────────
# Security Scanner — custom hostname / port scan
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def security_scanner_view(request):
    """Render the standalone Security Scanner page."""
    if not request.user.is_staff:
        return render(request, 'control/403.html', status=403)
    pub_ipv4, pub_ipv6 = get_public_ip()
    return render(request, 'control/security_scanner.html', {
        'pub_ipv4': pub_ipv4,
        'pub_ipv6': pub_ipv6,
    })


@login_required
def security_scanner_run(request):
    """
    Run HTTP/TLS security scan on an arbitrary hostname/IP entered by the user.
    GET params:
      target=<hostname or IP>
      port=<int>         (optional, default 443)
      tls=1|0            (optional, default 1 if port==443 else 0)
    """
    if not request.user.is_staff:
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)

    target = request.GET.get('target', '').strip()
    if not target:
        return JsonResponse({'error': 'Kein Ziel angegeben'}, status=400)

    port_str = request.GET.get('port', '').strip()
    tls_param = request.GET.get('tls', '').strip()

    try:
        port = int(port_str) if port_str else None
    except ValueError:
        return JsonResponse({'error': 'Ungültiger Port'}, status=400)

    # IPv6 addresses must be wrapped in brackets in URLs
    try:
        addr = ipaddress.ip_address(target)
        url_host = f'[{target}]' if isinstance(addr, ipaddress.IPv6Address) else target
    except ValueError:
        url_host = target

    if port is None:
        port = 443

    if port == 443:
        url = f'https://{url_host}/'
        check_tls = True
    elif port == 80:
        url = f'http://{url_host}/'
        check_tls = False
    else:
        # Assume HTTPS for non-80 custom ports (can be overridden)
        scheme = 'https'
        check_tls = True
        if tls_param == '0':
            scheme = 'http'
            check_tls = False
        url = f'{scheme}://{url_host}:{port}/'

    # Allow tls param to override
    if tls_param == '1':
        check_tls = True
    elif tls_param == '0':
        check_tls = False

    hostname = target if target else None
    logger.info('HTTP-Scan angefordert von %s für %s', request.user, url)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(run_http_security_scan, url, hostname=hostname, check_tls=check_tls)
        try:
            result = fut.result(timeout=_SCAN_TIMEOUT)
        except concurrent.futures.TimeoutError:
            logger.warning('HTTP-Scan Timeout nach %ds: %s (User: %s)', _SCAN_TIMEOUT, url, request.user)
            return JsonResponse({'error': f'Scan-Timeout: Die Verbindung hat nach {_SCAN_TIMEOUT}s nicht geantwortet.'})
    return JsonResponse(result)


@login_required
def port_scan_run(request):
    """
    Run a TCP port scan on an arbitrary host.
    GET params:
      target=<hostname or IP>
      mode=common|range   (default: common)
      from=<int>          (for range mode, default 1)
      to=<int>            (for range mode, default 1024)
    """
    if not request.user.is_staff:
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)

    target = request.GET.get('target', '').strip()
    if not target:
        return JsonResponse({'error': 'Kein Ziel angegeben'}, status=400)

    mode = request.GET.get('mode', 'common')
    if mode not in ('common', 'range'):
        mode = 'common'

    try:
        port_from = int(request.GET.get('from', 1))
        port_to   = int(request.GET.get('to', 1024))
    except ValueError:
        return JsonResponse({'error': 'Ungültiger Port-Bereich'}, status=400)

    logger.info('Port-Scan angefordert von %s für %s (Modus=%s)', request.user, target, mode)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(run_port_scan, target, mode=mode, port_start=port_from, port_end=port_to)
        try:
            result = fut.result(timeout=_SCAN_TIMEOUT)
        except concurrent.futures.TimeoutError:
            logger.warning('Port-Scan Timeout nach %ds: %s (User: %s)', _SCAN_TIMEOUT, target, request.user)
            return JsonResponse({'error': f'Port-Scan Timeout nach {_SCAN_TIMEOUT}s.'})
    return JsonResponse(result)


# ──────────────────────────────────────────────────────────────────────────────
# Scan-Log (in-memory, kein Neustart nötig)
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def scan_log_view(request):
    """Return the in-memory scan log as JSON (staff only)."""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)
    return JsonResponse({'entries': get_log_entries()})


@login_required
def clear_scan_log(request):
    """Clear the in-memory scan log (POST, staff only)."""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Zugriff verweigert'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST erforderlich'}, status=405)
    clear_log()
    logger.info('Scan-Log geleert von %s', request.user)
    return JsonResponse({'ok': True})
