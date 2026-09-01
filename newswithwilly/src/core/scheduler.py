"""Persistent APScheduler jobs for external monitoring and maintenance."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable
from typing import Any

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)
_JOB_REGISTRY: dict[str, "SchedulerService"] = {}


def _run_registered_job(service_key: str, job_name: str) -> Any:
    service = _JOB_REGISTRY.get(service_key)
    if service is None:
        raise RuntimeError(f"No live scheduler registered for {service_key}")
    return service._run_job(job_name, getattr(service, job_name))


class SchedulerService:
    """Schedule monitoring jobs and manage their lifecycle."""

    def __init__(
        self,
        forex_factory: Any,
        twitter_monitor: Any | None = None,
        *,
        news_monitor: Any | None = None,
        news_check_callback: Callable[[], Any] | None = None,
        critical_news_check_callback: Callable[[], Any] | None = None,
        news_interval_minutes: int = 5,
        critical_news_interval_minutes: int = 2,
        event_process_window_hours: int = 1,
        cleanup_callback: Callable[[], Any] | None = None,
        health_callback: Callable[[], Any] | None = None,
        job_store_url: str | None = None,
        forex_interval_minutes: int = 5,
    ) -> None:
        if forex_interval_minutes < 1:
            raise ValueError("forex_interval_minutes must be at least 1")
        self.forex_factory = forex_factory
        self.twitter_monitor = twitter_monitor
        self.news_monitor = news_monitor
        if news_interval_minutes < 1 or critical_news_interval_minutes < 1 or event_process_window_hours < 1:
            raise ValueError("news intervals and event window must be at least 1")
        self.news_check_callback = news_check_callback
        self.critical_news_check_callback = critical_news_check_callback
        self.news_interval_minutes = news_interval_minutes
        self.critical_news_interval_minutes = critical_news_interval_minutes
        self.event_process_window_hours = event_process_window_hours
        self.cleanup_callback = cleanup_callback or _noop
        self.health_callback = health_callback or _noop
        configured_url = job_store_url or os.getenv("SCHEDULER_DATABASE_URL", "sqlite:///./scheduler.db")
        self._scheduler = BackgroundScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=_sync_database_url(configured_url))},
            executors={"default": ThreadPoolExecutor(4)},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
            timezone="UTC",
        )
        self.forex_interval_minutes = forex_interval_minutes
        self._lock = threading.Lock()
        self._started = False
        self._scheduler.add_listener(self._job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

    def start(self) -> None:
        """Register persistent jobs and start the scheduler and Twitter stream."""
        with self._lock:
            if self._started:
                raise RuntimeError("scheduler is already running")
            
            service_key = f"{id(self)}"
            _JOB_REGISTRY[service_key] = self
            job_args = (service_key,)
            if hasattr(self.forex_factory, "start_adaptive_checks"):
                self.forex_factory.start_adaptive_checks(process_window_hours=self.event_process_window_hours)
            else:
                self._scheduler.add_job(_run_registered_job, "interval", minutes=self.forex_interval_minutes, args=job_args + ("forex_factory_check",), id="forex_factory_check", replace_existing=True)
            if self.news_check_callback is not None:
                self._scheduler.add_job(_run_registered_job, "interval", minutes=self.news_interval_minutes, args=job_args + ("forex_news_check",), id="forex_news_check", replace_existing=True)
            if self.critical_news_check_callback is not None:
                self._scheduler.add_job(_run_registered_job, "interval", minutes=self.critical_news_interval_minutes, args=job_args + ("critical_news_check",), id="critical_news_check", replace_existing=True)
            self._scheduler.add_job(_run_registered_job, "interval", hours=24, args=job_args + ("cleanup_old_data",), id="cleanup_old_data", replace_existing=True)
            self._scheduler.add_job(_run_registered_job, "interval", hours=1, args=job_args + ("health_check",), id="health_check", replace_existing=True)
            self._scheduler.start()
            # Clear any stale jobs from previous runs after starting the scheduler
            try:
                for job in list(self._scheduler.get_jobs()):
                    # Only keep jobs that have the current service_key in their args
                    if not (job.args and len(job.args) > 0 and str(job.args[0]) == str(service_key)):
                        self._scheduler.remove_job(job.id)
                        logger.info("Removed stale job: %s", job.id)
            except Exception as e:
                logger.warning("Failed to clean up stale jobs: %s", e)
            if self.twitter_monitor is not None:
                self.twitter_monitor.start(background=True)
            self._started = True
        logger.info("Scheduler started with persistent jobs")

    def shutdown(self, wait: bool = True) -> None:
        """Stop Twitter and scheduler jobs without interrupting running work."""
        with self._lock:
            if not self._started:
                return
            if self.twitter_monitor is not None:
                self.twitter_monitor.stop(timeout=10)
            if hasattr(self.forex_factory, "stop_scheduled_checks"):
                self.forex_factory.stop_scheduled_checks(timeout=10)
            self._scheduler.shutdown(wait=wait)
            _JOB_REGISTRY.pop(f"{id(self)}", None)
            self._started = False
        logger.info("Scheduler shut down")

    def forex_factory_check(self) -> Any:
        return self._run_job("forex_factory_check", self.forex_factory.check_once)

    def forex_news_check(self) -> Any:
        if self.news_check_callback is None:
            return None
        return self._run_job("forex_news_check", self.news_check_callback)

    def critical_news_check(self) -> Any:
        if self.critical_news_check_callback is None:
            return None
        return self._run_job("critical_news_check", self.critical_news_check_callback)

    def cleanup_old_data(self) -> Any:
        return self._run_job("cleanup_old_data", self.cleanup_callback)

    def health_check(self) -> Any:
        return self._run_job("health_check", self.health_callback)

    @property
    def scheduler(self) -> BackgroundScheduler:
        return self._scheduler

    @staticmethod
    def _run_job(name: str, callback: Callable[[], Any]) -> Any:
        logger.info("Starting scheduled job: %s", name)
        try:
            result = callback()
            if asyncio.iscoroutine(result):
                result = asyncio.run(result)
            logger.info("Finished scheduled job: %s", name)
            return result
        except Exception:
            logger.exception("Scheduled job failed: %s", name)
            raise

    @staticmethod
    def _job_listener(event: Any) -> None:
        if event.exception:
            logger.error("Job %s failed", event.job_id)
        else:
            logger.debug("Job %s executed successfully", event.job_id)


def _sync_database_url(url: str) -> str:
    """SQLAlchemyJobStore is synchronous even when the app database is async."""
    return url.replace("+aiosqlite", "").replace("+asyncpg", "")


def _noop() -> None:
    return None
