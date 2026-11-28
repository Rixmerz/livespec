def migrate(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS invoices (id INTEGER);
        CREATE TABLE IF NOT EXISTS payments (id INTEGER, invoice_id INTEGER);
        INSERT INTO invoices (id) VALUES (1);
        INSERT INTO payments (id, invoice_id) VALUES (1, 1);
        """
    )
