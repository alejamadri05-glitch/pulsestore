import os

from psycopg_pool import ConnectionPool

pool = ConnectionPool(
    conninfo=os.environ["DATABASE_URL"],
    min_size=1,
    max_size=int(os.getenv("DB_POOL_MAX", "10")),
    # Check a connection before handing it out: cloud databases and proxies drop
    # idle connections, and a dead one would otherwise surface as a 500.
    check=ConnectionPool.check_connection,
    open=False,
)
