"""Посты из Instagram и Facebook.

yt-dlp здесь всегда подменён: тесты проверяют не скачивание, а то, что
получившийся пост правильно ложится в заметку — подпись, автор, ссылки на
вложения в архиве и честная причина, когда скачать не вышло.
"""
from __future__ import annotations

import pytest

from bot import social
from bot.pipeline import Saver
from tests.test_archive import ARCHIVE, callout_link, callout_text, callouts
from tests.test_pipeline import make_message
from tests.test_saver import FakeBot, FakeNotion, blocks_of, props_of, settings, title_of

REEL = "https://www.instagram.com/reel/Cabc123/"


def post(**overrides) -> social.Post:
    base = dict(
        url=REEL,
        title=None,
        description="Подпись к посту",
        uploader="@someone",
        items=[social.PostItem(data=b"mp4-bytes", filename="001_abc.mp4")],
    )
    base.update(overrides)
    return social.Post(**base)


@pytest.fixture
def fake_post(monkeypatch):
    """Подменить скачивание: тест задаёт готовый результат."""
    calls: list[dict] = []
    holder: dict = {"post": post()}

    async def fake_fetch(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return holder["post"]

    monkeypatch.setattr(social, "fetch_post", fake_fetch)
    return holder, calls


def social_settings(**overrides):
    base = dict(archive_chat_id=ARCHIVE, fetch_articles=True)
    base.update(overrides)
    return settings(**base)


# --------------------------------------------------------------------------
# Какие ссылки считаются постами
# --------------------------------------------------------------------------

HOSTS = social.hosts_from("instagram.com,instagr.am,facebook.com,fb.watch")


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.instagram.com/reel/Cabc/", True),
        ("https://instagram.com/p/Cabc/", True),
        ("https://m.facebook.com/watch/?v=1", True),   # поддомен — свой
        ("https://fb.watch/abc/", True),
        ("https://example.com/post", False),
        ("https://notinstagram.com/p/Cabc/", False),   # не путать с суффиксом
        ("https://habr.com/ru/post/1/", False),
    ],
)
def test_supported_hosts(url, expected):
    assert social.is_supported(url, HOSTS) is expected


def test_no_hosts_configured_disables_everything():
    assert social.is_supported("https://instagram.com/p/C/", social.hosts_from("")) is False


def test_hosts_are_normalised():
    assert social.hosts_from(" WWW.Instagram.com ; fb.watch,") == {"instagram.com", "fb.watch"}


def test_video_is_told_from_a_picture():
    assert social.PostItem(b"", "a.mp4").is_video is True
    assert social.PostItem(b"", "a.JPG").is_video is False


# --------------------------------------------------------------------------
# Метаданные
# --------------------------------------------------------------------------

def test_truncated_caption_is_not_used_as_a_title():
    """У Instagram title — это обрезанная подпись; заголовок соберём сами."""
    described = social._describe(REEL, {
        "title": "Очень длинная подпись...",
        "description": "Очень длинная подпись, целиком и с продолжением",
        "uploader": "@someone",
    })
    assert described.title is None
    assert described.description.startswith("Очень длинная подпись,")


def test_own_title_survives():
    described = social._describe(REEL, {"title": "Название", "description": "Другой текст"})
    assert described.title == "Название"


def test_carousel_metadata_comes_from_the_playlist():
    described = social._describe(REEL, {
        "entries": [{"uploader": "@author", "description": "Подпись"}],
    })
    assert described.uploader == "@author"


@pytest.mark.parametrize(
    "message, expected",
    [
        ("ERROR: login required to view this post", "нужны cookies"),
        ("HTTP Error 429: Too Many Requests", "ограничил запросы"),
        ("ERROR: This video is private", "закрытый"),
        ("ERROR: Unsupported URL: https://x.test/", "не умеет этот тип"),
        ("что-то совсем новое", "не удалось скачать пост"),
    ],
)
def test_errors_are_explained_in_russian(message, expected):
    assert expected in social._human_error(RuntimeError(message))


# --------------------------------------------------------------------------
# Пост в заметке
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reel_is_downloaded_and_linked(fake_post):
    holder, calls = fake_post
    notion = FakeNotion()
    saver = Saver(notion, social_settings())
    bot = FakeBot()

    result = await saver.save(bot, [make_message(7, text=REEL)])

    assert calls[0]["url"] == REEL
    assert bot.forwarded[0] == {
        "chat_id": ARCHIVE, "how": "video", "filename": "001_abc.mp4",
        "size": len(b"mp4-bytes"), "caption": f"@someone · {REEL}",
    }
    card = [b for b in callouts(notion) if callout_link(b)][0]
    assert callout_link(card) == "https://t.me/c/1234567890/1001"
    assert props_of(notion)["Тип"]["select"]["name"] == "Видео"
    assert title_of(notion) == "Подпись к посту"
    assert result.notes == []


@pytest.mark.asyncio
async def test_caption_and_author_land_in_the_page(fake_post):
    notion = FakeNotion()
    saver = Saver(notion, social_settings())

    await saver.save(FakeBot(), [make_message(7, text=REEL)])

    texts = [callout_text(b) for b in callouts(notion)]
    assert any("Автор: @someone" in t for t in texts)
    paragraphs = [
        "".join(i["text"]["content"] for i in b["paragraph"]["rich_text"])
        for b in blocks_of(notion) if b["type"] == "paragraph"
    ]
    assert "Подпись к посту" in paragraphs


@pytest.mark.asyncio
async def test_carousel_becomes_an_album_with_a_link_per_item(fake_post):
    holder, _ = fake_post
    holder["post"] = post(items=[
        social.PostItem(data=b"one", filename="001_a.jpg"),
        social.PostItem(data=b"two", filename="002_b.mp4"),
    ])
    notion = FakeNotion()
    saver = Saver(notion, social_settings())
    bot = FakeBot()

    await saver.save(bot, [make_message(7, text=REEL)])

    assert [s["how"] for s in bot.forwarded] == ["document", "video"]
    assert props_of(notion)["Тип"]["select"]["name"] == "Альбом"
    links = [callout_link(b) for b in callouts(notion) if callout_link(b)]
    assert len(links) == 2


@pytest.mark.asyncio
async def test_trafilatura_is_not_called_for_social_links(fake_post, monkeypatch):
    """Instagram всё равно ничего не отдаёт — незачем ходить туда дважды."""
    import bot.pipeline as pipeline_module

    async def explode(*args, **kwargs):
        raise AssertionError("статью по ссылке на пост тянуть не надо")

    monkeypatch.setattr(pipeline_module, "fetch_article", explode)
    saver = Saver(FakeNotion(), social_settings(fetch_articles=True))

    await saver.save(FakeBot(), [make_message(7, text=REEL)])


@pytest.mark.asyncio
async def test_failed_download_keeps_the_link_and_explains_why(fake_post):
    holder, _ = fake_post
    holder["post"] = post(items=[], error="нужны cookies: сайт требует авторизацию")
    notion = FakeNotion()
    saver = Saver(notion, social_settings())
    bot = FakeBot()

    result = await saver.save(bot, [make_message(7, text=REEL)])

    assert notion.created, "страница должна быть создана всё равно"
    assert bot.forwarded == []
    assert props_of(notion)["Ссылка"]["url"] == REEL
    assert any(b["type"] == "bookmark" for b in blocks_of(notion))
    assert any("нужны cookies" in note for note in result.notes)


@pytest.mark.asyncio
async def test_skipped_oversized_attachment_is_reported(fake_post):
    holder, _ = fake_post
    holder["post"] = post(skipped=2)
    notion = FakeNotion()
    saver = Saver(notion, social_settings())

    result = await saver.save(FakeBot(), [make_message(7, text=REEL)])

    assert any("пропущено по размеру: 2" in note for note in result.notes)


@pytest.mark.asyncio
async def test_download_can_be_switched_off(fake_post):
    _, calls = fake_post
    saver = Saver(FakeNotion(), social_settings(social_download=False, fetch_articles=False))

    await saver.save(FakeBot(), [make_message(7, text=REEL)])

    assert calls == [], "выключено — значит, не ходим никуда"


@pytest.mark.asyncio
async def test_settings_reach_yt_dlp(fake_post):
    _, calls = fake_post
    saver = Saver(FakeNotion(), social_settings(
        social_cookies_file="/etc/secrets/cookies.txt",
        social_max_items=3,
        social_timeout=15.0,
    ))

    await saver.save(FakeBot(), [make_message(7, text=REEL)])

    assert calls[0]["cookies_file"] == "/etc/secrets/cookies.txt"
    assert calls[0]["max_items"] == 3
    assert calls[0]["timeout"] == 15.0
