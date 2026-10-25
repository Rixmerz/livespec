from app.models import User


def build_user(data):
    return User(**data)
