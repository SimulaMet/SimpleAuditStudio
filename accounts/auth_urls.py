from django.urls import path

from . import views

urlpatterns = [
    path("register/", views.register, name="auth-register"),
    path("token/", views.obtain_token, name="auth-token"),
    path("me/", views.me, name="auth-me"),
    path("profile/", views.update_profile, name="auth-profile"),
]
