from django.urls import path, include
from django.shortcuts import redirect
from control import views as ctrl

urlpatterns = [
    path('', lambda req: redirect('dashboard'), name='root'),
    path('login/', ctrl.login_view, name='login'),
    path('logout/', ctrl.logout_view, name='logout'),
    path('', include('control.urls')),
]
