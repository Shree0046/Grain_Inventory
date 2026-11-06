"""MySQL connection pooling and small query helpers.

All connections run in autocommit mode; multi-statement work (e.g. issuing a
bill) uses ``transaction()`` which starts an explicit transaction, commits on
success and rolls back on any error.
"""
import logging
import os
import threading
from contextlib import contextmanager

import mysql.connector
from dotenv import load_dotenv
from mysql.connector import pooling

load_dotenv()
log = logging.getLogger("grain.db")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "root"),
    "database": os.getenv("DB_NAME", "grain_inventory"),
    "charset": "utf8mb4",
    "autocommit": True,
    "connection_timeout": 10,
}
POOL_SIZE = max(2, min(int(os.getenv("DB_POOL_SIZE", "10")), 32))


class DatabaseUnavailable(Exception):
    """Raised when the database cannot be reached."""


_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                try:
                    _pool = pooling.MySQLConnectionPool(
                        pool_name="grain_pool",
                        pool_size=POOL_SIZE,
                        pool_reset_session=True,
                        **DB_CONFIG,
                    )
                    log.info("Database pool created (%s@%s/%s)", DB_CONFIG["user"], DB_CONFIG["host"], DB_CONFIG["database"])
                except mysql.connector.Error as exc:
                    log.error("Cannot connect to database: %s", exc)
                    raise DatabaseUnavailable(str(exc)) from exc
    return _pool


@contextmanager
def connection():
    try:
        conn = _get_pool().get_connection()
    except mysql.connector.Error as exc:
        log.error("Could not get a pooled connection: %s", exc)
        raise DatabaseUnavailable(str(exc)) from exc
    try:
        yield conn
    finally:
        conn.close()  # returns the connection to the pool


def fetch_all(sql, params=None):
    with connection() as conn:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(sql, params or ())
            return cur.fetchall()
        finally:
            cur.close()


def fetch_one(sql, params=None):
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None):
    """Run a single write statement. Returns (last_insert_id, rows_affected)."""
    with connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(sql, params or ())
            return cur.lastrowid, cur.rowcount
        finally:
            cur.close()


@contextmanager
def transaction():
    """Yield a dict cursor inside a transaction (commit on success, rollback on error)."""
    with connection() as conn:
        conn.start_transaction()
        cur = conn.cursor(dictionary=True)
        try:
            yield cur
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except mysql.connector.Error:
                log.exception("Rollback failed")
            raise
        finally:
            cur.close()
