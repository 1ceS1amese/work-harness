"""Persisted evidence for a particular upstream connection and model."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Effort = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
COMMON_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


class ReasoningCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effort: Effort | None = None
    status: Literal["supported", "unsupported", "unverified", "error"]
    detail: str = Field(max_length=300)
    accepted: bool = False
    source: Literal["default", "parameter_validation", "channel_confirmation", "unknown"] = (
        "unknown"
    )


class ModelVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    fingerprint: str
    checked_at: datetime
    model_available: bool
    checks: list[ReasoningCheck] = Field(max_length=16)

    @property
    def supported_efforts(self) -> list[str]:
        return [
            check.effort
            for check in self.checks
            if check.effort is not None and check.status == "supported"
        ]


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verification: ModelVerification
    default_effort: Effort | None = None

    @model_validator(mode="after")
    def require_verified_effort(self) -> ModelSettings:
        if self.default_effort is not None and (
            not self.verification.model_available
            or self.default_effort not in self.verification.supported_efforts
        ):
            raise ValueError("推理档位尚未通过该模型的兼容性测试")
        return self
