from django.urls import path
from . import views

urlpatterns = [
    # Dashboard
    path('dashboard/', views.dashboard, name='dashboard'),

    # Install wizard
    path('install/', views.install_form, name='install_form'),
    path('install/run/', views.install_run, name='install_run'),
    path('install/progress/<str:project>/<str:run_id>/', views.install_progress, name='install_progress'),
    path('install/poll/<str:log_name>/', views.install_poll, name='install_stream'),
    path('install/ssh-key/<str:project>/', views.ssh_key_display, name='ssh_key_display'),
    path('install/ssh-key/<str:project>/confirm/', views.ssh_key_confirm, name='ssh_key_confirm'),
    path('install/ssh-key/<str:project>/download/', views.ssh_key_download, name='ssh_key_download'),

    # Global GitHub deploy key (legacy)
    path('deploy-key/', views.global_deploy_key, name='global_deploy_key'),
    path('deploy-key/download/', views.global_deploy_key_download, name='global_deploy_key_download'),

    # Deploy Key Registry
    path('deploy-keys/', views.deploy_keys_list, name='deploy_keys_list'),
    path('deploy-keys/create/', views.deploy_key_create, name='deploy_key_create'),
    path('deploy-keys/<str:key_id>/', views.deploy_key_detail, name='deploy_key_detail'),
    path('deploy-keys/<str:key_id>/delete/', views.deploy_key_delete, name='deploy_key_delete'),

    # Per-project GitHub deploy key + assign
    path('project/<str:project>/deploy-key/', views.project_deploy_key, name='project_deploy_key'),
    path('project/<str:project>/deploy-key/download/', views.project_deploy_key_download, name='project_deploy_key_download'),
    path('project/<str:project>/deploy-key/assign/', views.project_assign_key, name='project_assign_key'),

    # Project management
    path('project/<str:name>/', views.project_detail, name='project_detail'),
    path('project/<str:name>/action/', views.project_action, name='project_action'),
    path('project/<str:name>/update/', views.project_update, name='project_update'),
    path('project/<str:name>/allowed-hosts/', views.project_allowed_hosts, name='project_allowed_hosts'),
    path('project/<str:name>/backup/delete/', views.backup_delete, name='backup_delete'),
    path('project/<str:name>/upload-zip/', views.project_upload_zip, name='project_upload_zip'),
    path('project/<str:name>/stats/', views.project_stats, name='project_stats'),
    path('project/<str:name>/security-scan/', views.project_security_scan, name='project_security_scan'),
    path('project/<str:name>/logs/', views.log_viewer, name='log_viewer'),
    path('project/<str:name>/remove/', views.remove_confirm, name='remove_confirm'),
    path('project/<str:name>/remove/run/', views.remove_run, name='remove_run'),
    path('remove/done/', views.remove_done, name='remove_done'),

    # 2FA
    path('2fa/setup/', views.two_factor_setup, name='two_factor_setup'),
    path('2fa/verify/', views.two_factor_verify, name='two_factor_verify'),

    # User management (admin only)
    path('users/', views.user_list, name='user_list'),
    path('users/create/', views.user_create, name='user_create'),
    path('users/<int:uid>/edit/', views.user_edit, name='user_edit'),
    path('users/<int:uid>/delete/', views.user_delete, name='user_delete'),

    # Profile (any logged-in user)
    path('profile/', views.profile_view, name='profile_view'),

    # Audit log (admin only)
    path('audit/', views.audit_log_view, name='audit_log'),

    # Security settings (admin only)
    path('security/', views.security_settings_view, name='security_settings'),

    # Manager-Einstellungen: ALLOWED_HOSTS (admin only)
    path('manager-settings/', views.manager_settings_view, name='manager_settings'),
    path('manager-settings/env/', views.manager_env_view, name='manager_env'),

    # Projekt .env-Editor (admin only)
    path('project/<str:name>/env/', views.project_env_view, name='project_env'),

    # Firewall / ufw Port-Verwaltung (admin only)
    path('firewall/', views.firewall_view, name='firewall'),

    # Manager self-management
    path('manager/action/', views.manager_action, name='manager_action'),
    path('manager/update/', views.manager_update, name='manager_update'),
    path('manager/security-scan/', views.manager_security_scan, name='manager_security_scan'),
]
