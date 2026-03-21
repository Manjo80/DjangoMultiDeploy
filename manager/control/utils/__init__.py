"""
DjangoMultiDeploy Manager — Utils package

Re-exports all public utility functions and constants from submodules so that
`from control.utils import X` continues to work unchanged.
"""

from .registry import (
    _parse_conf,
    get_all_projects,
    get_project,
    get_service_status,
    service_action,
    get_journal_logs,
    get_nginx_log,
    set_project_conf_value,
)

from .backup import (
    list_backups,
    run_backup,
    delete_backup,
    get_last_backup,
)

from .deployment import (
    run_update,
    extract_project_zip,
    update_project_from_zip,
    remove_project,
    start_install,
    run_management_command,
)

from .deploy_keys import (
    KEYS_DIR,
    GLOBAL_DEPLOY_KEY,
    create_deploy_key,
    list_deploy_keys,
    get_deploy_key_pubkey,
    delete_deploy_key,
    assign_project_deploy_key,
    get_project_deploy_key,
    get_global_deploy_key,
    get_ssh_key,
)

from .config import (
    get_allowed_hosts,
    get_nginx_server_names,
    update_allowed_hosts,
    sync_env_to_conf,
)

from .firewall import (
    get_ufw_status,
    get_ufw_port_rules,
    ufw_toggle_port,
)

from .stats import (
    get_nginx_stats,
    get_service_restarts,
    get_server_stats,
)

from .pip_utils import (
    run_pip_audit,
    run_django_deploy_check,
    run_manager_pip_audit,
    run_manager_deploy_check,
    run_migration_status,
    run_pip_outdated,
    run_pip_upgrade,
)

from .scanning import (
    run_http_security_scan,
    get_public_ip,
    run_port_scan,
)

__all__ = [
    # registry
    '_parse_conf', 'get_all_projects', 'get_project', 'get_service_status',
    'service_action', 'get_journal_logs', 'get_nginx_log', 'set_project_conf_value',
    # backup
    'list_backups', 'run_backup', 'delete_backup', 'get_last_backup',
    # deployment
    'run_update', 'extract_project_zip', 'update_project_from_zip',
    'remove_project', 'start_install', 'run_management_command',
    # deploy_keys
    'KEYS_DIR', 'GLOBAL_DEPLOY_KEY',
    'create_deploy_key', 'list_deploy_keys', 'get_deploy_key_pubkey',
    'delete_deploy_key', 'assign_project_deploy_key', 'get_project_deploy_key',
    'get_global_deploy_key', 'get_ssh_key',
    # config
    'get_allowed_hosts', 'get_nginx_server_names', 'update_allowed_hosts', 'sync_env_to_conf',
    # firewall
    'get_ufw_status', 'get_ufw_port_rules', 'ufw_toggle_port',
    # stats
    'get_nginx_stats', 'get_service_restarts', 'get_server_stats',
    # pip_utils
    'run_pip_audit', 'run_django_deploy_check', 'run_manager_pip_audit',
    'run_manager_deploy_check', 'run_migration_status', 'run_pip_outdated', 'run_pip_upgrade',
    # scanning
    'run_http_security_scan', 'get_public_ip', 'run_port_scan',
]
