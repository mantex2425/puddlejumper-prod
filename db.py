import os
import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor

def get_db():
    db_host = os.getenv("DB_HOST", "10.128.0.3")
    print(f"DEBUG: Connecting to DB_HOST: {db_host}")
    print(f"DEBUG: Using DB_USER: {os.getenv('DB_USER')}")
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME", "puddlejumper"),
        user=os.getenv("DB_USER", "atjb"),
        password=os.environ.get("DB_PASSWORD"),
        host=db_host,
        port=os.getenv("DB_PORT", "5432"),
        cursor_factory=RealDictCursor,
        connect_timeout=10,
        sslmode='disable'
    )

def get_db_connection():
    return get_db()

# Module-level readonly connection pool (2-5 connections)
_readonly_pool = None

def _get_readonly_pool():
    global _readonly_pool
    if _readonly_pool is None:
        db_host = os.getenv("DB_HOST", "10.128.0.3")
        _readonly_pool = psycopg2.pool.SimpleConnectionPool(
            minconn=2,
            maxconn=5,
            dbname=os.getenv("DB_NAME", "puddlejumper"),
            user="puddlejumper_readonly",
            password=os.environ.get("DB_PASSWORD_READONLY"),
            host=db_host,
            port=os.getenv("DB_PORT", "5432"),
            cursor_factory=RealDictCursor,
            connect_timeout=10,
            sslmode='disable'
        )
    return _readonly_pool

def get_db_readonly():
    """Borrow a read-only connection from the pool."""
    return _get_readonly_pool().getconn()

def return_db_readonly(conn):
    """Return a read-only connection back to the pool."""
    if conn:
        _get_readonly_pool().putconn(conn)
