import sqlalchemy as sa

from app import db
from app.models import Admin, Computed, Orphan, Post, Staff, User, UserRole


def one(username):
    return db.session.scalar(sa.select(User).where(User.username == username))


def page(number):
    query = sa.select(Post).order_by(Post.id.desc())
    return db.paginate(query, page=number)


def role(role_id):
    return db.get_or_404(UserRole, role_id)


def first(name):
    return db.first_or_404(sa.select(User).where(User.username == name))


def how_many():
    return db.session.scalar(sa.select(sa.func.count()).select_from(Post))


def posts_of(user):
    return db.session.scalars(user.posts.select())


def admins():
    return db.session.scalars(sa.select(Admin))


def staff():
    return db.session.scalars(sa.select(Staff))


def computed():
    return db.session.scalars(sa.select(Computed))


def orphans():
    return db.session.scalars(sa.select(Orphan))


def untyped(cls):
    return db.session.scalars(sa.select(cls))


def add_role(role_id):
    db.session.add(UserRole(id=role_id))
