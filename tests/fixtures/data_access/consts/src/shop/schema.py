"""SQL held in module constants: a script, a query, DDL nobody runs, and prose."""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS widgets (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS gadgets (id INTEGER PRIMARY KEY, widget_id INTEGER);
INSERT INTO widgets (id, name) VALUES (1, 'seed');
"""

# Defined here, executed by nobody: its table must still count as declared.
ORPHAN_DDL = """
CREATE TABLE orphans (id INTEGER PRIMARY KEY);
"""

_ROW_SELECT = "SELECT id, name FROM widgets"

# Looks like SQL to a keyword matcher; it is an English sentence.
HELP_TEXT = "Select a file from the list"
UPDATE_NOTICE = "Update the record before you leave"


def current_version() -> str:
    return HELP_TEXT


def widget_rows(conn):
    return conn.execute(f"{_ROW_SELECT} WHERE id = ?", (1,)).fetchall()
