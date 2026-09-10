"""Тесты выбора категории кнопками под сохранённой записью."""
from __future__ import annotations

import pytest

from bot import categories
from bot.categories import CATEGORIES
from bot.handlers import CALLBACK_PREFIX, _keyboard, _url_from

NOTION_URL = "https://notion.so/page-id"
PAGE_ID = "3d7de3e0016c8105930eca28cf5e065c"


def all_buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def category_buttons(markup):
    return [b for b in all_buttons(markup) if b.callback_data]


# --------------------------------------------------------------------------
# Список категорий
# --------------------------------------------------------------------------

def test_category_names_are_unique():
    names = [c.name for c in CATEGORIES]
    assert len(names) == len(set(names))


def test_lookup_by_index_and_name_agree():
    for index, category in enumerate(CATEGORIES):
        assert categories.by_index(index) is category
        assert categories.index_of(category.name) == index


@pytest.mark.parametrize("index", [-1, len(CATEGORIES), 999])
def test_out_of_range_index_returns_none(index):
    assert categories.by_index(index) is None


def test_unknown_name_returns_none():
    assert categories.index_of("Такой категории нет") is None


# --------------------------------------------------------------------------
# Клавиатура
# --------------------------------------------------------------------------

def test_keyboard_has_button_per_category_plus_link():
    markup = _keyboard(NOTION_URL, PAGE_ID)

    assert len(category_buttons(markup)) == len(CATEGORIES)
    assert markup.inline_keyboard[-1][0].url == NOTION_URL


def test_callback_data_fits_telegram_limit():
    """Telegram обрубает callback_data длиннее 64 байт — молча."""
    for button in category_buttons(_keyboard(NOTION_URL, PAGE_ID)):
        assert len(button.callback_data.encode()) <= 64


def test_callback_data_carries_index_and_page():
    buttons = category_buttons(_keyboard(NOTION_URL, PAGE_ID))
    prefix, index, page = buttons[3].callback_data.split(":", 2)

    assert prefix == CALLBACK_PREFIX
    assert int(index) == 3
    assert page == PAGE_ID


def test_page_id_with_dashes_is_normalised():
    dashed = "3d7de3e0-016c-8105-930e-ca28cf5e065c"
    button = category_buttons(_keyboard(NOTION_URL, dashed))[0]
    assert button.callback_data.split(":", 2)[2] == PAGE_ID


def test_chosen_category_is_marked():
    markup = _keyboard(NOTION_URL, PAGE_ID, chosen=2)
    marked = [b for b in category_buttons(markup) if b.text.startswith("✓")]

    assert len(marked) == 1
    assert marked[0].text == "✓ " + CATEGORIES[2].name


def test_rows_are_not_wider_than_two_buttons():
    for row in _keyboard(NOTION_URL, PAGE_ID).inline_keyboard:
        assert len(row) <= 2


def test_without_page_id_only_link_remains():
    """У дубля своей страницы нет — категории показывать нечему."""
    markup = _keyboard(NOTION_URL)
    assert category_buttons(markup) == []
    assert len(all_buttons(markup)) == 1


def test_no_url_and_no_page_gives_no_keyboard():
    assert _keyboard("") is None


# --------------------------------------------------------------------------
# Восстановление ссылки из клавиатуры
# --------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, markup):
        self.reply_markup = markup


def test_url_is_recovered_from_existing_keyboard():
    """При перерисовке клавиатуры ссылку берём из неё же, а не из базы."""
    markup = _keyboard(NOTION_URL, PAGE_ID)
    assert _url_from(FakeMessage(markup)) == NOTION_URL


def test_url_recovery_tolerates_missing_markup():
    assert _url_from(None) == ""
    assert _url_from(FakeMessage(None)) == ""


# --------------------------------------------------------------------------
# Одноразовые отправки
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_retry_limits_to_single_attempt():
    """Служебное «Сохраняю…» не должно догоняться повторами."""
    import asyncio

    from aiogram.exceptions import TelegramNetworkError

    from bot.session import RetryingSession, no_retry

    calls = 0

    class Counting(RetryingSession):
        async def make_request(self, bot, method, timeout=None):
            nonlocal calls

            async def boom(*_a, **_kw):
                nonlocal calls
                calls += 1
                raise TelegramNetworkError(method=None, message="нет связи")

            original = RetryingSession.__mro__[1].make_request
            RetryingSession.__mro__[1].make_request = boom
            try:
                return await super().make_request(bot, method, timeout=timeout)
            finally:
                RetryingSession.__mro__[1].make_request = original

    session = Counting(timeout=5.0, attempts=4)

    class Method:
        pass

    # Внутри no_retry — ровно одна попытка.
    calls = 0
    with pytest.raises(TelegramNetworkError):
        with no_retry():
            await session.make_request(None, Method())
    assert calls == 1

    # Снаружи — полный набор повторов.
    calls = 0
    with pytest.raises(TelegramNetworkError):
        await session.make_request(None, Method())
    assert calls == 4

    await session.close()


@pytest.mark.asyncio
async def test_no_retry_flag_does_not_leak_between_tasks():
    """Флаг живёт в своей задаче и не влияет на соседние отправки."""
    import asyncio

    from bot.session import _single_attempt, no_retry

    seen: list[bool] = []

    async def neighbour():
        await asyncio.sleep(0.01)
        seen.append(_single_attempt.get())

    async def with_flag():
        with no_retry():
            await asyncio.sleep(0.02)
            seen.append(_single_attempt.get())

    await asyncio.gather(asyncio.create_task(neighbour()), asyncio.create_task(with_flag()))

    assert seen == [False, True]


def test_session_timeout_stays_a_number():
    """aiogram считает int(session.timeout + polling_timeout).

    Если подменить это поле объектом ClientTimeout, опрос падает на первом
    же цикле с TypeError — поймали ровно это.
    """
    from bot.session import RetryingSession

    session = RetryingSession(timeout=90.0)

    assert isinstance(session.timeout, (int, float))
    assert int(session.timeout + 30) == 120


def test_connect_phase_is_bounded_separately():
    """Общий лимит щедрый (загрузка файлов), подключение — короткое."""
    from bot.session import RetryingSession

    session = RetryingSession(timeout=90.0, connect_timeout=10.0)

    deadline = session._deadline(None)
    assert deadline.total == 90.0
    assert deadline.connect == 10.0
    assert deadline.sock_connect == 10.0

    # Явный таймаут запроса (его передаёт опрос) меняет общий лимит,
    # но подключение остаётся ограниченным.
    polling = session._deadline(120)
    assert polling.total == 120.0
    assert polling.connect == 10.0
