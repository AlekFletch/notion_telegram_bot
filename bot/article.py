"""Извлечение содержимого статьи по ссылке из сообщения.

Скачиваем страницу сами (httpx), разбираем через trafilatura в отдельном
потоке — разбор HTML это CPU-работа, в event loop ей не место.
Любая ошибка здесь не должна ронять сохранение сообщения: не получилось
достать текст — сохраняем то, что есть, и идём дальше.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

#: Больше этого не читаем — защита от случайной ссылки на ISO-образ.
_MAX_HTML_BYTES = 4 * 1024 * 1024


@dataclass
class Article:
    url: str
    title: str | None = None
    description: str | None = None
    site: str | None = None
    author: str | None = None
    text: str | None = None
    error: str | None = None

    @property
    def has_text(self) -> bool:
        return bool(self.text and self.text.strip())


async def fetch_article(
    url: str,
    *,
    timeout: float = 10.0,
    max_chars: int = 40_000,
) -> Article:
    """Скачать страницу и вытащить заголовок, описание и текст статьи."""
    try:
        html = await _download(url, timeout)
    except Exception as exc:  # noqa: BLE001 — причина уходит в карточку, не в лог-шум
        log.info("Не удалось скачать %s: %s", url, exc)
        return Article(url=url, error=_human_error(exc))

    if html is None:
        return Article(url=url, error="по ссылке не HTML-страница")

    try:
        return await asyncio.to_thread(_extract, url, html, max_chars)
    except Exception as exc:  # noqa: BLE001
        log.info("Не удалось разобрать %s: %s", url, exc)
        return Article(url=url, error="не удалось разобрать страницу")


async def _download(url: str, timeout: float) -> str | None:
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru,en;q=0.9",
    }
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "html" not in content_type.lower():
                return None

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= _MAX_HTML_BYTES:
                    break

            encoding = response.encoding or "utf-8"
            return b"".join(chunks).decode(encoding, errors="replace")


def _extract(url: str, html: str, max_chars: int) -> Article:
    import trafilatura  # импорт внутри: тяжёлый, нужен не в каждом запуске

    article = Article(url=url)

    metadata = trafilatura.extract_metadata(html, default_url=url)
    if metadata is not None:
        article.title = _clean(metadata.title)
        article.description = _clean(metadata.description)
        article.site = _clean(metadata.sitename)
        article.author = _clean(metadata.author)

    text = trafilatura.extract(
        html,
        url=url,
        output_format="txt",
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    if text:
        text = text.strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n\n[…] Текст обрезан по лимиту."
        article.text = text
    else:
        article.error = "страница не отдала текст (пейволл, JS-рендеринг или защита от ботов)"

    return article


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned or None


def _human_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"сайт ответил {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "сайт не ответил вовремя"
    if isinstance(exc, httpx.HTTPError):
        return "сайт недоступен"
    return "не удалось загрузить страницу"
