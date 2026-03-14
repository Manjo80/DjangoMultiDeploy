from django.urls import path
from . import views

urlpatterns = [
    # Dashboard
    path('dashboard/', views.dashboard, name='dashboard'),

    # Install wizard
    path('install/', views.install_form, name='install_form'),
    path('install/run/', views.install_run, name='install_run'),
    path('install/progress/<str:project>/<str:run_id>/', views.install_progress, name='install_progress'),
    path('install/stream/<str:log_name>/', views.install_stream, name='install_stream'),
    path('install/ssh-key/<str:project>/', views.ssh_key_display, name='ssh_key_display'),
    path('install/ssh-key/<str:project>/confirm/', views.ssh_key_confirm, name='ssh_key_confirm'),
    path('install/ssh-key/<str:project>/download/', views.ssh_key_download, name='ssh_key_download'),

    # Global GitHub deploy key
    path('deploy-key/', views.global_deploy_key, name='global_deploy_key'),
    path('deploy-key/download/', views.global_deploy_key_download, name='global_deploy_key_download'),

    # Project management
    path('project/<str:name>/', views.project_detail, name='project_detail'),
    path('project/<str:name>/action/', views.project_action, name='project_action'),
    path('project/<str:name>/logs/', views.log_viewer, name='log_viewer'),
    path('project/<str:name>/remove/', views.remove_confirm, name='remove_confirm'),
    path('project/<str:name>/remove/run/', views.remove_run, name='remove_run'),
]
