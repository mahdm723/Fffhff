"""Request bodies. Fields are loosely typed on purpose: business validation
(with user-facing Arabic messages) happens in the service layer; these models
only bound sizes and drop unknown fields."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ChallengeBody(_Body):
    purpose: str = Field(max_length=16)


class RegisterBody(_Body):
    email: str = Field(max_length=320)
    password: str = Field(max_length=512)
    password_confirm: str = Field(max_length=512)
    antibot: dict[str, Any] | None = None
    website: str | None = Field(default=None, max_length=200)  # honeypot: humans never fill it
    gender: str | None = Field(default=None, max_length=12)  # male|female|unspecified (required)
    age_confirmed: bool = False  # "I am 18+ and accept the terms" (required)


class LoginBody(_Body):
    email: str = Field(max_length=320)
    password: str = Field(max_length=512)
    antibot: dict[str, Any] | None = None


class GoogleBody(_Body):
    credential: str = Field(max_length=8192)


class SendBody(_Body):
    content: str = Field(max_length=20_000)
    client_id: str | None = Field(default=None, max_length=64)


class ReportBody(_Body):
    reason: str = Field(max_length=32)
    details: str | None = Field(default=None, max_length=2000)


class PushKeys(_Body):
    p256dh: str = Field(max_length=255)
    auth: str = Field(max_length=255)


class PushSubscribeBody(_Body):
    endpoint: str = Field(max_length=1024)
    keys: PushKeys


class PushUnsubscribeBody(_Body):
    endpoint: str = Field(max_length=1024)


class ResolveReportBody(_Body):
    action: str = Field(max_length=16)


class UserStatusBody(_Body):
    status: str = Field(max_length=16)


class ResetRequestBody(_Body):
    email: str = Field(max_length=320)
    antibot: dict[str, Any] | None = None


class ResetVerifyBody(_Body):
    email: str = Field(max_length=320)
    code: str = Field(max_length=32)


class ResetCompleteBody(_Body):
    reset_token: str = Field(max_length=128)
    password: str = Field(max_length=512)
    password_confirm: str = Field(max_length=512)
    antibot: dict[str, Any] | None = None
