from django.contrib import admin

from .models import CollectedMessage, FetchRun, FetchRunEvent, RawArtifact


@admin.register(CollectedMessage)
class CollectedMessageAdmin(admin.ModelAdmin):
    list_display = ["title", "feed", "status", "collected_at", "version"]
    list_filter = ["status", "source", "detected_language"]
    search_fields = ["title", "search_text"]
    raw_id_fields = ["raw_artifact", "fetch_run", "supersedes", "duplicate_of", "extracting_rule"]


class EventInline(admin.TabularInline):
    model = FetchRunEvent
    extra = 0
    readonly_fields = ["created_at", "level", "message", "context"]


@admin.register(FetchRun)
class FetchRunAdmin(admin.ModelAdmin):
    list_display = ["id", "feed", "mailbox", "trigger", "status", "started_at"]
    list_filter = ["status", "trigger"]
    inlines = [EventInline]


@admin.register(RawArtifact)
class RawArtifactAdmin(admin.ModelAdmin):
    list_display = ["sha256", "file_type", "size", "source_url", "fetched_at"]
    search_fields = ["sha256", "source_url", "original_filename"]
