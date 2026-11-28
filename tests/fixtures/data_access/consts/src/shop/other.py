"""An absolute import under an alias; the same name as a local shadow is no use."""

from shop.schema import SCHEMA_SQL as SCHEMA


def go(conn):
    conn.executescript(SCHEMA)


def shadowed(conn, SCHEMA):
    # the parameter hides the import: nothing is known about this text
    conn.executescript(SCHEMA)
