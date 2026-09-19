"""Connection pool, with password or Microsoft Entra authentication.

DB_AUTH=password (default): DATABASE_URL carries the password, as in local development and CI.
DB_AUTH=entra: DATABASE_URL has no password. Each new connection gets a fresh Entra token as
its password. In Azure the token comes from the app's managed identity; on a laptop, from the
developer's `az login`. Password authentication is disabled on the Azure server, so no password
exists anywhere (docs/azure.md).
"""

import os

import psycopg
from psycopg_pool import ConnectionPool

# Microsoft Entra resource for Azure Database for PostgreSQL.
ENTRA_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


def entra_connection_class(credential) -> type[psycopg.Connection]:
    """A connection class that authenticates with a token from `credential`.

    PostgreSQL checks the password only when a connection opens, so an open connection stays
    valid after its token expires: the token is needed at connect time only. azure-identity
    caches tokens, so asking for one per new connection costs a network call about once an hour.
    """

    class EntraConnection(psycopg.Connection):
        @classmethod
        def connect(cls, conninfo: str = "", **kwargs):
            kwargs["password"] = credential.get_token(ENTRA_SCOPE).token
            return super().connect(conninfo, **kwargs)

    return EntraConnection


def make_pool(conninfo: str | None = None, auth: str | None = None, credential=None):
    conninfo = conninfo if conninfo is not None else os.environ["DATABASE_URL"]
    auth = (auth or os.getenv("DB_AUTH", "password")).lower()
    options = {
        "conninfo": conninfo,
        "min_size": 1,
        "max_size": int(os.getenv("DB_POOL_MAX", "10")),
        # Check a connection before handing it out: cloud databases and proxies drop idle
        # connections, and a dead one would otherwise surface as a 500.
        "check": ConnectionPool.check_connection,
        "open": False,
    }
    if auth == "entra":
        if credential is None:
            # Imported here so password mode (local, CI) never needs Azure libraries at import.
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
        options["connection_class"] = entra_connection_class(credential)
    elif auth != "password":
        raise ValueError(f"DB_AUTH must be 'password' or 'entra', not {auth!r}")
    return ConnectionPool(**options)


pool = make_pool()
