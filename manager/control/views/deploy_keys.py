"""
DjangoMultiDeploy Manager — Deploy key views.
Contains: deploy_keys_list, deploy_key_detail, deploy_key_create, deploy_key_delete,
          project_assign_key, project_deploy_key, project_deploy_key_download,
          _github_keys_url
"""
import logging
import re

from django.shortcuts import render, redirect
from django.urls import reverse
from django.http import HttpResponse, JsonResponse, Http404
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required

from ..models import AuditLog
from ..utils import (
    get_all_projects, get_project, list_deploy_keys, create_deploy_key,
    get_deploy_key_pubkey, delete_deploy_key, assign_project_deploy_key,
    get_project_deploy_key,
)
from ..utils import _load_key_registry
from ._helpers import admin_required, operator_required, _check_project_access

logger = logging.getLogger('djmanager.views.deploy_keys')


# ──────────────────────────────────────────────────────────────────────────────
# Deploy Key Registry (global list + CRUD)
# ──────────────────────────────────────────────────────────────────────────────

@admin_required
def deploy_keys_list(request):
    keys = list_deploy_keys()
    all_projects = get_all_projects()
    return render(request, 'control/deploy_keys_list.html', {
        'keys': keys,
        'all_projects': all_projects,
    })


@admin_required
def deploy_key_detail(request, key_id):
    """Show the public key for a registry entry."""
    pub_key, error = get_deploy_key_pubkey(key_id)
    meta = _load_key_registry().get(key_id, {'id': key_id, 'label': key_id})
    if request.GET.get('download') and pub_key:
        label = meta.get('label', key_id).replace(' ', '_')
        resp  = HttpResponse(pub_key + '\n', content_type='text/plain')
        resp['Content-Disposition'] = f'attachment; filename="{label}_{key_id}.pub"'
        return resp
    return render(request, 'control/deploy_key_detail.html', {
        'key_id':  key_id,
        'meta':    meta,
        'pub_key': pub_key,
        'error':   error,
    })


@require_POST
@admin_required
def deploy_key_create(request):
    from datetime import datetime
    label = request.POST.get('label', '').strip()
    if not label:
        label = f'Key {datetime.now().strftime("%Y-%m-%d")}'
    key_id, _pub, error = create_deploy_key(label)
    if error:
        return JsonResponse({'ok': False, 'error': error}, status=400)
    # If a project was requested, auto-assign and redirect to its key page
    project = request.POST.get('project', '').strip()
    if project and key_id:
        assign_project_deploy_key(project, key_id)
        return redirect('project_deploy_key', project=project)
    return redirect('deploy_keys_list')


@require_POST
@admin_required
def deploy_key_delete(request, key_id):
    ok, error = delete_deploy_key(key_id)
    if not ok:
        # Return to list with an error message via query param
        from urllib.parse import urlencode
        return redirect(f"{reverse('deploy_keys_list')}?error={error}")
    return redirect('deploy_keys_list')


@require_POST
@operator_required
def project_assign_key(request, project):
    if not _check_project_access(request.user, project):
        return render(request, 'control/403.html', status=403)
    key_id = request.POST.get('key_id', '').strip()
    ok, error = assign_project_deploy_key(project, key_id)
    if not ok:
        return JsonResponse({'ok': False, 'error': error}, status=400)
    return redirect('project_deploy_key', project=project)


# ──────────────────────────────────────────────────────────────────────────────
# Per-Project GitHub Deploy Key
# ──────────────────────────────────────────────────────────────────────────────

def _github_keys_url(conf):
    """Return direct URL to GitHub repo deploy keys page, or None."""
    import re as _re
    url = conf.get('GITHUB_REPO_URL') or conf.get('GITHUB', '')
    if not url:
        return None
    m = _re.search(r'[:/]([^/:]+/[^/]+?)(?:\.git)?$', url)
    return f'https://github.com/{m.group(1)}/settings/keys' if m else None


@login_required
def project_deploy_key(request, project):
    if not _check_project_access(request.user, project):
        return render(request, 'control/403.html', status=403)
    conf = get_project(project)
    pub_key, error = get_project_deploy_key(project)
    all_keys = list_deploy_keys()
    current_key_id = (conf or {}).get('DEPLOY_KEY_ID', '').strip()
    return render(request, 'control/project_deploy_key.html', {
        'project':         project,
        'pub_key':         pub_key,
        'error':           error,
        'conf':            conf,
        'github_keys_url': _github_keys_url(conf or {}),
        'all_keys':        all_keys,
        'current_key_id':  current_key_id,
    })


@login_required
def project_deploy_key_download(request, project):
    if not _check_project_access(request.user, project):
        raise Http404()
    pub_key, error = get_project_deploy_key(project)
    if error:
        raise Http404(error)
    response = HttpResponse(pub_key + '\n', content_type='text/plain')
    response['Content-Disposition'] = f'attachment; filename="deploy_{project}_ed25519.pub"'
    return response
