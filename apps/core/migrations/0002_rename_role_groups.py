"""Rename the role groups to the WarnHub names, keeping their members and permissions."""
from django.db import migrations

RENAMES = {"EWCE Admin": "WarnHub Admin", "EWCE Operator": "WarnHub Operator"}


def rename(apps, mapping):
    Group = apps.get_model("auth", "Group")
    for old, new in mapping.items():
        if not Group.objects.filter(name=new).exists():
            Group.objects.filter(name=old).update(name=new)


def forwards(apps, schema_editor):
    rename(apps, RENAMES)


def backwards(apps, schema_editor):
    rename(apps, {new: old for old, new in RENAMES.items()})


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
