"""Настройки бота. Читаются из переменных окружения или файла .env."""
from __future__ import annotations

import os
from functools import cached_property
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_bot_token: str
    notion_token: str
    notion_database_id: str

    # Список через запятую. Пустая строка = режим настройки: бот пускает всех
    # и подсказывает user_id, но громко предупреждает об этом в логах.
    allowed_user_ids: str = ""

    mode: Literal["polling", "webhook"] = "polling"
    base_webhook_url: str = ""
    webhook_path: str = "/tg/webhook"
    webhook_secret: str = ""
    port: int = 10000

    fetch_articles: bool = True
    article_max_chars: int = 40_000
    article_timeout: float = 10.0

    log_level: str = "INFO"

    @cached_property
    def allowed_ids(self) -> frozenset[int]:
        ids = set()
        for chunk in self.allowed_user_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                ids.add(int(chunk))
        return frozenset(ids)

    @property
    def setup_mode(self) -> bool:
        """Белый список пуст — бот ещё не привязан к владельцу."""
        return not self.allowed_ids

    @property
    def webhook_url(self) -> str:
        return f"{self.base_webhook_url.rstrip('/')}{self.webhook_path}"

    @model_validator(mode="after")
    def _check_webhook(self) -> "Settings":
        if not self.base_webhook_url:
            # Render сам публикует адрес сервиса — руками его вписывать не надо.
            self.base_webhook_url = os.getenv("RENDER_EXTERNAL_URL", "")

        if self.mode == "webhook" and not self.base_webhook_url:
            raise ValueError(
                "MODE=webhook требует BASE_WEBHOOK_URL "
                "(на Render он подставляется из RENDER_EXTERNAL_URL автоматически)"
            )
        # Тронем свойство, чтобы кривой ALLOWED_USER_IDS упал на старте,
        # а не при первом сообщении.
        _ = self.allowed_ids
        return self


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
