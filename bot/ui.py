"""Shared, safe rendering primitives for Telegram conversation screens."""
from __future__ import annotations

import base64
import html
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from .navigation import back_callback

MAX_SCREEN_UNITS = 500
MAX_CALLBACK_UNITS = 200
_RICH_MARKER = re.compile(
    r"\x00(?:CODE:(?P<code>[A-Za-z0-9_-]*)"
    r"|LINK:(?P<user_id>\d+):(?P<label>[A-Za-z0-9_-]*)"
    r"|URL:(?P<url>[A-Za-z0-9_-]*):(?P<url_label>[A-Za-z0-9_-]*))\x00"
)
_STATUS_PREFIX = re.compile(r"^(?:✅|❌|⚠️|ℹ️|⏳)\s*")
_NAVIGATION_PREFIXES = ("⬅️", "✖️", "✅", "➡️", "↩️", "🏠")
_SINGLE_ACTION_PREFIXES = ("⏰", "📝", "➕")
_MENU_CLOSED_TEXT = "📋 Предыдущее меню закрыто."
_MESSAGE_DELETE_AGE_SECONDS = 48 * 60 * 60


def code(value: int | str | None) -> str:
    """Mark a value for safe display as an HTML code span."""
    if value is None:
        return "⚪ не задано"
    return f"\x00CODE:{_encode_marker_value(str(value))}\x00"


def _encode_marker_value(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_marker_value(encoded: str) -> str:
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode(
        "utf-8"
    )


def _decode_marker(match: re.Match) -> tuple[str, str]:
    """Return a marker's kind and visible text; malformed ones stay literal."""
    is_code = match.group("code") is not None
    try:
        value = _decode_marker_value(
            match.group("code") if is_code else
            match.group("url_label") if match.group("url") is not None
            else match.group("label")
        )
    except ValueError:
        return "text", match.group(0).replace("\x00", "")
    return ("code" if is_code else "url" if match.group("url") is not None else "link"), value


def _escape_rich_text(text: str) -> str:
    pieces = []
    cursor = 0
    for match in _RICH_MARKER.finditer(text):
        pieces.append(html.escape(text[cursor:match.start()]))
        kind, value = _decode_marker(match)
        if kind == "code":
            pieces.append(f"<code>{html.escape(value)}</code>")
        elif kind == "link":
            pieces.append(
                f'<a href="tg://user?id={match.group("user_id")}">'
                f"{html.escape(value)}</a>"
            )
        elif kind == "url":
            try:
                url = _decode_marker_value(match.group("url"))
            except ValueError:
                url = ""
            if _valid_telegram_url(url):
                pieces.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(value)}</a>')
            else:
                pieces.append(html.escape(value))
        else:
            pieces.append(html.escape(value))
        cursor = match.end()
    pieces.append(html.escape(text[cursor:]))
    return "".join(pieces)



def plain_text(text: str) -> str:
    """Replace rich-text markers with their visible text for parse_mode=None."""
    return _RICH_MARKER.sub(lambda match: _decode_marker(match)[1], text)


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def visible_units(text: str) -> int:
    """Measure text as Telegram shows it, with rich-text markers decoded."""
    return _units(plain_text(text))


def _line_tokens(line: str) -> list[str]:
    """Split a line into characters and whole rich-text markers."""
    tokens = []
    cursor = 0
    for match in _RICH_MARKER.finditer(line):
        tokens.extend(line[cursor:match.start()])
        tokens.append(match.group(0))
        cursor = match.end()
    tokens.extend(line[cursor:])
    return tokens


def _split_long_line(line: str, limit: int) -> list[str]:
    result = []
    current = []
    current_units = 0
    for token in _line_tokens(line):
        # Ссылка с подписью длиннее экрана не помещается целиком:
        # показываем её обычным текстом, разбитым по лимиту.
        pieces = [token]
        if len(token) > 1 and visible_units(token) > limit:
            pieces = list(plain_text(token))
        for piece in pieces:
            size = visible_units(piece)
            if current and current_units + size > limit:
                result.append("".join(current))
                current = []
                current_units = 0
            current.append(piece)
            current_units += size
    if current:
        result.append("".join(current))
    return result or [""]


def user_link(telegram_id: int | str | None, label: str) -> str:
    """Mark a label as a Telegram mention; invalid IDs leave plain text."""
    user_id = str(telegram_id or "").strip()
    if not user_id.isdecimal() or int(user_id) <= 0:
        return label
    return f"\x00LINK:{int(user_id)}:{_encode_marker_value(label)}\x00"


def _valid_telegram_url(url: str) -> bool:
    return re.fullmatch(r"https://t\.me/[A-Za-z0-9_+/-]+", url) is not None


def telegram_link(url: str, label: str) -> str:
    """A safe Telegram chat link, rendered by the same pipeline as mentions."""
    if not _valid_telegram_url(url):
        return label
    return f"\x00URL:{_encode_marker_value(url)}:{_encode_marker_value(label)}\x00"


def screen_text_chunks(text: str, limit: int = MAX_SCREEN_UNITS) -> list[str]:
    """Split at line boundaries without breaking rich-text markers."""
    if limit < 32:
        raise ValueError("Лимит экрана должен быть не меньше 32 символов.")
    source = str(text).strip()
    if not source:
        source = "ℹ️ Продолжайте диалог"

    lines = source.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    current_units = 0
    for line in lines:
        for segment in _split_long_line(line, limit):
            extra = visible_units(segment) + (1 if current else 0)
            if current and current_units + extra > limit:
                chunks.append("\n".join(current))
                current = []
                current_units = 0
                extra = visible_units(segment)
            if not current and not segment.strip():
                # Пустая строка в начале части — без пустого заголовка.
                continue
            current.append(segment)
            current_units += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


def render_screen_chunks(text: str, limit: int = MAX_SCREEN_UNITS) -> list[str]:
    """Escape plain dialog text, emphasize its first line, and cap each screen."""
    rendered = []
    for chunk in screen_text_chunks(text, limit):
        title, separator, body = chunk.partition("\n")
        title_html = f"<b>{_escape_rich_text(title)}</b>"
        rendered.append(
            title_html
            + (f"\n{_escape_rich_text(body)}" if separator else "")
        )
    return rendered


def callback_text(text: str | None) -> str | None:
    """Normalize Telegram toast text to concise, plain, emoji-free text."""
    if text is None:
        return None
    normalized = _STATUS_PREFIX.sub("", str(text)).strip()
    if not normalized:
        return None
    shortened = []
    units = 0
    for character in normalized:
        size = _units(character)
        if units + size > MAX_CALLBACK_UNITS:
            break
        shortened.append(character)
        units += size
    return "".join(shortened)


def validate_inline_keyboard(
    rows: list[list[object]], *, max_buttons: int = 6
) -> None:
    """Raise when a screen's inline keyboard exceeds Telegram design limits."""
    if max_buttons <= 0:
        raise ValueError("Лимит кнопок на экране должен быть положительным.")
    if any(len(row) > 3 for row in rows):
        raise ValueError("В одном ряду допускается не более 3 кнопок.")
    if sum(map(len, rows)) > max_buttons:
        raise ValueError(
            f"На одном экране допускается не более {max_buttons} кнопок."
        )
    for row in rows:
        for button in row:
            callback_data = getattr(button, "callback_data", None)
            if (
                callback_data is not None
                and len(callback_data.encode("utf-8")) > 64
            ):
                raise ValueError("callback_data не может превышать 64 байта.")


def nav_row(
    back: InlineKeyboardButton | None = None,
    save: InlineKeyboardButton | None = None,
    cancel: InlineKeyboardButton | None = None,
) -> list[InlineKeyboardButton]:
    """Build a navigation row from whichever navigation buttons are present."""
    return [button for button in (back, save, cancel) if button is not None]


def _already_gone(exc: TelegramAPIError) -> bool:
    """Сообщения уже нет (удалено раньше) — чистить больше нечего."""
    text = str(exc).lower()
    return "message to delete not found" in text or "message_id_invalid" in text


async def clean_previous_menu(
    message: Message | None,
    debug: logging.Logger,
    *,
    fallback_text: str = _MENU_CLOSED_TEXT,
) -> None:
    """Delete an old wizard menu, or neutralize it when deletion is unavailable."""
    if message is None:
        return
    message_date = getattr(message, "date", None)
    is_too_old = False
    if isinstance(message_date, datetime):
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)
        is_too_old = (
            datetime.now(timezone.utc) - message_date
        ).total_seconds() >= _MESSAGE_DELETE_AGE_SECONDS
    if not is_too_old:
        delete = getattr(message, "delete", None)
        if callable(delete):
            try:
                await delete()
            except TelegramAPIError as exc:
                if _already_gone(exc):
                    debug.debug(
                        "Предыдущее меню %s уже удалено",
                        getattr(message, "message_id", None),
                    )
                    return
                debug.debug(
                    "Не удалось удалить предыдущее меню %s; пробую очистить его",
                    getattr(message, "message_id", None),
                    exc_info=True,
                )
            else:
                debug.debug(
                    "Удалено предыдущее меню: %s",
                    getattr(message, "message_id", None),
                )
                return
        else:
            debug.warning("У сообщения отсутствует метод удаления меню")
    edit_text = getattr(message, "edit_text", None)
    if not callable(edit_text):
        debug.warning("У сообщения отсутствует метод очистки меню")
        return
    try:
        await edit_text(fallback_text, reply_markup=None)
    except TelegramAPIError:
        debug.debug(
            "Не удалось удалить или заменить предыдущее меню %s",
            getattr(message, "message_id", None),
            exc_info=True,
        )
    else:
        debug.debug(
            "Предыдущее меню %s заменено нейтральным текстом",
            getattr(message, "message_id", None),
        )


async def clean_previous_menu_by_id(
    bot: Bot,
    chat_id: int | None,
    message_id: int | None,
    debug: logging.Logger,
    *,
    fallback_text: str = _MENU_CLOSED_TEXT,
) -> None:
    """Delete a saved wizard menu by its identifiers, with an edit fallback."""
    if chat_id is None or message_id is None:
        return
    delete = getattr(bot, "delete_message", None)
    if callable(delete):
        try:
            await delete(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError as exc:
            if _already_gone(exc):
                debug.debug("Предыдущее меню %s уже удалено", message_id)
                return
            debug.debug(
                "Не удалось удалить предыдущее меню %s; пробую очистить его",
                message_id,
                exc_info=True,
            )
        else:
            debug.debug("Удалено предыдущее меню: %s", message_id)
            return
    else:
        debug.warning("У бота отсутствует метод удаления меню %s", message_id)
    edit = getattr(bot, "edit_message_text", None)
    if not callable(edit):
        debug.warning("У бота отсутствует метод очистки меню %s", message_id)
        return
    try:
        await edit(
            fallback_text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )
    except TelegramAPIError:
        debug.debug(
            "Не удалось удалить или заменить предыдущее меню %s",
            message_id,
            exc_info=True,
        )
    else:
        debug.debug(
            "Предыдущее меню %s заменено нейтральным текстом", message_id
        )


def pair_back_help(
    rows: list[list[InlineKeyboardButton]],
) -> list[list[InlineKeyboardButton]]:
    """Keep the real return action and help together below all other actions."""
    buttons = [button for row in rows for button in row]
    back_data = back_callback(InlineKeyboardMarkup(inline_keyboard=rows))
    back = next(
        (button for button in reversed(buttons)
         if back_data and button.callback_data == back_data),
        None,
    )
    help_button = next(
        (button for button in buttons
         if button.text.startswith(("❓ Справка", "❓ Помощь"))),
        None,
    )
    if back is None or help_button is None:
        return rows
    result = [
        [button for button in row if button is not back and button is not help_button]
        for row in rows
    ]
    return [row for row in result if row] + [[back, help_button]]


def layout_buttons(
    buttons: list[InlineKeyboardButton],
    *,
    max_row_width: int = 40,
    max_row_buttons: int = 3,
    short_button: int = 20,
    very_short_button: int = 15,
) -> list[list[InlineKeyboardButton]]:
    """Arrange flat inline buttons into compact rows, keeping navigation last."""
    if (
        max_row_width <= 0
        or max_row_buttons <= 0
        or short_button < 0
        or very_short_button < 0
    ):
        raise ValueError("Параметры компоновки кнопок должны быть положительными.")

    def is_navigation(button: InlineKeyboardButton) -> bool:
        return button.text.startswith(_NAVIGATION_PREFIXES) or button.text.endswith(
            "➡️"
        )

    navigation = [button for button in buttons if is_navigation(button)]
    regular = [button for button in buttons if not is_navigation(button)]
    rows: list[list[InlineKeyboardButton]] = []
    group: list[InlineKeyboardButton] = []

    def flush_group() -> None:
        if not group:
            return
        max_buttons = (
            max_row_buttons
            if all(len(button.text) <= very_short_button for button in group)
            else min(2, max_row_buttons)
        )
        row_count = (len(group) + max_buttons - 1) // max_buttons

        def balanced_partitions(
            remaining: int, parts: int, prefix: tuple[int, ...] = ()
        ) -> list[tuple[int, ...]]:
            if parts == 0:
                return [prefix] if remaining == 0 else []
            min_size = max(1, remaining - max_buttons * (parts - 1))
            max_size = min(max_buttons, remaining - (parts - 1))
            return [
                partition
                for size in range(max_size, min_size - 1, -1)
                for partition in balanced_partitions(
                    remaining - size, parts - 1, (*prefix, size)
                )
            ]

        while True:
            partitions = balanced_partitions(len(group), row_count)
            partitions.sort(
                key=lambda sizes: (
                    max(sizes) - min(sizes),
                    tuple(-size for size in sizes),
                )
            )
            for sizes in partitions:
                candidate_rows = []
                offset = 0
                for size in sizes:
                    row = group[offset:offset + size]
                    offset += size
                    row_width = sum(len(button.text) for button in row) + (
                        2 * (len(row) - 1)
                    )
                    if size > 1 and row_width > max_row_width:
                        break
                    candidate_rows.append(row)
                else:
                    rows.extend(candidate_rows)
                    group.clear()
                    return
            row_count += 1

    for button in regular:
        if (
            len(button.text) > short_button
            or button.text.startswith(_SINGLE_ACTION_PREFIXES)
        ):
            flush_group()
            rows.append([button])
        else:
            group.append(button)
    flush_group()

    if len(navigation) in {2, 3} and max_row_buttons >= len(navigation):
        if all(len(button.text) <= 24 for button in navigation):
            rows.append(navigation)
        else:
            rows.extend([[button] for button in navigation])
    else:
        rows.extend([[button] for button in navigation])
    return pair_back_help(rows)


async def send_telegram_screen(
    send_message: Callable[..., Awaitable[Any]],
    chat_id: int,
    text: str,
    limit: int = MAX_SCREEN_UNITS,
    **kwargs,
) -> Any:
    """Send a safe HTML screen, splitting long text at Telegram UI boundaries."""
    kwargs.pop("parse_mode", None)
    kwargs.setdefault("link_preview_options", LinkPreviewOptions(is_disabled=True))
    markup = kwargs.pop("reply_markup", None)
    if isinstance(markup, InlineKeyboardMarkup):
        validate_inline_keyboard(markup.inline_keyboard)
    chunks = render_screen_chunks(text, limit=limit)
    result = None
    for index, chunk in enumerate(chunks):
        result = await send_message(
            chat_id,
            chunk,
            parse_mode="HTML",
            reply_markup=(
                markup if index == len(chunks) - 1 else None
            ),
            **kwargs,
        )
    return result


def render_edit_screen(
    text: str, limit: int = MAX_SCREEN_UNITS
) -> str:
    """Render one editable HTML screen; reject content that needs pagination."""
    chunks = render_screen_chunks(text, limit=limit)
    if len(chunks) != 1:
        raise ValueError("Длинное сообщение нельзя обновить одним экраном.")
    return chunks[0]
