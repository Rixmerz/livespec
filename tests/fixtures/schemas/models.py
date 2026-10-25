"""Fixture for Python schema extraction: Pydantic models and dataclasses."""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import InitVar, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Literal, Optional, Union
from uuid import UUID

import pydantic
from pydantic import BaseModel, ConfigDict, EmailStr, Field, constr


class Base(BaseModel):
    id: int
    created_at: datetime


class User(Base):
    """@schema: user-profile"""

    model_config = ConfigDict(frozen=True)
    registry: ClassVar[dict[str, int]] = {}
    _secret: str = "x"

    email: str | None
    nickname: str = "x"
    status: Status
    kind: Literal["a", "b"]
    tags: list[str] = Field(default_factory=list)
    orders: list[OrderItem]
    alias: Optional[str] = Field(None, alias="a")  # noqa: UP045 - the form under test
    login: EmailStr = Field(..., description="required")
    code: constr(max_length=3)
    born: date
    balance: Decimal
    external_id: UUID
    extra: dict[str, Any]
    ratio: Annotated[float, Field(ge=0)]
    either: Union[str, int]  # noqa: UP007 - the form under test
    pair: tuple[int, str]
    many: tuple[int, ...]
    owner: "OrderItem"  # noqa: UP037 - a forward reference under test
    id: int = 0


class Status(enum.Enum):
    ACTIVE = "active"


class Plain:
    name: str


class Settings(pydantic.BaseModel):
    debug: bool = False


@dataclass
class OrderItem:
    sku: str
    qty: int = 1
    labels: list[str] = field(default_factory=list)
    note: str | None = field()
    seed: InitVar[int] = 0


@dataclasses.dataclass(frozen=True)
class GiftItem(OrderItem):
    message: str = ""
    qty: int = 2
