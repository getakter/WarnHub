"""Mailbox routing rules and an end-to-end poll against an in-memory mailbox."""
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.collection.models import CollectedMessage, FetchRun, RawArtifact
from apps.collection.worker import Worker
from apps.mailbox import service
from apps.mailbox.models import MailboxAccount, MailboxRule, MailboxSeenMessage
from apps.sources.models import Feed, Source
from tests.helpers import FakeServer, make_eml, make_pdf

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _rule(**kw):
    defaults = dict(name="r", enabled=True, match_mode="all", from_patterns=[], to_patterns=[], subject_contains=[],
                    subject_regex="", body_keywords=[], has_attachment="any", attachment_types=[], stop_processing=True)
    defaults.update(kw)
    return MailboxRule(**defaults)


# --- routing (unit) ------------------------------------------------------------------
def test_address_patterns():
    assert service.address_matches("@ffwc.gov.bd", "desk@ffwc.gov.bd")
    assert service.address_matches("ffwc.gov.bd", "desk@mail.ffwc.gov.bd")
    assert service.address_matches("Desk@FFWC.gov.bd", "desk@ffwc.gov.bd")
    assert not service.address_matches("@ffwc.gov.bd", "desk@notffwc.gov.bd")
    assert not service.address_matches("desk@ffwc.gov.bd", "other@ffwc.gov.bd")


def test_parse_email_bangla_and_attachments():
    raw = make_eml("বন্যা পূর্বাভাস", "আজ নদীর পানি বাড়বে।", sender="FFWC Desk <desk@ffwc.gov.bd>",
                   attachments=[("bulletin.pdf", make_pdf(["Flood bulletin"]), "application", "pdf")],
                   inline=[("logo.png", PNG, "image", "png")])
    info = service.parse_email(raw)
    assert info.subject == "বন্যা পূর্বাভাস" and info.from_address == "desk@ffwc.gov.bd"
    assert info.body_text == "আজ নদীর পানি বাড়বে।" and info.message_id == "test-1@ffwc.gov.bd"
    assert [(a.filename, a.file_type, a.inline) for a in info.attachments] == [("bulletin.pdf", "pdf", False),
                                                                              ("logo.png", "image", True)]
    assert info.date.isoformat() == "2026-10-27T09:00:00+06:00"


def test_evaluate_rule_conditions_and_modes():
    info = service.parse_email(make_eml("Daily flood forecast", "Water level rising at Sirajganj",
                                        attachments=[("b.pdf", b"%PDF-1.4 x", "application", "pdf")]))
    matched, checks = service.evaluate_rule(_rule(from_patterns=["@ffwc.gov.bd"], subject_contains=["forecast"],
                                                  has_attachment="yes", attachment_types=["pdf"]), info)
    assert matched and [c[0] for c in checks] == ["from", "subject contains", "has attachment", "attachment types"]
    assert not service.evaluate_rule(_rule(from_patterns=["@bmd.gov.bd"], subject_contains=["forecast"]), info)[0]
    assert service.evaluate_rule(_rule(match_mode="any", from_patterns=["@bmd.gov.bd"], subject_contains=["forecast"]), info)[0]
    assert service.evaluate_rule(_rule(subject_regex=r"flood\s+forecast", body_keywords=["sirajganj"]), info)[0]
    assert not service.evaluate_rule(_rule(has_attachment="no"), info)[0]
    assert service.evaluate_rule(_rule(), info) == (True, [("(no conditions)", True, "matches every email")])


def test_route_order_and_stop_processing():
    info = service.parse_email(make_eml("forecast", "x"))
    first = _rule(name="first", subject_contains=["forecast"], stop_processing=False)
    second = _rule(name="second")
    disabled = _rule(name="off", enabled=False)
    assert [r.name for r, _ in service.route([disabled, first, second], info)] == ["first", "second"]
    first.stop_processing = True
    assert [r.name for r, _ in service.route([first, second], info)] == ["first"]


def test_select_attachments():
    info = service.parse_email(make_eml("x", "y", attachments=[("a.pdf", b"%PDF-1.4 " + b"x" * 2048, "application", "pdf"),
                                                                ("a.zip", b"PK\x03\x04" + b"\x00" * 30, "application", "zip")],
                                        inline=[("logo.png", PNG, "image", "png")]))
    rule = _rule(allowed_attachment_types=["pdf", "image"], ignore_inline_images=True, max_attachment_mb=25)
    chosen, skipped = service.select_attachments(rule, info, {"files": {"allowed_types": []}})
    assert [a.filename for a in chosen] == ["a.pdf"]
    assert dict((a.filename, r) for a, r in skipped) == {"a.zip": "type 'zip' not allowed", "logo.png": "inline image / signature logo"}
    rule.max_attachment_mb = 0.001
    assert service.select_attachments(rule, info, {"files": {"allowed_types": []}})[0] == []


# --- end to end ----------------------------------------------------------------------
@pytest.fixture
def mail_setup(db, monkeypatch):
    monkeypatch.setenv("EWCE_TEST_MAILBOX_PASSWORD", "secret")
    source = Source.objects.create(name="Flood Centre", code="FFWC")
    flood = Feed.objects.create(source=source, name="Flood bulletins", slug="flood", method="email",
                                settings={"files": {"allowed_types": ["pdf", "html", "txt"]}, "capture": {"min_text_length": 5}})
    forecasts = Feed.objects.create(source=source, name="Forecast emails", slug="forecast", method="email",
                                    settings={"capture": {"min_text_length": 5}})
    mailbox = MailboxAccount.objects.create(label="Alerts", protocol="test_fake", host="mail.test", port=993,
                                            username="alerts@example.org", password_env_var="EWCE_TEST_MAILBOX_PASSWORD",
                                            folders=["INBOX"], is_active=True, default_post_action="mark_read",
                                            unmatched_post_action="none")
    MailboxRule.objects.create(mailbox=mailbox, name="FFWC PDFs", position=10, from_patterns=["@ffwc.gov.bd"],
                               has_attachment="yes", attachment_types=["pdf"], target_feed=flood, take="attachments",
                               allowed_attachment_types=["pdf"], split="per_attachment", post_action="move",
                               post_target="Processed")
    MailboxRule.objects.create(mailbox=mailbox, name="Forecast subjects", position=20,
                               subject_contains=["পূর্বাভাস", "forecast"], target_feed=forecasts, take="both",
                               split="per_email")
    MailboxRule.objects.create(mailbox=mailbox, name="FFWC no attachments", position=30, from_patterns=["@ffwc.gov.bd"],
                               target_feed=flood, take="attachments")
    FakeServer.reset(INBOX={
        "1": make_eml("Flood bulletin 27 Oct", "See attached.", message_id="<m1@ffwc.gov.bd>",
                      attachments=[("flood-27.pdf", make_pdf(["Jamuna above danger level at Sirajganj"]), "application", "pdf"),
                                   ("notes.zip", b"PK\x03\x04" + b"\x00" * 30, "application", "zip")]),
        "2": make_eml("আবহাওয়ার পূর্বাভাস", "ঢাকায় হালকা বৃষ্টি হতে পারে আজ।", sender="desk@bmd.gov.bd",
                      message_id="<m2@bmd.gov.bd>", attachments=[("detail.txt", "বিস্তারিত পূর্বাভাস তথ্য".encode(), "text", "plain")]),
        "3": make_eml("Lunch on Friday", "Office lunch at 1 pm on Friday.", sender="hr@example.org", message_id="<m3@example.org>"),
        "4": make_eml("FFWC note", "Only a logo here, sorry.", message_id="<m4@ffwc.gov.bd>", inline=[("logo.png", PNG, "image", "png")]),
    })
    return mailbox, flood, forecasts


@pytest.mark.django_db
def test_end_to_end_poll(mail_setup):
    mailbox, flood, forecasts = mail_setup
    run = service.poll_mailbox(mailbox)
    assert run.status == "success", list(run.events.values_list("message", flat=True))
    assert run.stats["emails"] == 4 and run.stats["emails_routed"] == 3 and run.stats["emails_unrouted"] == 1
    assert run.stats["unrouted"] == 1  # message status count

    pdf = CollectedMessage.objects.get(feed=flood, raw_artifact__file_type="pdf")
    assert "Jamuna above danger" in pdf.body_text and pdf.mailbox == mailbox
    assert pdf.raw_artifact.parent.file_type == "eml" and pdf.raw_artifact.origin == "attachment"
    assert pdf.source_item_key == "email:m1@ffwc.gov.bd#flood-27.pdf"
    assert not CollectedMessage.objects.filter(raw_artifact__original_filename="notes.zip").exists()

    forecast = CollectedMessage.objects.get(feed=forecasts)
    assert forecast.title == "আবহাওয়ার পূর্বাভাস" and "ঢাকায় হালকা বৃষ্টি" in forecast.body_text
    assert "[Attachment: detail.txt]" in forecast.body_text and "বিস্তারিত পূর্বাভাস" in forecast.body_text
    assert forecast.published_at.isoformat() == "2026-10-27T03:00:00+00:00"

    unrouted = CollectedMessage.objects.get(status="unrouted")
    assert unrouted.feed is None and unrouted.mailbox == mailbox and unrouted.title == "Lunch on Friday"
    assert unrouted.raw_artifact.file_type == "eml"

    logo_only = CollectedMessage.objects.get(feed=flood, title="FFWC note")
    assert logo_only.status == "needs_attention" and "none were usable" in logo_only.attention_reason

    assert RawArtifact.objects.filter(file_type="eml", origin="email").count() == 4  # every original kept
    assert MailboxSeenMessage.objects.filter(mailbox=mailbox).count() == 4
    assert ("move", "INBOX", "1", "Processed") in FakeServer.actions
    assert ("mark_read", "INBOX", "2") in FakeServer.actions
    assert not [a for a in FakeServer.actions if a[2] == "3"]  # unmatched: left untouched

    again = service.poll_mailbox(mailbox)
    assert again.stats == {} and CollectedMessage.objects.count() == 4  # no reprocessing

    FakeServer.deliver("Archive", 9, FakeServer.folders["INBOX"]["2"])  # same Message-ID in another folder
    mailbox.folders = ["INBOX", "Archive"]
    mailbox.save()
    third = service.poll_mailbox(mailbox)
    assert third.stats.get("already_seen") == 1 and CollectedMessage.objects.count() == 4


@pytest.mark.django_db
def test_missing_password_env_fails_visibly(mail_setup, monkeypatch):
    mailbox, *_ = mail_setup
    monkeypatch.delenv("EWCE_TEST_MAILBOX_PASSWORD")
    run = service.poll_mailbox(mailbox)
    mailbox.refresh_from_db()
    assert run.status == "failed" and "EWCE_TEST_MAILBOX_PASSWORD is not set" in mailbox.last_error
    ok, message, _ = service.test_connection(mailbox)
    assert not ok and "is not set" in message


@pytest.mark.django_db
def test_worker_polls_due_mailboxes(mail_setup):
    mailbox, *_ = mail_setup
    Worker(inline=True).poll_mailboxes()
    mailbox.refresh_from_db()
    assert mailbox.last_success_at and mailbox.next_poll_at > mailbox.last_polled_at
    assert FetchRun.objects.filter(mailbox=mailbox, trigger="mailbox").count() == 1
    Worker(inline=True).poll_mailboxes()  # not due yet
    assert FetchRun.objects.filter(mailbox=mailbox).count() == 1


@pytest.mark.django_db
def test_mailbox_ui(mail_setup, admin_client, operator_client):
    mailbox, flood, _ = mail_setup
    for url in [reverse("ui:mailbox_list"), mailbox.get_absolute_url(), reverse("ui:mailbox_edit", args=[mailbox.pk]),
                reverse("ui:mailbox_rule_create", args=[mailbox.pk]), reverse("ui:mailbox_preview", args=[mailbox.pk]),
                reverse("ui:mailbox_tester", args=[mailbox.pk])]:
        assert admin_client.get(url).status_code == 200, url
    preview = admin_client.get(reverse("ui:mailbox_preview", args=[mailbox.pk])).context["rows"]
    assert {r["uid"]: [m[0].name for m in r["matches"]] for r in preview}["3"] == []
    response = admin_client.post(reverse("ui:mailbox_test", args=[mailbox.pk]), follow=True)
    assert "Logged in to mail.test" in response.content.decode()

    eml = SimpleUploadedFile("x.eml", FakeServer.folders["INBOX"]["1"], content_type="message/rfc822")
    tested = admin_client.post(reverse("ui:mailbox_tester", args=[mailbox.pk]), {"eml": eml, "dry_run": "on"})
    verdicts = {v["rule"].name: v["matched"] for v in tested.context["verdicts"]}
    assert verdicts == {"FFWC PDFs": True, "Forecast subjects": False, "FFWC no attachments": True}
    assert [p["rule"].name for p in tested.context["plans"]] == ["FFWC PDFs"]
    assert [r["status"] for r in tested.context["results"]] == ["new"] and not CollectedMessage.objects.exists()

    assert operator_client.get(reverse("ui:mailbox_edit", args=[mailbox.pk])).status_code == 403
    run_page = operator_client.post(reverse("ui:mailbox_poll", args=[mailbox.pk]), follow=True)
    assert run_page.status_code == 200 and CollectedMessage.objects.filter(status="unrouted").count() == 1
    unrouted = operator_client.get(reverse("ui:message_list"), {"status": "unrouted"}).context["page"]
    assert [m.title for m in unrouted] == ["Lunch on Friday"]

    response = admin_client.post(reverse("ui:mailbox_rule_create", args=[mailbox.pk]), {
        "name": "Bad regex", "enabled": "on", "match_mode": "all", "subject_regex": "(unclosed", "has_attachment": "any",
        "target_feed": flood.pk, "take": "both", "max_attachment_mb": "25", "split": "per_email",
        "post_action": "default", "stop_processing": "on"})
    assert "Subject regex" in response.content.decode()


def test_imap_connection_uses_peek_and_maps_actions(monkeypatch):
    """The real IMAP adapter, against a stand-in for imap_tools.MailBox."""
    import imap_tools

    from apps.mailbox.protocols import ImapConnection

    calls = []

    class FakeClient:
        capabilities = ("IMAP4REV1",)

        def uid(self, *args):
            calls.append(("uid",) + args)
            return "OK", [(b"1 (UID 7 BODY[] {5}", b"RAW-7"), b")"]

    class FakeFolder:
        def set(self, name):
            calls.append(("select", name))

        def status(self, folder, items):
            return {"UIDVALIDITY": 42, "MESSAGES": 3, "UNSEEN": 1}

        def list(self):
            return [type("F", (), {"name": "INBOX"})()]

    class FakeMailBox:
        def __init__(self, host, port, timeout=None):
            calls.append(("connect", host, port))
            self.client, self.folder = FakeClient(), FakeFolder()

        def login(self, user, password, initial_folder=None):
            calls.append(("login", user, password))

        def uids(self, criteria):
            calls.append(("search", str(criteria)))
            return ["10", "7"]

        def flag(self, uid, flag, value):
            calls.append(("flag", uid, flag, value))

        def move(self, uid, target):
            calls.append(("move", uid, target))

        def logout(self):
            calls.append(("logout",))

    monkeypatch.setattr(imap_tools, "MailBox", FakeMailBox)
    monkeypatch.setenv("EWCE_IMAP_PW", "pw")
    account = MailboxAccount(host="imap.example.org", port=993, security="ssl", username="u", password_env_var="EWCE_IMAP_PW")
    conn = ImapConnection(account)
    assert conn.uidvalidity("INBOX") == "42" and conn.folders() == ["INBOX"]
    assert conn.search_uids("INBOX", unseen_only=True) == ["7", "10"]  # oldest first
    assert conn.fetch_raw("INBOX", ["7"]) == {"7": b"RAW-7"}
    assert ("uid", "FETCH", "7", "(BODY.PEEK[])") in calls  # never marks as read while downloading
    conn.mark_read("INBOX", "7")
    conn.move("INBOX", "7", "Processed")
    conn.label("INBOX", "7", "WarnHub")
    conn.close()
    assert ("flag", "7", "\Seen", True) in calls and ("move", "7", "Processed") in calls and ("flag", "7", "WarnHub", True) in calls
    assert calls[0] == ("connect", "imap.example.org", 993) and ("login", "u", "pw") in calls and calls[-1] == ("logout",)
