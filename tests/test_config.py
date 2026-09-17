import pytest

from app.config import settings


def test_ai_provider_defaults_to_copilot(config):
    assert config.ai_provider == "copilot"
    assert config.openrouter_enabled is False


def test_openrouter_provider_requires_credentials(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openrouter")
    settings.cache_clear()
    with pytest.raises(ValueError, match="AI_PROVIDER=openrouter requires"):
        settings()


def test_openrouter_provider_enabled_with_credentials(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    settings.cache_clear()
    config = settings()
    assert config.ai_provider == "openrouter"
    assert config.openrouter_enabled is True
