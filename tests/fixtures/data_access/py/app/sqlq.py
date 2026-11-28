"""Raw SQL in string literals."""

USERS_SQL = "SELECT id, email FROM public.users WHERE active = 1"


def list_users(cur):
    cur.execute(USERS_SQL)


def orders_with_users(cur):
    return cur.execute(
        """
        SELECT o.id, u.email
        FROM orders o
        JOIN "Customers" c ON c.id = o.customer_id
        LEFT JOIN users u ON u.id = c.user_id
        """
    )


def top_buyers(cur):
    cur.execute(
        "WITH recent AS (SELECT * FROM orders WHERE total > 10) "
        "SELECT * FROM recent r JOIN users u ON u.id = r.user_id"
    )


def with_subquery(cur):
    cur.execute("SELECT * FROM users WHERE id IN (SELECT user_id FROM audit_log)")


def archive(cur, since):
    cur.execute(
        f"INSERT INTO orders_archive (id, total) SELECT id, total FROM orders WHERE ts < {since}"
    )


def add_user(cur, email):
    cur.execute("INSERT INTO users (email) VALUES (%s)", (email,))


def rename_user(cur, name):
    cur.execute("UPDATE users SET name = %s WHERE id = 1", (name,))


def drop_user(cur):
    cur.execute('DELETE FROM "users" WHERE id = 1')


def upsert_setting(cur):
    cur.execute("REPLACE INTO settings (k, v) VALUES ('a', 'b')")
    cur.execute(
        "MERGE INTO inventory i USING incoming n ON i.id = n.id WHEN MATCHED THEN UPDATE SET q = n.q"
    )


def migrate(cur):
    cur.execute("CREATE TABLE IF NOT EXISTS feature_flags (name text)")
    cur.execute("ALTER TABLE users ADD COLUMN age int")
    cur.execute("DROP TABLE legacy_things")


def dynamic(cur, table):
    cur.execute(f"SELECT * FROM {table} WHERE 1 = 1")
    cur.execute(f"SELECT * FROM reports_{table} WHERE 1 = 1")


def prose():
    """Update the record from the cache when the user wants to select a file from a list."""
    note = "Select a file from the list and delete from there"
    other = "Update the record"
    return note, other
