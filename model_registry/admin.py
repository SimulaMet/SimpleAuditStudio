from django.contrib import admin

from .models import AuditProfile, ModelConnection, ModelEndpoint, RegisteredModel


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


@admin.register(ModelEndpoint)
class ModelEndpointAdmin(admin.ModelAdmin):
    list_display = ("display_name", "provider", "model_id", "enabled", "project", "created_at")
    list_filter = ("provider", "enabled", "project")
    search_fields = ("display_name", "model_id")


@admin.register(AuditProfile)
class AuditProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "project", "created_at")
    list_filter = ("project",)
    search_fields = ("name",)
