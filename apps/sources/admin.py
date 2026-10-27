"""Django admin is a superuser fallback; day-to-day configuration uses the main UI."""
from django.contrib import admin

from .models import (
    CustomFieldDefinition, ExtractionRule, Feed, FeedKeywordSet, FormatProfile, Keyword, KeywordSet, Source,
)


class RuleInline(admin.TabularInline):
    model = ExtractionRule
    extra = 0
    fields = ["position", "name", "rule_type", "enabled"]


class KeywordInline(admin.TabularInline):
    model = Keyword
    extra = 0


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "base_url", "is_active", "is_example"]
    search_fields = ["name", "code"]


@admin.register(Feed)
class FeedAdmin(admin.ModelAdmin):
    list_display = ["name", "source", "method", "is_active", "health_status", "last_success_at"]
    list_filter = ["source", "method", "is_active", "health_status"]
    inlines = [RuleInline]


admin.site.register(FormatProfile)
admin.site.register(ExtractionRule)
admin.site.register(CustomFieldDefinition)
admin.site.register(FeedKeywordSet)


@admin.register(KeywordSet)
class KeywordSetAdmin(admin.ModelAdmin):
    inlines = [KeywordInline]
