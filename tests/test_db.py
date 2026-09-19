"""Entra authentication, tested without Azure.

A fake credential hands out the test role's real password as its "token". If the connection
succeeds, the token was used as the password, at connect time, which is the whole mechanism.
"""

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo


class FakeCredential:
    def __init__(self, token: str):
        self.token_value = token
        self.calls = 0

    def get_token(self, scope: str):
        from app.db import ENTRA_SCOPE

        assert scope == ENTRA_SCOPE
        self.calls += 1
        return type("AccessToken", (), {"token": self.token_value})()


@pytest.fixture
def without_password(app_database_url):
    """The app role's conninfo with the password removed, as in Azure, plus that password."""
    params = conninfo_to_dict(app_database_url)
    password = params.pop("password")
    return make_conninfo(**params), password


def test_entra_mode_uses_a_fresh_token_as_the_password(client, without_password):
    from app.db import entra_connection_class

    conninfo, password = without_password
    credential = FakeCredential(password)
    connection_class = entra_connection_class(credential)
    for _ in range(2):
        with connection_class.connect(conninfo) as conn:
            assert conn.execute("SELECT current_user").fetchone()[0] == "pulse_app"
    assert credential.calls == 2  # one token per new connection


def test_without_the_token_the_same_conninfo_is_refused(client, without_password):
    conninfo, _ = without_password
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(conninfo, connect_timeout=5)


def test_entra_pool_serves_connections(client, without_password):
    from app.db import make_pool

    conninfo, password = without_password
    credential = FakeCredential(password)
    pool = make_pool(conninfo, auth="entra", credential=credential)
    pool.open(wait=True, timeout=10)
    try:
        with pool.connection() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        pool.close()
    assert credential.calls >= 1


def test_password_is_the_default_mode(client, monkeypatch, app_database_url):
    from app.db import make_pool

    monkeypatch.delenv("DB_AUTH", raising=False)
    assert make_pool(app_database_url).connection_class is psycopg.Connection


def test_unknown_auth_mode_fails_at_startup(client, app_database_url):
    from app.db import make_pool

    with pytest.raises(ValueError, match="DB_AUTH"):
        make_pool(app_database_url, auth="kerberos")
