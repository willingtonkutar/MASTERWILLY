from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from models import (
    Alert,
    AnalysisResult,
    NewsEvent,
    create_alert,
    create_analysis_result,
    create_news_event,
    deserialize_model,
)


def test_news_event_factory_and_serialization():
    event = create_news_event(
        source="twitter",
        headline="Fed signals rates may remain unchanged",
        keywords=["fed", "rates"],
        asset_mentions=["XAUUSD", "DXY"],
    )

    restored = deserialize_model(NewsEvent, event.to_json())

    assert restored.id == event.id
    assert restored.timestamp.tzinfo is not None
    assert restored.to_dict()["source"] == "twitter"


def test_analysis_result_validates_bounds_and_serializes_uuid():
    event_id = uuid4()
    result = create_analysis_result(
        event_id=event_id,
        asset="XAUUSD",
        sentiment="BULLISH",
        impact_score=9,
        action="LOOK FOR BUYS",
        reasoning="Safe-haven demand is increasing.",
        confidence=0.85,
    )

    assert result.to_dict()["event_id"] == str(event_id)
    assert result.analyzed_at.tzinfo is not None

    with pytest.raises(ValidationError):
        AnalysisResult(
            event_id=event_id,
            asset="XAUUSD",
            sentiment="BULLISH",
            impact_score=11,
            action="LOOK FOR BUYS",
            reasoning="Reason",
            confidence=0.85,
        )


def test_alert_factory_defaults_to_pending():
    alert = create_alert(
        event_id=uuid4(),
        analysis_result_id=uuid4(),
        formatted_message="Impact 9: look for buys.",
    )

    assert alert.status == "pending"
    assert alert.sent_at is None


def test_models_reject_invalid_literals_and_empty_text():
    with pytest.raises(ValidationError):
        NewsEvent(source="rss", headline="Headline")

    with pytest.raises(ValidationError):
        NewsEvent(source="twitter", headline="   ")

    with pytest.raises(ValidationError):
        AnalysisResult(
            event_id=uuid4(),
            asset="XAUUSD",
            sentiment="UNKNOWN",
            impact_score=5,
            action="WAIT",
            reasoning="Reason",
            confidence=1.1,
        )


def test_alert_accepts_explicit_utc_sent_time():
    sent_at = datetime.now(timezone.utc)
    alert = Alert(
        event_id=uuid4(),
        analysis_result_id=uuid4(),
        formatted_message="Sent",
        sent_at=sent_at,
        status="sent",
    )

    assert alert.sent_at == sent_at
