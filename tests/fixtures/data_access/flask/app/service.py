"""No SQLAlchemy import here: `select()` on a collection attribute is still seen."""


def followed_by(user):
    return list(user.following.select())


def tagged(item):
    # `add` on a set is just a set: without SQLAlchemy in the file nothing is claimed
    item.tags.add("x")
