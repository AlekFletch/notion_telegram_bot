"""Извлечение содержимого статьи по ссылке из сообщения.

Скачиваем страницу сами (httpx), разбираем через trafilatura в отдельном
потоке — разбор HTML это CPU-работа, в event loop ей не место.
Любая ошибка здесь не должна ронять сохранение сообщения: не получилось
достать текст — сохраняем то, что есть, и идём дальше.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import urlparse

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
        article.description = _clean(metadata.description)
        article.site = _clean(metadata.sitename)
        article.author = _clean(metadata.author)

    # Заголовок берём из мета-тегов и <title>, а не у trafilatura: она
    # собирает его из вёрстки заголовка страницы и склеивает слова,
    # разнесённые по разным элементам («agent» + «its» → «agentits»).
    # Имя сайта отрезаем у любого источника: оно одинаково лезет и в og:title,
    # и в <title>, а в списке Notion «… / Хабр» у каждой записи только мешает.
    article.title = _strip_site_name(
        _meta_title(html)
        or _document_title(html)
        or _clean(metadata.title if metadata else None),
        article.site,
        url,
    )

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


#: Разделители, которыми сайты отбивают своё имя от заголовка страницы.
_SEPARATORS = ("·", "|", "—", "–", "::", "•", "»", "/", "-")

_META_TITLE_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](?:og:title|twitter:title)[\"'][^>]*?"
    r"content=[\"'](?P<value>[^\"']*)[\"']"
    r"|<meta[^>]+content=[\"'](?P<value2>[^\"']*)[\"'][^>]*?"
    r"(?:property|name)=[\"'](?:og:title|twitter:title)[\"']",
    re.IGNORECASE,
)

_DOC_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _meta_title(html_text: str) -> str | None:
    """Заголовок из og:title или twitter:title — самый надёжный источник."""
    match = _META_TITLE_RE.search(html_text)
    if not match:
        return None
    raw = match.group("value") or match.group("value2")
    return _clean(unescape(raw or ""))


def _document_title(html_text: str) -> str | None:
    match = _DOC_TITLE_RE.search(html_text)
    return _clean(unescape(match.group(1))) if match else None


def _site_aliases(site: str | None, url: str | None) -> set[str]:
    """Как сайт может называть себя в заголовке: og:site_name и домен.

    og:site_name есть далеко не везде, а «obscura.sh · Заголовок» и
    «Obscura · Заголовок» встречаются одинаково часто.
    """
    aliases = {site.strip().lower()} if site else set()

    if url:
        host = urlparse(url).netloc.split(":")[0].lower()
        host = host[4:] if host.startswith("www.") else host
        if host:
            aliases.add(host)
            aliases.add(host.split(".")[0])

    return {alias for alias in aliases if alias}


def _strip_site_name(title: str | None, site: str | None, url: str | None = None) -> str | None:
    """Убрать имя сайта из «Obscura · Give every agent its own browser».

    Отрезаем только точное совпадение с именем сайта — иначе легко покалечить
    нормальный заголовок, в котором есть тире или вертикальная черта.
    """
    aliases = _site_aliases(site, url)
    if not title or not aliases:
        return title

    for separator in _SEPARATORS:
        parts = [part.strip() for part in title.split(f" {separator} ")]
        if len(parts) < 2:
            continue
        kept = [part for part in parts if part.lower() not in aliases]
        if kept and len(kept) < len(parts):
            return f" {separator} ".join(kept)
    return title


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
