from pydantic import BaseModel, Field


class User(BaseModel):
    """@schema: user"""

    id: int
    email: str
    display: str = Field(alias="fullName")
