from django.contrib import admin

from .models import (
    Scenario,
    ScenarioRevision,
    ScenarioSet,
    ScenarioSetVersion,
    ScenarioSetVersionItem,
)


@admin.register(Scenario)
class ScenarioAdmin(admin.ModelAdmin):
    list_display = ("key", "title", "category", "project", "created_at")
    list_filter = ("category", "project")
    search_fields = ("key", "title")


@admin.register(ScenarioSet)
class ScenarioSetAdmin(admin.ModelAdmin):
    list_display = ("name", "description", "project", "created_at")
    list_filter = ("project",)
    search_fields = ("name",)


@admin.register(ScenarioSetVersion)
class ScenarioSetVersionAdmin(admin.ModelAdmin):
    list_display = ("version", "scenario_set", "published_by", "published_at")
    list_filter = ("scenario_set__project",)


admin.site.register(ScenarioRevision)
admin.site.register(ScenarioSetVersionItem)
