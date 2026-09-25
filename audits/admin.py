from django.contrib import admin

from .events import AuditEvent, ScenarioResult
from .models import AuditRun


@admin.register(AuditRun)
class AuditRunAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "project", "created_at")
    list_filter = ("status", "project")
    search_fields = ("name",)
    readonly_fields = ("generation_parameters_snapshot",)


admin.site.register(AuditEvent)
admin.site.register(ScenarioResult)
