"""Validated data contracts shared by ingestion, analysis, and alert delivery."""

from datetime import datetime, timezone
from json import loads
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


Source = Literal["forexfactory", "forexfactory_news", "twitter"]
Sentiment = Literal["BULLISH", "BEARISH", "NEUTRAL"]
AlertStatus = Literal["pending", "sent", "failed"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _required_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    return value


class ModelBase(BaseModel):
    """Common model behavior and serialization helpers."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    def to_dict(self) -> dict[str, Any]:
        """Serialize the model to JSON-compatible Python values."""
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        """Serialize the model to a JSON string."""
        return self.model_dump_json()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelBase":
        """Create a model from a dictionary or JSON-compatible dictionary."""
        return cls.model_validate(data)


class NewsEvent(ModelBase):
    """A normalized event received from a monitored news source."""

    id: UUID = Field(default_factory=uuid4)
    source: Source
    headline: str
    content: str | None = None
    url: str | None = None
    timestamp: datetime = Field(default_factory=_utc_now)
    keywords: list[str] = Field(default_factory=list)
    asset_mentions: list[str] = Field(default_factory=list)

    _validate_headline = field_validator("headline")(_required_text)


class AnalysisResult(ModelBase):
    """Structured sentiment and impact assessment for a news event."""

    event_id: UUID
    asset: str
    sentiment: Sentiment
    impact_score: int = Field(ge=1, le=10)
    action: str
    reasoning: str
    confidence: float = Field(ge=0.0, le=1.0)
    analyzed_at: datetime = Field(default_factory=_utc_now)

    _validate_text = field_validator("asset", "action", "reasoning")(_required_text)


class Alert(ModelBase):
    """A Telegram-ready alert and its delivery state."""

    id: UUID = Field(default_factory=uuid4)
    event_id: UUID
    analysis_result_id: UUID
    formatted_message: str
    sent_at: datetime | None = None
    status: AlertStatus = "pending"

    _validate_message = field_validator("formatted_message")(_required_text)


def create_news_event(**data: Any) -> NewsEvent:
    """Build a NewsEvent using generated id and timestamp defaults."""
    return NewsEvent(**data)


def create_analysis_result(**data: Any) -> AnalysisResult:
    """Build an AnalysisResult using the generated analysis timestamp default."""
    return AnalysisResult(**data)


def create_alert(**data: Any) -> Alert:
    """Build a pending Alert using a generated alert id by default."""
    return Alert(**data)


def deserialize_model(model: type[ModelBase], payload: str) -> ModelBase:
    """Deserialize JSON into a requested model with normal Pydantic validation."""
    return model.model_validate(loads(payload))
