import asyncio
import time
from datetime import datetime, timezone

from core.orchestrator import Orchestrator
from core.scheduler import SchedulerService
from models import AnalysisResult, NewsEvent


class FakeForex:
    def __init__(self):
        self.checks = 0

    def check_once(self):
        self.checks += 1
        return []


class FakeScheduler:
    def start(self):
        pass

    def shutdown(self, wait=True):
        pass


class FakeAnalyzer:
    def analyze_event(self, event):
        return AnalysisResult(event_id=event.id, asset="XAUUSD", sentiment="BULLISH", impact_score=9, action="BUY", reasoning="Test", confidence=0.9)


class FakeManager:
    def __init__(self):
        self.calls = 0

    async def process_analysis(self, analysis, event, *, planned=False):
        self.calls += 1


def make_event(headline="Iran escalation lifts gold"):
    return NewsEvent(source="forexfactory_news", headline=headline, timestamp=datetime.now(timezone.utc))


def test_scheduler_registers_jobs_and_stops_components(tmp_path):
    forex = FakeForex()
    scheduler = SchedulerService(forex, job_store_url=f"sqlite:///{tmp_path / 'jobs.db'}")

    scheduler.start()
    jobs = {job.id for job in scheduler.scheduler.get_jobs()}
    scheduler.shutdown()

    assert jobs == {"forex_factory_check", "cleanup_old_data", "health_check"}


def test_orchestrator_filters_and_processes_events():
    manager = FakeManager()
    orchestrator = Orchestrator(
        analyzer=FakeAnalyzer(),
        alert_manager=manager,
        scheduler=FakeScheduler(),
        worker_count=1,
        executor_workers=1,
    )

    orchestrator.start()
    assert orchestrator.submit_event(make_event())
    assert not orchestrator.submit_event(make_event("A sunny garden update"))
    deadline = time.time() + 2
    while manager.calls == 0 and time.time() < deadline:
        time.sleep(0.01)
    orchestrator.shutdown()

    assert manager.calls == 1
    assert orchestrator.metrics().queued_events == 1
    assert orchestrator.metrics().filtered_events == 1
