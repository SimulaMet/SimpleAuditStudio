from django.contrib import admin

from .models import ModelConnection, RegisteredModel


@admin.register(ModelConnection)
class ModelConnectionAdmin(admin.ModelAdmin):
    list_display = ("name", "provider", "base_url", "enabled", "project", "created_at")
    list_filter = ("provider", "enabled", "project")
    search_fields = ("name", "base_url")


@admin.register(RegisteredModel)
class RegisteredModelAdmin(admin.ModelAdmin):
    list_display = ("display_name", "model_id", "connection", "enabled", "created_at")
    list_filter = ("enabled", "connection__project")
    search_fields = ("display_name", "model_id")

