import pytest

from newswithwilly.config import Settings
from newswithwilly.errors import ConfigurationError


def test_settings_parse_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("KEYWORDS", "Iran, CPI, gold")
    monkeypatch.setenv("IMPACT_THRESHOLD", "8")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))

    settings = Settings.from_environment(env_file=None)

    assert settings.keywords == ("iran", "cpi", "gold")
    assert settings.impact_threshold == 8
    assert settings.log_file == tmp_path / "app.log"


def test_settings_reject_invalid_threshold(monkeypatch):
    monkeypatch.setenv("KEYWORDS", "gold")
    monkeypatch.setenv("IMPACT_THRESHOLD", "11")

    with pytest.raises(ConfigurationError, match="IMPACT_THRESHOLD"):
        Settings.from_environment(env_file=None)
