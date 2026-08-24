"""Async SQLAlchemy service with SQLite and PostgreSQL support."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Alert, AnalysisResult, Base, NewsEvent


class DatabaseService:
    """Own the async engine and provide transaction-scoped CRUD operations."""

    def __init__(self, database_url: str | None = None, *, echo: bool = False, pool_size: int = 5) -> None:
        self.database_url = database_url or os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./newswithwilly.db")
        engine_options: dict[str, Any] = {"echo": echo, "pool_pre_ping": True}
        if not self.database_url.startswith("sqlite"):
            engine_options.update(pool_size=pool_size, max_overflow=max(2, pool_size))
        self.engine: AsyncEngine = create_async_engine(self.database_url, **engine_options)
        self._session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session and commit on success, rolling back on failure."""
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def create_tables(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    async def check_connection(self) -> bool:
        """Verify that the configured database accepts a lightweight query."""
        from sqlalchemy import text

        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    async def create_news_event(self, data: Mapping[str, Any]) -> NewsEvent:
        event = NewsEvent(**_normalize_event_data(data))
        async with self.session() as session:
            session.add(event)
            await session.flush()
        return event

    async def get_news_event(self, event_id: str | UUID) -> NewsEvent | None:
        async with self.session() as session:
            return await session.get(NewsEvent, str(event_id))

    async def list_news_events(self, limit: int = 100) -> list[NewsEvent]:
        _validate_limit(limit)
        async with self.session() as session:
            result = await session.execute(select(NewsEvent).order_by(NewsEvent.timestamp.desc()).limit(limit))
            return list(result.scalars())

    async def update_news_event(self, event_id: str | UUID, values: Mapping[str, Any]) -> NewsEvent | None:
        async with self.session() as session:
            event = await session.get(NewsEvent, str(event_id))
            if event is None:
                return None
            normalized = _normalize_event_data(values)
            for field, value in normalized.items():
                if field not in {"source", "headline", "content", "url", "timestamp", "keywords", "asset_mentions"}:
                    raise ValueError(f"Unsupported news event field: {field}")
                setattr(event, field, value)
            await session.flush()
            return event

    async def delete_analysis_result(self, result_id: str | UUID) -> bool:
        async with self.session() as session:
            result = await session.execute(delete(AnalysisResult).where(AnalysisResult.id == str(result_id)))
            return result.rowcount == 1

    async def delete_news_event(self, event_id: str | UUID) -> bool:
        async with self.session() as session:
            result = await session.execute(delete(NewsEvent).where(NewsEvent.id == str(event_id)))
            return result.rowcount == 1

    async def create_analysis_result(self, data: Mapping[str, Any]) -> AnalysisResult:
        result = AnalysisResult(**_stringify_ids(data))
        async with self.session() as session:
            session.add(result)
            await session.flush()
        return result

    async def get_analysis_result(self, result_id: str | UUID) -> AnalysisResult | None:
        async with self.session() as session:
            return await session.get(AnalysisResult, str(result_id))

    async def list_analysis_results(self, event_id: str | UUID | None = None) -> list[AnalysisResult]:
        async with self.session() as session:
            statement = select(AnalysisResult).order_by(AnalysisResult.analyzed_at.desc())
            if event_id is not None:
                statement = statement.where(AnalysisResult.event_id == str(event_id))
            result = await session.execute(statement)
            return list(result.scalars())

    async def update_analysis_result(self, result_id: str | UUID, values: Mapping[str, Any]) -> AnalysisResult | None:
        async with self.session() as session:
            analysis = await session.get(AnalysisResult, str(result_id))
            if analysis is None:
                return None
            allowed = {"asset", "sentiment", "impact_score", "action", "reasoning", "confidence", "analyzed_at"}
            for field, value in _stringify_ids(values).items():
                if field not in allowed:
                    raise ValueError(f"Unsupported analysis result field: {field}")
                setattr(analysis, field, value)
            await session.flush()
            return analysis

    async def create_alert(self, data: Mapping[str, Any]) -> Alert:
        alert = Alert(**_stringify_ids(data))
        async with self.session() as session:
            session.add(alert)
            await session.flush()
        return alert

    async def get_alert(self, alert_id: str | UUID) -> Alert | None:
        async with self.session() as session:
            return await session.get(Alert, str(alert_id))

    async def list_alerts(self, event_id: str | UUID | None = None) -> list[Alert]:
        async with self.session() as session:
            statement = select(Alert).order_by(Alert.id)
            if event_id is not None:
                statement = statement.where(Alert.event_id == str(event_id))
            result = await session.execute(statement)
            return list(result.scalars())

    async def update_alert(self, alert_id: str | UUID, values: Mapping[str, Any]) -> Alert | None:
        async with self.session() as session:
            alert = await session.get(Alert, str(alert_id))
            if alert is None:
                return None
            for field, value in values.items():
                if field not in {"formatted_message", "sent_at", "status"}:
                    raise ValueError(f"Unsupported alert field: {field}")
                setattr(alert, field, value)
            await session.flush()
            return alert

    async def delete_alert(self, alert_id: str | UUID) -> bool:
        async with self.session() as session:
            result = await session.execute(delete(Alert).where(Alert.id == str(alert_id)))
            return result.rowcount == 1


def _normalize_event_data(data: Mapping[str, Any]) -> dict[str, Any]:
    values = dict(data)
    for field in ("keywords", "asset_mentions"):
        if isinstance(values.get(field), (list, tuple)):
            values[field] = ",".join(str(item) for item in values[field])
    if isinstance(values.get("timestamp"), str):
        values["timestamp"] = datetime.fromisoformat(values["timestamp"])
    return values


def _stringify_ids(data: Mapping[str, Any]) -> dict[str, Any]:
    values = dict(data)
    for field in ("event_id", "analysis_result_id"):
        if field in values:
            values[field] = str(values[field])
    for field in ("analyzed_at", "sent_at"):
        if isinstance(values.get(field), str):
            values[field] = datetime.fromisoformat(values[field])
    return values


def _validate_limit(limit: int) -> None:
    if limit < 1:
        raise ValueError("limit must be at least 1")
