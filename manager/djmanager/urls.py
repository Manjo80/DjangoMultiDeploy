from django.urls import path, include
from django.shortcuts import redirect
from django.http import HttpResponse
from control import views as ctrl


def _robots_txt(request):
    return HttpResponse(
        "User-agent: *\nDisallow: /\n",
        content_type="text/plain",
    )


urlpatterns = [
    path('', lambda req: redirect('dashboard'), name='root'),
    path('robots.txt', _robots_txt, name='robots_txt'),
    path('login/', ctrl.login_view, name='login'),
    path('logout/', ctrl.logout_view, name='logout'),
    path('', include('control.urls')),
]
