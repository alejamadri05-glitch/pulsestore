"""Phase 7: the app role can do its job and nothing else."""

import psycopg
import pytest


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM annotations",
        "TRUNCATE annotations",
        "UPDATE devices SET serial_number = 'HACKED'",
        "UPDATE recordings SET subject_code = 'OTHER-01'",
        "DROP TABLE annotations",
        "CREATE TABLE sneaky (id int)",
        "ALTER TABLE annotations ADD COLUMN note text",
    ],
)
def test_app_role_cannot_delete_rewrite_or_change_the_schema(app_database_url, statement):
    with psycopg.connect(app_database_url) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)


def test_app_role_can_upsert_a_device_model(client, auth):
    """The one UPDATE the role has: POST /recordings refreshes a known device's model."""
    body = {
        "device_serial": "SIM-UPSERT",
        "device_model": "Holter-Sim",
        "subject_code": "TEST-003",
        "source_record": "upsert-a",
        "lead_name": "MLII",
        "sampling_rate_hz": 360,
        "started_at": "2026-01-01T00:00:00Z",
    }
    assert client.post("/recordings", json=body, headers=auth).status_code == 201
    body |= {"device_model": "Holter-Sim-2", "source_record": "upsert-b"}
    assert client.post("/recordings", json=body, headers=auth).status_code == 201


def test_app_role_is_not_a_superuser_and_owns_nothing(app_database_url):
    with psycopg.connect(app_database_url) as conn:
        superuser, owned = conn.execute(
            """SELECT rolsuper,
                      (SELECT count(*) FROM pg_class WHERE relowner = r.oid)
               FROM pg_roles r WHERE rolname = current_user"""
        ).fetchone()
    assert superuser is False
    assert owned == 0
