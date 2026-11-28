"""Imports its SQL: one relative hop, one absolute hop."""

import sqlite3

from .schema import SCHEMA_SQL


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def tables(conn: sqlite3.Connection):
    return conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()


def columns(conn):
    return conn.execute("SELECT column_name FROM information_schema.columns").fetchall()
