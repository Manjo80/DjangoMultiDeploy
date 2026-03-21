"""
DjangoMultiDeploy Manager — Views package.
Re-exports all view functions for backwards compatibility with urls.py.
"""
from .auth import login_view, logout_view, two_factor_setup, two_factor_verify, profile_view
from .users import user_list, user_create, user_edit, user_delete
from .admin_views import (
    audit_log_view, security_settings_view, manager_settings_view,
    manager_env_view, project_env_view, firewall_view,
    manager_action, manager_update, manager_security_scan, manager_http_scan,
)
from .dashboard import dashboard
from .install import (
    install_form, install_run, install_progress, install_poll,
    install_manager, install_kill,
    ssh_key_display, ssh_key_download, ssh_key_confirm,
    global_deploy_key, global_deploy_key_download,
)
from .deploy_keys import (
    deploy_keys_list, deploy_key_detail, deploy_key_create, deploy_key_delete,
    project_assign_key, project_deploy_key, project_deploy_key_download,
)
from .projects import (
    project_detail, project_allowed_hosts, project_action,
    backup_delete, project_upload_zip, project_stats,
    project_security_scan, project_http_scan,
    log_viewer, remove_confirm, remove_run, remove_done,
    project_update, project_favorite_commands,
    project_migrations, project_pip_outdated, project_pip_upgrade,
    project_clone_form, project_clone_run,
)
from .scanner import security_scanner_view, security_scanner_run, port_scan_run, scan_log_view, clear_scan_log

__all__ = [
    # auth
    'login_view', 'logout_view', 'two_factor_setup', 'two_factor_verify', 'profile_view',
    # users
    'user_list', 'user_create', 'user_edit', 'user_delete',
    # admin
    'audit_log_view', 'security_settings_view', 'manager_settings_view',
    'manager_env_view', 'project_env_view', 'firewall_view',
    'manager_action', 'manager_update', 'manager_security_scan', 'manager_http_scan',
    # dashboard
    'dashboard',
    # install
    'install_form', 'install_run', 'install_progress', 'install_poll',
    'install_manager', 'install_kill',
    'ssh_key_display', 'ssh_key_download', 'ssh_key_confirm',
    'global_deploy_key', 'global_deploy_key_download',
    # deploy keys
    'deploy_keys_list', 'deploy_key_detail', 'deploy_key_create', 'deploy_key_delete',
    'project_assign_key', 'project_deploy_key', 'project_deploy_key_download',
    # projects
    'project_detail', 'project_allowed_hosts', 'project_action',
    'backup_delete', 'project_upload_zip', 'project_stats',
    'project_security_scan', 'project_http_scan',
    'log_viewer', 'remove_confirm', 'remove_run', 'remove_done',
    'project_update', 'project_favorite_commands',
    'project_migrations', 'project_pip_outdated', 'project_pip_upgrade',
    'project_clone_form', 'project_clone_run',
    # scanner
    'security_scanner_view', 'security_scanner_run', 'port_scan_run',
    'scan_log_view', 'clear_scan_log',
]
