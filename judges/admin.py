from django.contrib import admin

from .models import Judge, JudgeVersion


class JudgeVersionInline(admin.TabularInline):
    model = JudgeVersion
    extra = 0
    fields = ("version", "base", "output", "note", "created_at")
    readonly_fields = fields
    can_delete = False


@admin.register(Judge)
class JudgeAdmin(admin.ModelAdmin):
    list_display = ("name", "project", "updated_at")
    list_filter = ("project",)
    search_fields = ("name",)
    inlines = [JudgeVersionInline]
