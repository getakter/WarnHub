"""Environment checks shown on the dashboard and by `manage.py bootstrap`."""
from django.conf import settings
from django.db import connection


def database_warnings():
    warnings = []
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT @@GLOBAL.max_allowed_packet, @@character_set_database, @@collation_database")
            packet, charset, collation = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        return [f"Could not query database settings: {exc}"]
    if int(packet) < settings.EWCE_MIN_PACKET_BYTES:
        warnings.append(
            f"MariaDB max_allowed_packet is {int(packet) // 1024} KB. Long bulletins may fail to save. "
            r"Set max_allowed_packet=64M under [mysqld] in C:\xampp\mysql\bin\my.ini and restart MySQL."
        )
    if charset != "utf8mb4":
        warnings.append(f"Database charset is {charset}/{collation}; expected utf8mb4 (Bangla may break).")
    return warnings
