from app.sqlq import add_user, list_users


def register(cur, email):
    add_user(cur, email)
    return audit_trail(cur)


def audit_trail(cur):
    list_users(cur)


def not_a_dao(items):
    return [i for i in items if i]
