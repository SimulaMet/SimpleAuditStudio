"""Chat URLs, mounted at /chat/ by config/urls.py.

Both views 404 while the module is disabled, so they are safe to include
unconditionally.
"""
from django.urls import path

from chat.views import ChatView, authz

urlpatterns = [
    path("", ChatView.as_view(), name="chat"),
    path("authz", authz, name="chat_authz"),
]
