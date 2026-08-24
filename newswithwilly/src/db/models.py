"""SQLAlchemy persistence models for normalized news and alerts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for all database models."""


class NewsEvent(Base):
    __tablename__ = "news_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    keywords: Mapped[str] = mapped_column(Text, default="", nullable=False)
    asset_mentions: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    analyses: Mapped[list[AnalysisResult]] = relationship(back_populates="event", cascade="all, delete-orphan")
    alerts: Mapped[list[Alert]] = relationship(back_populates="event", cascade="all, delete-orphan")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "headline": self.headline,
            "content": self.content,
            "url": self.url,
            "timestamp": self.timestamp,
            "keywords": _split_values(self.keywords),
            "asset_mentions": _split_values(self.asset_mentions),
            "created_at": self.created_at,
        }


class AnalysisResult(Base):
    __tablename__ = "analysis_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    event_id: Mapped[str] = mapped_column(ForeignKey("news_events.id", ondelete="CASCADE"), nullable=False, index=True)
    asset: Mapped[str] = mapped_column(String(32), nullable=False)
    sentiment: Mapped[str] = mapped_column(String(16), nullable=False)
    impact_score: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    event: Mapped[NewsEvent] = relationship(back_populates="analyses")
    alerts: Mapped[list[Alert]] = relationship(back_populates="analysis_result", cascade="all, delete-orphan")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    event_id: Mapped[str] = mapped_column(ForeignKey("news_events.id", ondelete="CASCADE"), nullable=False, index=True)
    analysis_result_id: Mapped[str] = mapped_column(ForeignKey("analysis_results.id", ondelete="CASCADE"), nullable=False, index=True)
    formatted_message: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)

    event: Mapped[NewsEvent] = relationship(back_populates="alerts")
    analysis_result: Mapped[AnalysisResult] = relationship(back_populates="alerts")


def _split_values(value: str) -> list[str]:
    return [item for item in value.split(",") if item]
