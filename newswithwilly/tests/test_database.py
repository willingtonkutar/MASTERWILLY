import asyncio

from db.database import DatabaseService
from models import CalendarEventData


def test_database_service_creates_and_reads_news_event(tmp_path):
    async def scenario():
        service = DatabaseService(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
        await service.create_tables()
        event = await service.create_news_event({
            "source": "forexfactory",
            "headline": "CPI released",
            "timestamp": "2026-08-24T12:00:00+00:00",
            "keywords": ["cpi"],
            "asset_mentions": ["XAUUSD"],
        })
        loaded = await service.get_news_event(event.id)
        await service.close()
        return event, loaded

    event, loaded = asyncio.run(scenario())

    assert loaded is not None
    assert loaded.id == event.id
    assert loaded.keywords == "cpi"
    assert loaded.asset_mentions == "XAUUSD"


def test_database_service_persists_calendar_data(tmp_path):
    async def scenario():
        service = DatabaseService(f"sqlite+aiosqlite:///{tmp_path / 'calendar.db'}")
        await service.create_tables()
        event = await service.create_news_event({
            "source": "forexfactory",
            "headline": "US CPI",
            "timestamp": "2026-08-24T12:00:00+00:00",
            "calendar": CalendarEventData(currency="USD", impact_level="high", previous="3.2%", forecast="3.5%"),
        })
        loaded = await service.get_news_event(event.id)
        await service.close()
        return loaded

    loaded = asyncio.run(scenario())

    assert loaded is not None
    assert loaded.calendar_data is not None
