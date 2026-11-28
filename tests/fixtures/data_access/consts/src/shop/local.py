"""SQL built in local variables inside one function."""


def search(conn, term):
    where = "WHERE name LIKE ?"
    base = "SELECT id FROM books"
    return conn.execute(base + " " + where, (term,)).fetchall()


def with_subquery(conn):
    recent = "SELECT author_id FROM loans"
    query = f"SELECT name FROM authors WHERE id IN ({recent})"
    return conn.execute(query).fetchall()


def conditional(conn, only_open):
    sql = "SELECT id FROM tickets"
    if only_open:
        sql += " JOIN statuses ON statuses.ticket_id = tickets.id"
    return conn.execute(sql).fetchall()


def reassigned(conn, flag):
    q = build_query(flag)  # noqa: F821
    q = other_query(q)  # noqa: F821
    return conn.execute(q).fetchall()


def rebound(conn, flag):
    if flag:
        stmt = "SELECT id FROM red_things"
    else:
        stmt = "SELECT id FROM blue_things"
    return conn.execute(stmt).fetchall()


def from_parameter(conn, query):
    stmt = query
    return conn.execute(stmt).fetchall()


def dynamic(conn, table):
    conn.execute(f"DELETE FROM {table}")
    sql = "SELECT * FROM " + table
    return conn.execute(sql).fetchall()
