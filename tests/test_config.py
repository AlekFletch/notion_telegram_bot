"""Тесты настроек — прежде всего секрета вебхука.

Telegram принимает в secret_token только A-Z, a-z, 0-9, «_» и «-». Render
генерирует значение по своим правилам, и на этом контейнер падал с
«secret token contains illegal characters».
"""
from __future__ import annotations

import pytest

from bot.config import Settings

BASE = dict(
    telegram_bot_token="123:AA",
    notion_token="ntn_test",
    notion_database_id="db",
)


def settings(**overrides) -> Settings:
    return Settings(**{**BASE, **overrides})


@pytest.mark.parametrize(
    "secret",
    ["abcDEF123", "with_underscore", "with-dash", "A" * 256, "0123456789"],
)
def test_valid_secret_passes_through(secret):
    assert settings(webhook_secret=secret).webhook_secret == secret


@pytest.mark.parametrize(
    "secret",
    ["Xy+9/abc=", "секрет", "with space", "with.dot", "a" * 257, "sym$bol"],
)
def test_unsafe_secret_becomes_hash(secret):
    result = settings(webhook_secret=secret).webhook_secret

    assert result != secret
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


def test_hash_is_stable_between_restarts():
    """Секрет должен переживать перезапуск: иначе вебхук перестанет сходиться."""
    first = settings(webhook_secret="Xy+9/abc=").webhook_secret
    second = settings(webhook_secret="Xy+9/abc=").webhook_secret
    assert first == second


def test_different_secrets_do_not_collide():
    a = settings(webhook_secret="Xy+9/abc=").webhook_secret
    b = settings(webhook_secret="Xy+9/abd=").webhook_secret
    assert a != b


def test_empty_secret_stays_empty():
    assert settings(webhook_secret="").webhook_secret == ""
    assert settings(webhook_secret="   ").webhook_secret == ""


def test_surrounding_spaces_are_trimmed():
    assert settings(webhook_secret="  token123  ").webhook_secret == "token123"


def test_webhook_mode_requires_base_url():
    with pytest.raises(ValueError, match="BASE_WEBHOOK_URL"):
        settings(mode="webhook", base_webhook_url="")


def test_webhook_url_is_assembled_without_double_slash():
    s = settings(mode="webhook", base_webhook_url="https://x.onrender.com/")
    assert s.webhook_url == "https://x.onrender.com/tg/webhook"


def test_empty_archive_id_means_disabled():
    """В .env переменная может стоять пустой — это «выключено», а не ошибка."""
    settings = Settings(
        telegram_bot_token="1:a",
        notion_token="n",
        notion_database_id="d",
        archive_chat_id="",
    )
    assert settings.archive_chat_id == 0


def test_archive_id_is_read_as_number():
    settings = Settings(
        telegram_bot_token="1:a",
        notion_token="n",
        notion_database_id="d",
        archive_chat_id="-1001234567890",
    )
    assert settings.archive_chat_id == -1001234567890
