import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Account(Base):
    """Plain declarative SQLAlchemy has no default table name."""

    id: Mapped[int] = mapped_column(primary_key=True)


class Ledger(Base):
    __tablename__ = "ledgers"
    id: Mapped[int] = mapped_column(primary_key=True)


class Widget(store.Model):  # noqa: F821 - `store` is not a Flask-SQLAlchemy instance
    id: Mapped[int] = mapped_column(primary_key=True)


def balance(session):
    return session.scalar(sa.select(Account))


def entries(session):
    return session.scalars(sa.select(Ledger)).all()


def widgets(session):
    return session.scalars(sa.select(Widget)).all()
