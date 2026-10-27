import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.collection.models import CollectedMessage, FetchRun
from apps.collection.pipeline import ingest_upload
from tests.helpers import make_pdf

pytestmark = pytest.mark.django_db


@pytest.fixture
def messages_data(feed):
    from apps.sources.models import Keyword, KeywordSet

    kset = KeywordSet.objects.create(name="hazards")
    Keyword.objects.create(keyword_set=kset, term="বন্যা", match_mode="prefix", language="bn")
    feed.keyword_sets.add(kset)
    ingest_upload(feed, text="সিলেটে বন্যার পানি বাড়ছে। নম্বর ১২", title="বন্যা সতর্কতা")
    ingest_upload(feed, text="Dhaka weather is sunny and calm today", title="Dhaka weather")
    m = CollectedMessage.objects.get(title="Dhaka weather")
    m.extra_fields = {"bulletin_number": "B-77"}
    m.save()
    return feed


def test_list_filters(operator_client, messages_data):
    url = reverse("ui:message_list")
    def titles(**params):
        return [m.title for m in operator_client.get(url, params).context["page"]]
    assert set(titles()) == {"বন্যা সতর্কতা", "Dhaka weather"}
    assert titles(q="বন্যার") == ["বন্যা সতর্কতা"]
    assert titles(q="12") == ["বন্যা সতর্কতা"]  # Bangla digits found by ASCII search
    assert titles(q="DHAKA sunny") == ["Dhaka weather"]
    assert titles(keyword="বন্যা") == ["বন্যা সতর্কতা"]
    assert titles(language="en") == ["Dhaka weather"]
    assert titles(field_name="bulletin_number", field_value="b-77") == ["Dhaka weather"]
    assert titles(status="filtered_out") == []
    assert titles(manual="no") == []


def test_csv_export_has_bom_and_bangla(operator_client, messages_data):
    response = operator_client.get(reverse("ui:message_export_csv"), {"q": "বন্যার"})
    content = b"".join(response.streaming_content).decode("utf-8")
    assert content.startswith("﻿") and "বন্যা সতর্কতা" in content and "Dhaka weather" not in content


def test_detail_mark_and_clear_attention(operator_client, messages_data):
    message = CollectedMessage.objects.get(title="Dhaka weather")
    assert operator_client.get(message.get_absolute_url()).status_code == 200
    operator_client.post(reverse("ui:message_mark_attention", args=[message.pk]), {"reason": "Check the date"})
    message.refresh_from_db()
    assert message.status == "needs_attention" and "Check the date" in message.attention_reason
    operator_client.post(reverse("ui:message_clear_attention", args=[message.pk]))
    message.refresh_from_db()
    assert message.status == "new"


def test_artifact_view_sandboxes_html(operator_client, feed):
    ingest_upload(feed, files=[("page.html", b"<html><body><script>alert(1)</script>Flood</body></html>", "text/html")])
    message = CollectedMessage.objects.get()
    response = operator_client.get(reverse("ui:artifact_download", args=[message.raw_artifact.pk]))
    assert response["Content-Security-Policy"].startswith("sandbox")
    assert response["X-Frame-Options"] == "SAMEORIGIN"
    assert operator_client.get(message.get_absolute_url()).status_code == 200


def test_upload_form(operator_client, feed):
    pdf = SimpleUploadedFile("bulletin.pdf", make_pdf(["Flood forecast for Sirajganj"]), content_type="application/pdf")
    response = operator_client.post(reverse("ui:upload"), {
        "feed": feed.pk, "files": [pdf], "title": "FFWC bulletin", "published_at": "2026-10-27T09:30",
    })
    run = FetchRun.objects.get()
    assert response.status_code == 302 and response["Location"] == run.get_absolute_url()
    message = CollectedMessage.objects.get()
    assert message.title == "FFWC bulletin" and message.created_by.username == "ewce_operator"
    assert message.published_at.isoformat() == "2026-10-27T03:30:00+00:00"  # entered in Dhaka time, stored UTC
    assert operator_client.get(run.get_absolute_url()).status_code == 200
    assert operator_client.get(reverse("ui:run_list")).status_code == 200


def test_upload_requires_something(operator_client, feed):
    response = operator_client.post(reverse("ui:upload"), {"feed": feed.pk})
    assert response.status_code == 200 and "Choose at least one file" in response.content.decode()
