from django.urls import path

from .views import config as c
from .views import messages as m
from .views import system as s
from .views import config_io as io
from .views import mailboxes as mb
from .views import tools as t

app_name = "ui"
urlpatterns = [
    path("", s.dashboard, name="dashboard"),
    path("plugins/", s.plugin_catalogue, name="plugins"),
    # Messages, uploads, artifacts, runs
    path("messages/", m.message_list, name="message_list"),
    path("messages/export.csv", m.message_export_csv, name="message_export_csv"),
    path("messages/<int:pk>/", m.message_detail, name="message_detail"),
    path("messages/<int:pk>/attention/", m.message_mark_attention, name="message_mark_attention"),
    path("messages/<int:pk>/clear-attention/", m.message_clear_attention, name="message_clear_attention"),
    path("artifacts/<int:pk>/", m.artifact_view, name="artifact_download"),
    path("upload/", m.upload, name="upload"),
    # Tools
    path("tools/inspect/", t.inspect_url, name="inspect_url"),
    path("tools/playground/", t.playground, name="playground"),
    path("feeds/<int:feed_pk>/test-extraction/", t.test_extraction, name="test_extraction"),
    path("feeds/<int:pk>/test/", t.feed_test, name="feed_test"),
    path("feeds/<int:pk>/run/", t.feed_run_now, name="feed_run_now"),
    path("feeds/<int:pk>/queue/", io.feed_queue_run, name="feed_queue_run"),
    # Mailboxes
    path("mailboxes/", mb.mailbox_list, name="mailbox_list"),
    path("mailboxes/new/", mb.mailbox_edit, name="mailbox_create"),
    path("mailboxes/<int:pk>/", mb.mailbox_detail, name="mailbox_detail"),
    path("mailboxes/<int:pk>/edit/", mb.mailbox_edit, name="mailbox_edit"),
    path("mailboxes/<int:pk>/delete/", mb.mailbox_delete, name="mailbox_delete"),
    path("mailboxes/<int:pk>/clone/", mb.mailbox_clone, name="mailbox_clone"),
    path("mailboxes/<int:pk>/toggle/", mb.mailbox_toggle, name="mailbox_toggle"),
    path("mailboxes/<int:pk>/test/", mb.mailbox_test, name="mailbox_test"),
    path("mailboxes/<int:pk>/poll/", mb.mailbox_poll, name="mailbox_poll"),
    path("mailboxes/<int:pk>/preview/", mb.mailbox_preview, name="mailbox_preview"),
    path("mailboxes/<int:pk>/tester/", mb.mailbox_tester, name="mailbox_tester"),
    path("mailboxes/<int:mailbox_pk>/rules/new/", mb.mailbox_rule_edit, name="mailbox_rule_create"),
    path("mailboxes/<int:mailbox_pk>/rules/<int:pk>/", mb.mailbox_rule_edit, name="mailbox_rule_edit"),
    path("mailboxes/<int:mailbox_pk>/rules/<int:pk>/delete/", mb.mailbox_rule_delete, name="mailbox_rule_delete"),
    path("mailboxes/<int:mailbox_pk>/rules/<int:pk>/<str:action>/", mb.mailbox_rule_action, name="mailbox_rule_action"),
    # Import / export
    path("config/", io.config_io, name="config_io"),
    path("config/export.json", io.export_everything, name="export_all"),
    path("config/export/<str:kind>/<int:pk>.json", io.export_object, name="export_object"),
    path("runs/", m.run_list, name="run_list"),
    path("runs/<int:pk>/", m.run_detail, name="run_detail"),
    path("audit/", s.audit_log, name="audit"),
    path("tools/validate-pattern/", s.validate_pattern, name="validate_pattern"),
    path("versions/rollback/<int:version_pk>/", s.version_rollback, name="version_rollback"),
    path("versions/<str:model_label>/<int:pk>/", s.version_list, name="versions"),
    path("toggle/<str:model>/<int:pk>/", c.toggle_active, name="toggle_active"),
    # Sources
    path("sources/", c.source_list, name="source_list"),
    path("sources/new/", c.source_edit, name="source_create"),
    path("sources/<int:pk>/", c.source_detail, name="source_detail"),
    path("sources/<int:pk>/edit/", c.source_edit, name="source_edit"),
    path("sources/<int:pk>/delete/", c.source_delete, name="source_delete"),
    path("sources/<int:pk>/clone/", c.source_clone, name="source_clone"),
    # Feeds
    path("feeds/", c.feed_list, name="feed_list"),
    path("feeds/new/", c.feed_create, name="feed_create"),
    path("feeds/<int:pk>/", c.feed_detail, name="feed_detail"),
    path("feeds/<int:pk>/edit/", c.feed_edit, name="feed_edit"),
    path("feeds/<int:pk>/delete/", c.feed_delete, name="feed_delete"),
    path("feeds/<int:pk>/clone/", c.feed_clone, name="feed_clone"),
    path("feeds/<int:pk>/save-as-profile/", c.feed_save_as_profile, name="feed_save_as_profile"),
    path("feeds/<int:pk>/keyword-sets/", c.feed_keyword_sets, name="feed_keyword_sets"),
    path("feeds/<int:feed_pk>/rules/new/", c.rule_edit, name="rule_create"),
    path("feeds/<int:feed_pk>/rules/<int:pk>/", c.rule_edit, name="rule_edit"),
    path("feeds/<int:feed_pk>/rules/<int:pk>/delete/", c.rule_delete, name="rule_delete"),
    path("feeds/<int:feed_pk>/rules/<int:pk>/<str:action>/", c.rule_action, name="rule_action"),
    path("feeds/<int:feed_pk>/fields/new/", c.field_edit, name="field_create"),
    path("feeds/<int:feed_pk>/fields/<int:pk>/", c.field_edit, name="field_edit"),
    path("feeds/<int:feed_pk>/fields/<int:pk>/delete/", c.field_delete, name="field_delete"),
    # Profiles
    path("profiles/", c.profile_list, name="profile_list"),
    path("profiles/new/", c.profile_edit, name="profile_create"),
    path("profiles/<int:pk>/", c.profile_detail, name="profile_detail"),
    path("profiles/<int:pk>/edit/", c.profile_edit, name="profile_edit"),
    path("profiles/<int:pk>/delete/", c.profile_delete, name="profile_delete"),
    path("profiles/<int:pk>/clone/", c.profile_clone, name="profile_clone"),
    path("profiles/<int:pk>/create-feed/", c.profile_create_feed, name="profile_create_feed"),
    # Keyword sets
    path("keywords/", c.keywordset_list, name="keywordset_list"),
    path("keywords/new/", c.keywordset_edit, name="keywordset_create"),
    path("keywords/<int:pk>/", c.keywordset_detail, name="keywordset_detail"),
    path("keywords/<int:pk>/edit/", c.keywordset_edit, name="keywordset_edit"),
    path("keywords/<int:pk>/delete/", c.keywordset_delete, name="keywordset_delete"),
    path("keywords/<int:pk>/clone/", c.keywordset_clone, name="keywordset_clone"),
    path("keywords/<int:pk>/export.csv", c.keywordset_export_csv, name="keywordset_export_csv"),
    path("keywords/<int:pk>/import/", c.keywordset_import_csv, name="keywordset_import_csv"),
    path("keywords/<int:set_pk>/kw/new/", c.keyword_edit, name="keyword_create"),
    path("keywords/<int:set_pk>/kw/<int:pk>/", c.keyword_edit, name="keyword_edit"),
    path("keywords/<int:set_pk>/kw/<int:pk>/delete/", c.keyword_delete, name="keyword_delete"),
]
