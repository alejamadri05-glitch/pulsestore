import os
import pathlib
import secrets
import uuid

import pytest

# Docker Desktop on macOS puts its socket under the user's home unless the "default Docker
# socket" option is enabled. CI runners use /var/run/docker.sock and never reach this branch.
_desktop_socket = pathlib.Path.home() / ".docker" / "run" / "docker.sock"
if "DOCKER_HOST" not in os.environ and _desktop_socket.exists():
    os.environ["DOCKER_HOST"] = f"unix://{_desktop_socket}"

from testcontainers.postgres import PostgresContainer  # noqa: E402

MIGRATIONS = sorted(pathlib.Path(__file__).parents[1].joinpath("migrations").glob("*.sql"))


@pytest.fixture(scope="session")
def database_url():
    """Admin connection: runs the migrations and owns the tables."""
    with PostgresContainer("postgres:16", driver=None) as pg:
        url = pg.get_connection_url()
        import psycopg

        with psycopg.connect(url, autocommit=True) as conn:
            for migration in MIGRATIONS:
                conn.execute(migration.read_text())
        yield url


@pytest.fixture(scope="session")
def app_database_url(database_url):
    """The least-privilege role from 003_app_role.sql, with a throwaway password.

    The whole API test suite connects as this role, so every test also checks that the
    service works without the privileges it was deliberately not given.
    """
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    password = secrets.token_urlsafe(24)
    with psycopg.connect(database_url, autocommit=True) as conn:
        # Utility statements cannot take bound parameters; sql.Literal quotes safely.
        conn.execute(sql.SQL("ALTER ROLE pulse_app PASSWORD {}").format(sql.Literal(password)))
    return make_conninfo(database_url, user="pulse_app", password=password)


@pytest.fixture(scope="session")
def client(app_database_url):
    os.environ["DATABASE_URL"] = app_database_url
    os.environ["API_KEY"] = "test-key"
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth():
    return {"x-api-key": "test-key"}


@pytest.fixture
def recording(client, auth):
    """A fresh recording per test, so tests never depend on each other's rows."""
    unique = uuid.uuid4().hex[:12]
    body = {
        "device_serial": f"SIM-{unique}",
        "device_model": "Holter-Sim",
        "subject_code": "TEST-001",
        "source_record": unique,
        "lead_name": "MLII",
        "sampling_rate_hz": 360,
        "started_at": "2026-01-01T00:00:00Z",
    }
    r = client.post("/recordings", json=body, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["id"]
