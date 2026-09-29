"""Chat URLs, mounted at /chat/ by config/urls.py.

Both views 404 while the module is disabled, so they are safe to include
unconditionally.
"""
from django.urls import path

from chat.views import ChatView, authz, chat_with

urlpatterns = [
    path("", ChatView.as_view(), name="chat"),
    path("with/<str:model_id>", chat_with, name="chat_with"),
    path("authz", authz, name="chat_authz"),
]
