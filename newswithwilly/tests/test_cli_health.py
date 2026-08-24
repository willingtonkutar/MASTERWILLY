from datetime import datetime, timezone

from core.health import HealthCheck
from main import build_parser
from newswithwilly.main import build_parser as package_build_parser
from pipeline_queue.event_queue import EventQueue


def test_cli_commands_and_options_parse():
    args = build_parser().parse_args(["analyze", "Fed raises rates", "--config", "custom.env", "--dry-run", "--once"])

    assert args.command == "analyze"
    assert args.headline == "Fed raises rates"
    assert args.dry_run
    assert args.once


def test_package_cli_accepts_run_and_dry_run_flags():
    args = package_build_parser().parse_args(["run", "--dry-run", "--once"])

    assert args.command == "run"
    assert args.dry_run
    assert args.once


def test_health_report_aggregates_component_status_and_metrics():
    event_queue = EventQueue(max_size=2)
    health = HealthCheck(event_queue=event_queue, claude=object(), telegram=object())
    health.record_event_processed()
    health.record_analysis(0.25, True)
    health.record_api_call("twitter", False)
    health.record_alert_sent()

    report = health.report()

    assert report.status == "YELLOW"
    assert report.metrics["queue_backlog"] == 0
    assert report.metrics["average_analysis_time_seconds"] == 0.25
    assert report.metrics["alerts_sent_per_hour"] == 1
    assert report.metrics["api_success_rate"] < 1
