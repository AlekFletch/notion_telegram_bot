"""Тонкий асинхронный клиент Notion API поверх httpx.

Официальный Python-SDK не покрывает загрузку файлов, поэтому здесь прямые
вызовы REST. Из версии API 2025-09-03 у баз появились data sources: страница
создаётся с родителем data_source_id, а запросы уходят в /v1/data_sources/...
Поэтому клиент один раз при старте резолвит data_source_id по database_id.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from bot.formatting import batched

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"

#: Консервативный запас, если API не сообщил лимит (free-план Notion).
DEFAULT_MAX_UPLOAD = 5 * 1024 * 1024

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4


class NotionError(RuntimeError):
    """Notion ответил ошибкой, которую нет смысла повторять."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"Notion {status} [{code}]: {message}")
        self.status = status
        self.code = code
        self.notion_message = message


@dataclass
class Uploaded:
    """Успешно загруженный в Notion файл."""

    id: str
    filename: str


def file_block(kind: str, upload_id: str, caption: str | None = None) -> dict:
    """Блок Notion, ссылающийся на загруженный файл.

    kind — один из image / video / audio / pdf / file.
    """
    payload: dict[str, Any] = {
        "type": "file_upload",
        "file_upload": {"id": upload_id},
    }
    if caption:
        payload["caption"] = [
            {"type": "text", "text": {"content": caption[:2000]}}
        ]
    return {"object": "block", "type": kind, kind: payload}


class NotionClient:
    def __init__(self, token: str, database_id: str, *, version: str = NOTION_VERSION):
        self._database_id = database_id.replace("-", "")
        self._version = version
        self._client = httpx.AsyncClient(
            base_url=API,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": version,
            },
        )
        self.data_source_id: str | None = None
        self.max_upload_bytes: int = DEFAULT_MAX_UPLOAD

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Транспорт
    # ------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        last_error: Exception | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:  # сеть моргнула
                last_error = exc
                if attempt == _MAX_ATTEMPTS:
                    raise
                await asyncio.sleep(self._backoff(attempt))
                continue

            if response.status_code < 300:
                return response.json() if response.content else {}

            body = self._error_body(response)
            code = body.get("code", "unknown")
            message = body.get("message", response.text[:300])

            if response.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                delay = self._retry_after(response) or self._backoff(attempt)
                log.warning(
                    "Notion %s %s → %s (%s). Повтор через %.1f с",
                    method, path, response.status_code, code, delay,
                )
                await asyncio.sleep(delay)
                continue

            raise NotionError(response.status_code, code, message)

        raise last_error or RuntimeError("Notion: запрос не удался")

    @staticmethod
    def _error_body(response: httpx.Response) -> dict:
        try:
            body = response.json()
            return body if isinstance(body, dict) else {}
        except ValueError:
            return {}

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return min(float(raw), 30.0)
        except ValueError:
            return None

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2 ** attempt, 16) * (0.5 + random.random() / 2)

    # ------------------------------------------------------------------
    # Инициализация
    # ------------------------------------------------------------------

    async def prepare(self) -> None:
        """Проверить доступ, узнать data source и лимит на размер файла."""
        me = await self._request("GET", "/users/me")
        bot_info = me.get("bot") or {}
        limit = bot_info.get("max_file_upload_size_in_bytes")
        if isinstance(limit, int) and limit > 0:
            self.max_upload_bytes = limit

        database = await self._request("GET", f"/databases/{self._database_id}")
        sources = database.get("data_sources") or []
        if sources:
            self.data_source_id = sources[0]["id"]
        else:
            # Совсем старые воркспейсы: база сама себе источник данных.
            self.data_source_id = self._database_id

        log.info(
            "Notion готов: база %s, data source %s, лимит файла %.1f МБ",
            self._database_id,
            self.data_source_id,
            self.max_upload_bytes / 1024 / 1024,
        )

    @property
    def database_title(self) -> str:
        return self._database_id

    # ------------------------------------------------------------------
    # Страницы
    # ------------------------------------------------------------------

    async def find_by_key(self, tg_key: str, property_name: str = "TG key") -> str | None:
        """URL уже сохранённой записи с таким ключом, если она есть."""
        if not self.data_source_id:
            raise RuntimeError("Сначала нужно вызвать prepare()")

        result = await self._request(
            "POST",
            f"/data_sources/{self.data_source_id}/query",
            json={
                "filter": {"property": property_name, "rich_text": {"equals": tg_key}},
                "page_size": 1,
            },
        )
        results = result.get("results") or []
        return results[0].get("url") if results else None

    async def create_page(
        self,
        properties: dict[str, Any],
        children: Sequence[dict] | None = None,
        *,
        icon: str | None = None,
    ) -> tuple[str, str]:
        """Создать страницу в базе. Возвращает (page_id, url)."""
        if not self.data_source_id:
            raise RuntimeError("Сначала нужно вызвать prepare()")

        payload: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
            "properties": properties,
        }
        if children:
            payload["children"] = list(children)
        if icon:
            payload["icon"] = {"type": "emoji", "emoji": icon}

        page = await self._request("POST", "/pages", json=payload)
        return page["id"], page.get("url", "")

    async def set_select(self, page_id: str, property_name: str, value: str | None) -> None:
        """Проставить значение select-свойства уже созданной записи."""
        await self._request(
            "PATCH",
            f"/pages/{page_id}",
            json={"properties": {property_name: select_property(value)}},
        )

    async def append_blocks(self, page_id: str, blocks: Sequence[dict]) -> None:
        """Дописать блоки в конец страницы (по 100 за запрос)."""
        for part in batched(list(blocks)):
            await self._request(
                "PATCH", f"/blocks/{page_id}/children", json={"children": part}
            )

    # ------------------------------------------------------------------
    # Файлы
    # ------------------------------------------------------------------

    def fits_upload_limit(self, size: int) -> bool:
        return 0 < size <= self.max_upload_bytes

    async def upload(self, filename: str, content_type: str, data: bytes) -> Uploaded:
        """Загрузить файл одним куском и вернуть идентификатор загрузки."""
        if len(data) > self.max_upload_bytes:
            raise NotionError(
                400,
                "file_too_large",
                f"{len(data)} байт больше лимита воркспейса {self.max_upload_bytes}",
            )

        safe_name = self._safe_filename(filename)
        created = await self._request(
            "POST",
            "/file_uploads",
            json={
                "mode": "single_part",
                "filename": safe_name,
                "content_type": content_type,
            },
        )

        upload_id = created["id"]
        upload_url = created.get("upload_url") or f"{API}/file_uploads/{upload_id}/send"

        await self._request(
            "POST",
            upload_url,
            files={"file": (safe_name, data, content_type)},
        )
        return Uploaded(id=upload_id, filename=safe_name)

    @staticmethod
    def _safe_filename(name: str, limit: int = 200) -> str:
        """Notion ограничивает имя 900 байтами; заодно чистим разделители пути."""
        cleaned = (name or "file").replace("/", "_").replace("\\", "_").strip()
        cleaned = cleaned or "file"
        encoded = cleaned.encode("utf-8")
        if len(encoded) <= limit:
            return cleaned
        head, _, ext = cleaned.rpartition(".")
        ext = f".{ext}" if head and len(ext) <= 10 else ""
        budget = limit - len(ext.encode("utf-8"))
        trimmed = encoded[:budget].decode("utf-8", errors="ignore")
        return trimmed + ext


# ----------------------------------------------------------------------
# Свойства базы
# ----------------------------------------------------------------------

def title_property(value: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": value[:2000]}}]}


def text_property(value: str | None) -> dict:
    if not value:
        return {"rich_text": []}
    return {"rich_text": [{"type": "text", "text": {"content": value[:2000]}}]}


def url_property(value: str | None) -> dict:
    return {"url": value or None}


def select_property(value: str | None) -> dict:
    return {"select": {"name": value} if value else None}


def date_property(value: Any) -> dict:
    if value is None:
        return {"date": None}
    return {"date": {"start": value.isoformat() if hasattr(value, "isoformat") else value}}


def checkbox_property(value: bool) -> dict:
    return {"checkbox": bool(value)}
