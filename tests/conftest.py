import pytest
from django.contrib.auth.models import Group, User

from apps.core.roles import ADMIN_GROUP, OPERATOR_GROUP, ensure_groups


@pytest.fixture
def roles(db):
    return ensure_groups()


@pytest.fixture
def admin_user(roles):
    user = User.objects.create_user("ewce_admin", password="pw-admin-123")
    user.groups.add(Group.objects.get(name=ADMIN_GROUP))
    return user


@pytest.fixture
def operator_user(roles):
    user = User.objects.create_user("ewce_operator", password="pw-operator-123")
    user.groups.add(Group.objects.get(name=OPERATOR_GROUP))
    return user


@pytest.fixture
def admin_client(admin_user):
    from django.test import Client

    client = Client()
    client.force_login(admin_user)
    return client


@pytest.fixture
def operator_client(operator_user):
    from django.test import Client

    client = Client()
    client.force_login(operator_user)
    return client


@pytest.fixture
def seeded(db):
    from apps.sources.seed import seed_all

    return seed_all()


@pytest.fixture(autouse=True)
def artifact_root(settings, tmp_path):
    """Keep test artifacts out of the real data/ folder."""
    settings.EWCE_ARTIFACT_ROOT = tmp_path / "artifacts"
    return settings.EWCE_ARTIFACT_ROOT


@pytest.fixture
def feed(db):
    """A plain feed accepting every file type, capture_all, dedupe by URL."""
    from apps.sources.models import Feed, Source
    from apps.sources.settings_schema import FILE_TYPES

    source = Source.objects.create(name="Test Met Office", code="TMO", default_language="auto")
    return Feed.objects.create(
        source=source, name="Bulletins", slug="bulletins", method="test_static",
        settings={"files": {"allowed_types": FILE_TYPES}, "capture": {"min_text_length": 5}},
    )


@pytest.fixture
def static_docs():
    from tests.helpers import STATIC_DOCUMENTS

    STATIC_DOCUMENTS.clear()
    yield STATIC_DOCUMENTS
    STATIC_DOCUMENTS.clear()


@pytest.fixture(autouse=True, scope="session")
def _test_plugins():
    import tests.helpers  # noqa: F401 - registers the test-only plugins (after Django is set up)
