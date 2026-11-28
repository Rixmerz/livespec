"""SQL constants used in the same file, as a class attribute and as a fragment."""

LOOKUP = "SELECT id FROM shelves"
BY_OWNER = " WHERE owner_id = ?"
HELP = "Delete from the cart everything you do not want"


class Shelf:
    LIST_SQL = "SELECT id, label FROM labels"

    def labels(self, conn):
        return conn.execute(self.LIST_SQL).fetchall()

    def owned(self, conn, owner):
        return conn.execute(LOOKUP + BY_OWNER, (owner,)).fetchall()

    def describe(self):
        return HELP

    def formatted(self, conn):
        return conn.execute(Shelf.LIST_SQL.format()).fetchall()
