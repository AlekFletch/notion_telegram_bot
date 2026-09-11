"""Настройки бота. Читаются из переменных окружения или файла .env."""
from __future__ import annotations

import hashlib
import os
import re
from functools import cached_property
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Что Telegram разрешает в secret_token вебхука.
_SAFE_SECRET = re.compile(r"[A-Za-z0-9_-]{1,256}")


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

    # Приватный канал, куда бот пересылает видео и всё, что не влезло в Notion.
    # Идентификатор вида -100…; 0 = архив выключен, поведение как раньше.
    archive_chat_id: int = 0

    fetch_articles: bool = True
    article_max_chars: int = 40_000
    article_timeout: float = 10.0

    # Посты из соцсетей: качаем вложения через yt-dlp и кладём в архив.
    # Instagram и Facebook анонимному запросу не отдают ничего, поэтому
    # обычный разбор статьи на таких ссылках бесполезен.
    social_download: bool = True
    social_hosts: str = "instagram.com,instagr.am,facebook.com,fb.watch"
    # Путь к cookies в формате Netscape. На Render это Secret File,
    # то есть /etc/secrets/<имя файла>. Без них Instagram требует логин.
    social_cookies_file: str = ""
    social_max_items: int = 10
    social_timeout: float = 30.0

    log_level: str = "INFO"

    @field_validator("archive_chat_id", mode="before")
    @classmethod
    def _empty_archive_is_off(cls, value):
        """Пустая строка в .env — это «выключено», а не повод падать на старте."""
        if isinstance(value, str) and not value.strip():
            return 0
        return value

    @cached_property
    def social_domains(self) -> frozenset[str]:
        from bot.social import hosts_from

        return hosts_from(self.social_hosts)

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
    def _normalize_secret(self) -> "Settings":
        """Привести секрет вебхука к набору символов, который принимает Telegram.

        Telegram допускает в secret_token только A-Z, a-z, 0-9, «_» и «-».
        Render генерирует значение по своим правилам, и оно эти рамки нарушает —
        setWebhook отвечает «secret token contains illegal characters» и
        контейнер падает. Вместо того чтобы полагаться на удачу, непригодное
        значение заменяем его хешем: он стабилен между перезапусками, состоит
        из разрешённых символов и не раскрывает исходную строку.
        """
        secret = self.webhook_secret.strip()
        if secret and not _SAFE_SECRET.fullmatch(secret):
            self.webhook_secret = hashlib.sha256(secret.encode()).hexdigest()
        else:
            self.webhook_secret = secret
        return self

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
