from fastapi import FastAPI

from app.service import build_user

app = FastAPI()


@app.get("/api/users/{user_id}")
def get_user(user_id):
    return build_user({"id": user_id})
