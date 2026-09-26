from django.urls import path

from . import views

urlpatterns = [
    path("models/ping-connection/<int:conn_pk>/", views.ping_connection, name="conn-ping"),
]
