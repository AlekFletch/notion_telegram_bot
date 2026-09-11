"""Посты из соцсетей: скачивание вложений через yt-dlp.

Зачем отдельно от [article.py](article.py): Instagram и Facebook анонимному
запросу не отдают ничего. Instagram возвращает 200 и пустую JS-оболочку без
единого мета-тега — даже у существующей страницы, так что trafilatura на таких
ссылках бессмысленна. Достать содержимое поста умеет только yt-dlp, и для
Instagram ему почти всегда нужны cookies живого аккаунта.

Как и везде в проекте: любая ошибка здесь не роняет сохранение. Не удалось
скачать — сохраняем ссылку и пишем в заметке почему.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: Telegram разрешает боту отправлять файлы не больше 50 МБ.
BOT_API_UPLOAD_LIMIT = 50 * 1024 * 1024

#: Расширения, которые отправляем как видео, а не как файл.
_VIDEO_EXT = frozenset({".mp4", ".mov", ".webm", ".mkv", ".m4v"})

#: Мусорные файлы, которые yt-dlp кладёт рядом с самим вложением.
_JUNK_EXT = frozenset({".json", ".jpg", ".jpeg", ".webp", ".png", ".vtt", ".srt", ".part", ".ytdl"})


@dataclass
class PostItem:
    """Одно вложение поста, уже прочитанное в память."""

    data: bytes
    filename: str

    @property
    def is_video(self) -> bool:
        return Path(self.filename).suffix.lower() in _VIDEO_EXT


@dataclass
class Post:
    """Что удалось вытащить из поста."""

    url: str
    title: str | None = None
    description: str | None = None
    uploader: str | None = None
    items: list[PostItem] = field(default_factory=list)
    skipped: int = 0           # вложений, которые не влезли в лимит
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.items)


def hosts_from(raw: str) -> frozenset[str]:
    """Разобрать список доменов из настройки."""
    parts = (chunk.strip().lower() for chunk in raw.replace(";", ",").split(","))
    return frozenset(part.removeprefix("www.") for part in parts if part)


def is_supported(url: str, hosts: frozenset[str]) -> bool:
    """Ссылка ведёт на сайт, который мы качаем через yt-dlp?"""
    if not hosts:
        return False

    host = urlparse(url).netloc.split(":")[0].lower()
    host = host.removeprefix("www.")
    if not host:
        return False

    # Поддомены тоже свои: m.facebook.com попадает под facebook.com.
    return any(host == known or host.endswith(f".{known}") for known in hosts)


async def fetch_post(
    url: str,
    *,
    cookies_file: str = "",
    max_bytes: int = BOT_API_UPLOAD_LIMIT,
    max_items: int = 10,
    timeout: float = 30.0,
) -> Post:
    """Скачать вложения поста. Сеть и разбор — в отдельном потоке."""
    try:
        return await asyncio.to_thread(
            _download, url, cookies_file, max_bytes, max_items, timeout
        )
    except Exception as exc:  # noqa: BLE001 — причина уходит в заметку, не в лог-шум
        log.info("Пост %s не скачался: %s", url, exc)
        return Post(url=url, error=_human_error(exc))


# --------------------------------------------------------------------------


class _Logger:
    """yt-dlp пишет ошибки прямо в stderr мимо logging — заворачиваем к себе.

    Иначе логи Render забиваются его многострочными советами про cookies,
    а причина всё равно приезжает пользователю в заметке.
    """

    def debug(self, message: str) -> None:
        log.debug("yt-dlp: %s", message)

    info = debug
    warning = debug

    def error(self, message: str) -> None:
        log.info("yt-dlp: %s", message)


def _options(folder: Path, cookies_file: str, max_bytes: int, max_items: int, timeout: float) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _Logger(),
        "outtmpl": str(folder / "%(playlist_index|0)03d_%(id)s.%(ext)s"),
        "playlistend": max_items,
        "max_filesize": max_bytes,
        "socket_timeout": timeout,
        "retries": 2,
        "fragment_retries": 2,
        # Карусель Instagram приезжает плейлистом — он нам как раз нужен,
        # а вот «скачать всю ленту автора» по ссылке на профиль — нет.
        "playlist_items": f"1:{max_items}",
    }
    if cookies_file and Path(cookies_file).exists():
        options["cookiefile"] = cookies_file
    elif cookies_file:
        log.warning("Файл cookies не найден: %s", cookies_file)
    return options


def _download(url: str, cookies_file: str, max_bytes: int, max_items: int, timeout: float) -> Post:
    from yt_dlp import YoutubeDL  # импорт внутри: тяжёлый, нужен не в каждом запуске

    with tempfile.TemporaryDirectory(prefix="post_") as folder_name:
        folder = Path(folder_name)
        options = _options(folder, cookies_file, max_bytes, max_items, timeout)

        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)

        post = _describe(url, info)
        files = sorted(
            path for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() not in _JUNK_EXT
        )

        for path in files[:max_items]:
            size = path.stat().st_size
            if size > max_bytes or size == 0:
                post.skipped += 1
                continue
            post.items.append(PostItem(data=path.read_bytes(), filename=path.name))

    if not post.items and not post.error:
        post.error = (
            "во вложении нет файла, который поместился бы в 50 МБ"
            if post.skipped else "yt-dlp не нашёл в посте вложений"
        )
    return post


def _describe(url: str, info: dict | None) -> Post:
    """Метаданные поста. У карусели они лежат на уровне плейлиста."""
    info = info or {}
    entries = info.get("entries") or []
    first = entries[0] if entries and isinstance(entries[0], dict) else info

    def pick(key: str) -> str | None:
        return _clean(info.get(key) or first.get(key))

    description = pick("description")
    title = pick("title")

    # У Instagram заголовок — это обрезанная подпись, часто с многоточием.
    # Если подпись есть целиком, заголовок из неё соберёт build_title.
    if title and description and description.startswith(title.rstrip(" .…")):
        title = None

    return Post(
        url=pick("webpage_url") or url,
        title=title,
        description=description,
        uploader=pick("uploader") or pick("channel") or pick("uploader_id"),
    )


def _clean(value) -> str | None:
    if not value:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _human_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "login" in text or "cookies" in text or "authenticat" in text or "empty media response" in text:
        return "нужны cookies: сайт требует авторизацию"
    if "rate-limit" in text or "429" in text or "too many" in text:
        return "сайт временно ограничил запросы — попробуй позже"
    if "private" in text:
        return "пост закрытый"
    if "unavailable" in text or "not exist" in text or "404" in text:
        return "пост недоступен или удалён"
    if "unsupported url" in text:
        return "yt-dlp не умеет этот тип ссылки"
    if "timed out" in text or "timeout" in text:
        return "сайт не ответил вовремя"
    return "не удалось скачать пост"
