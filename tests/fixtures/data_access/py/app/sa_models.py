from sqlalchemy import Column, Integer, MetaData, String, Table, delete, insert, select, update
from sqlalchemy.orm import declarative_base

Base = declarative_base()
metadata = MetaData()

audit = Table("audit_log", metadata, Column("id", Integer))


class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    name = Column(String)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = {"schema": "billing"}  # noqa: RUF012
    id = Column(Integer, primary_key=True)


def find_account(session, aid):
    return session.query(Account).filter(Account.id == aid).first()


def stmt_reads(session):
    return session.execute(select(Invoice).where(Invoice.id == 1))


def stmt_writes(session):
    session.execute(insert(Account).values(name="a"))
    session.execute(update(Invoice).values(id=2))
    session.execute(delete(Account))


def add_account(session):
    acc = Account(name="n")
    session.add(acc)


def touch_audit(conn):
    conn.execute(select(audit))
