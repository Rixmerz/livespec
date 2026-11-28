import sqlalchemy as sa
import sqlalchemy.orm as so

from app import db

followers = sa.Table(
    "followers",
    db.metadata,
    sa.Column("follower_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True),
)


class User(db.Model):
    id: so.Mapped[int] = so.mapped_column(primary_key=True)
    posts: so.WriteOnlyMapped["Post"] = so.relationship(back_populates="author")
    following: so.WriteOnlyMapped["User"] = so.relationship(
        secondary=followers, back_populates="followers"
    )
    followers: so.WriteOnlyMapped["User"] = so.relationship(secondary=followers)
    # a target the class does not state: nothing can be said about it
    mystery = so.relationship(lazy="dynamic")

    def follow(self, user):
        self.following.add(user)

    def recent(self):
        return db.session.scalars(self.posts.select())

    def audit(self):
        return self.mystery.filter_by(id=1).all()


class UserRole(db.Model):
    id: so.Mapped[int] = so.mapped_column(primary_key=True)


class Post(db.Model):
    id: so.Mapped[int] = so.mapped_column(primary_key=True)
    author: so.Mapped[User] = so.relationship(back_populates="posts")


class Spirit(db.Model):
    __tablename__ = "spirits"
    id: so.Mapped[int] = so.mapped_column(primary_key=True)


class Admin(User):
    """Single-table inheritance: no key of its own, so the parent's table."""

    level: so.Mapped[int] = so.mapped_column(default=0)


class Staff(User):
    """Joined inheritance: its own key, its own default name."""

    id: so.Mapped[int] = so.mapped_column(sa.ForeignKey("user.id"), primary_key=True)


class Computed(db.Model):
    @staticmethod
    def __tablename__():
        return "computed_things"

    id: so.Mapped[int] = so.mapped_column(primary_key=True)


class Orphan(Unknown):  # noqa: F821 - a base we know nothing about
    id: so.Mapped[int] = so.mapped_column(primary_key=True)
