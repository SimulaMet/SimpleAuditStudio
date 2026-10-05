"""Chat URLs, mounted at /ai/ by config/urls.py (the wrapper page).

Note: /chat/authz is mounted separately in config/urls.py at the path the
forward-auth contract uses (Caddy asks Studio there). All these views 404
while the module is disabled, so they are safe to include unconditionally.
"""
from django.urls import path

from chat.views import ChatView, chat_with

urlpatterns = [
    path("", ChatView.as_view(), name="chat"),
    path("with/<int:connection_id>/<str:model_id>", chat_with, name="chat_with"),
]
