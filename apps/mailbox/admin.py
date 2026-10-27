from django.contrib import admin

from .models import MailboxAccount, MailboxRule, MailboxSeenMessage


class RuleInline(admin.TabularInline):
    model = MailboxRule
    extra = 0
    fields = ["position", "name", "target_feed", "enabled"]


@admin.register(MailboxAccount)
class MailboxAccountAdmin(admin.ModelAdmin):
    list_display = ["label", "host", "username", "is_active", "last_polled_at"]
    inlines = [RuleInline]


@admin.register(MailboxSeenMessage)
class MailboxSeenAdmin(admin.ModelAdmin):
    list_display = ["mailbox", "folder", "uid", "subject", "outcome", "processed_at"]
    list_filter = ["outcome", "mailbox"]


admin.site.register(MailboxRule)
