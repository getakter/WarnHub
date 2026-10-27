from django.contrib import admin

from .models import AuditLog, ConfigVersion


@admin.register(ConfigVersion)
class ConfigVersionAdmin(admin.ModelAdmin):
    list_display = ["object_repr", "content_type", "version", "change_type", "created_at", "created_by"]
    list_filter = ["content_type", "change_type"]


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "user", "action", "object_repr"]
    list_filter = ["action"]
