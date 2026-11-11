"""Веерная рассылка: общее сообщение в группу + персональные ЛС с кнопкой."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from .service_registry import task_token
from .ui import render_edit_screen, send_telegram_screen

RATE_LIMIT_DELAY = 0.05         # согласно ТЗ — защита от HTTP 429
CONFIRM_PREFIX = "cfm"          # короткий префикс для callback_data
TELEGRAM_TEXT_LIMIT = 4096


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def build_confirm_kb(role: str, sunday_iso: str) -> InlineKeyboardMarkup:
    data = f"{CONFIRM_PREFIX}:{sunday_iso}:{role}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принял, буду", callback_data=data),
    ]])


def build_service_confirm_kb(token: str, event_date_iso: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Принял, буду",
            callback_data=f"scf:{event_date_iso}:{token}",
        ),
    ]])


def build_personal_answer_kb(
    service_id: str, event_date, task: str
) -> InlineKeyboardMarkup:
    """«Принял» / «Не смогу» в личном напоминании (этап 3.2)."""
    suffix = f"{service_id}:{event_date:%Y%m%d}:{task_token(task)}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принял", callback_data=f"pcf:c:{suffix}"),
        InlineKeyboardButton(text="🚫 Не смогу", callback_data=f"pcf:d:{suffix}"),
    ]])


def build_general_message(
    assignments: dict[str, dict],
    templates: dict,
    sunday_str: str,
) -> str:
    template = templates["general"]
    mentions = {}
    for role, placeholder in (
        ("Ответственный", "tg_responsible"),
        ("Ведущий", "tg_leader"),
        ("Проповедник", "tg_preacher"),
    ):
        username = assignments.get(role, {}).get("username")
        mention = f"@{username}" if username else "не найден"
        template = template.replace(f"@{{{placeholder}}}", f"{{{placeholder}}}")
        mentions[placeholder] = mention
    return template.format(sunday_date=sunday_str, **mentions)


def build_personal_message(
    role: str,
    info: dict,
    templates: dict,
    sunday_str: str,
) -> str:
    text = templates["roles"][role].format(
        name=info["name"], sunday_date=sunday_str,
    )
    return text if text.startswith("🔔") else f"🔔 {text}"


async def _notify_admin(bot: Bot, admin_id: int, text: str,
                        debug_logger: logging.Logger) -> bool:
    """Отправляет предупреждение администратору; True — если доставлено."""
    try:
        await send_telegram_screen(
            bot.send_message, admin_id, f"⚠️ {text}"
        )
    except Exception:  # noqa: BLE001
        debug_logger.exception("Не удалось уведомить администратора")
        return False
    return True


async def send_assignments(
    bot: Bot,
    group_chat_id: int,
    admin_id: int,
    assignments: dict[str, dict],
    state: dict,
    templates: dict,
    sunday_str: str,
    sunday_iso: str,
    interaction_logger: logging.Logger,
    debug_logger: logging.Logger,
    save_state: Callable[[], None] | None = None,
    user_notifications_enabled: Callable[[int], bool] | None = None,
    send_personal_notifications: bool = True,
) -> bool:
    """Отправляет общее сообщение и ЛС. Обновляет state на месте."""
    delivery_state_persisted = True

    async def persist_progress() -> None:
        nonlocal delivery_state_persisted
        if save_state is None:
            return
        try:
            save_state()
        except OSError as exc:
            delivery_state_persisted = False
            debug_logger.exception("Не удалось сохранить прогресс рассылки")
            await _notify_admin(
                bot,
                admin_id,
                f"Не удалось сохранить прогресс рассылки: {exc}",
                debug_logger,
            )

    # --- 1) Общее сообщение в группу ---
    general_text = build_general_message(assignments, templates, sunday_str)
    debug_logger.debug("Подготовлено сообщение в групповой чат:\n%s", general_text)
    if _utf16_length(general_text) > TELEGRAM_TEXT_LIMIT:
        delivery_state_persisted = False
        message = (
            "Общее уведомление не отправлено: длина превышает лимит Telegram "
            f"в {TELEGRAM_TEXT_LIMIT} единиц UTF-16"
        )
        debug_logger.error(message)
        await _notify_admin(bot, admin_id, message, debug_logger)
    elif state.get("general_sent"):
        if state.get("general_text") != general_text:
            try:
                rendered_text = render_edit_screen(
                    general_text, limit=TELEGRAM_TEXT_LIMIT
                )
            except ValueError:
                try:
                    general_msg = await send_telegram_screen(
                        bot.send_message,
                        group_chat_id,
                        general_text,
                        limit=TELEGRAM_TEXT_LIMIT,
                    )
                except TelegramAPIError as exc:
                    delivery_state_persisted = False
                    debug_logger.exception(
                        "Не удалось отправить обновлённое сообщение в группе"
                    )
                    await _notify_admin(
                        bot,
                        admin_id,
                        f"Ошибка обновления сообщения в группе: {exc}",
                        debug_logger,
                    )
                else:
                    state["general_message_id"] = general_msg.message_id
                    state["general_text"] = general_text
                    await persist_progress()
            else:
                try:
                    await bot.edit_message_text(
                        rendered_text,
                        chat_id=group_chat_id,
                        message_id=state["general_message_id"],
                        parse_mode="HTML",
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                except TelegramAPIError as exc:
                    delivery_state_persisted = False
                    debug_logger.exception("Не удалось обновить сообщение в группе")
                    await _notify_admin(
                        bot,
                        admin_id,
                        f"Ошибка обновления сообщения в группе: {exc}",
                        debug_logger,
                    )
                else:
                    state["general_text"] = general_text
                    await persist_progress()
                    interaction_logger.info(
                        "Обновлено общее уведомление в чате %s (msg_id=%s)",
                        group_chat_id, state["general_message_id"],
                    )
    else:
        try:
            general_msg = await send_telegram_screen(
                bot.send_message,
                group_chat_id,
                general_text,
                limit=TELEGRAM_TEXT_LIMIT,
            )
        except TelegramAPIError as exc:
            debug_logger.exception("Не удалось отправить сообщение в группу")
            await _notify_admin(
                bot, admin_id, f"Ошибка отправки в группу: {exc}", debug_logger
            )
            raise
        state["general_message_id"] = general_msg.message_id
        state["general_sent"] = True
        state["general_text"] = general_text
        await persist_progress()
        interaction_logger.info(
            "Общее уведомление отправлено в чат %s (msg_id=%s)",
            group_chat_id, general_msg.message_id,
        )

    # --- 2) Персональные ЛС ---
    for role, info in assignments.items():
        participant_state = state["participants"][role]
        if info.get("is_informational", False):
            debug_logger.debug(
                "Личное уведомление пропущено для информационной задачи: %s",
                role,
            )
            participant_state["notification_skipped"] = True
            continue
        if not info.get("tg_id") or participant_state.get("delivered"):
            continue
        if not send_personal_notifications:
            participant_state["notification_skipped"] = True
            debug_logger.debug(
                "Традиционная личная отправка отключена: роль=%s tg_id=%s",
                role,
                info["tg_id"],
            )
            continue
        if (
            user_notifications_enabled is not None
            and not user_notifications_enabled(info["tg_id"])
        ):
            participant_state["notification_skipped"] = True
            debug_logger.debug(
                "Личное уведомление пропущено по настройке пользователя: "
                "role=%s tg_id=%s",
                role,
                info["tg_id"],
            )
            continue
        participant_state["notification_skipped"] = False
        text = build_personal_message(role, info, templates, sunday_str)
        debug_logger.debug(
            "Подготовлено личное сообщение: роль=%s, tg_id=%s, username=@%s\n%s",
            role, info["tg_id"], info["username"], text,
        )
        if _utf16_length(text) > TELEGRAM_TEXT_LIMIT:
            message = (
                f"Личное напоминание для {info['name']} ({role}) не отправлено: "
                f"длина превышает лимит Telegram в {TELEGRAM_TEXT_LIMIT} единиц UTF-16"
            )
            debug_logger.error(message)
            await _notify_admin(bot, admin_id, message, debug_logger)
            continue
        try:
            msg = await send_telegram_screen(
                bot.send_message,
                info["tg_id"],
                text,
                limit=TELEGRAM_TEXT_LIMIT,
                reply_markup=build_confirm_kb(role, sunday_iso),
            )
            participant_state["message_id"] = msg.message_id
            participant_state["chat_id"] = msg.chat.id
            participant_state["group_chat_id"] = group_chat_id
            participant_state["delivered"] = True
            await persist_progress()
            interaction_logger.info(
                "ЛС отправлено: role=%s tg_id=%s username=@%s",
                role, info["tg_id"], info["username"],
            )
        except TelegramAPIError as exc:
            debug_logger.exception(
                "Не удалось отправить ЛС роли '%s' (tg_id=%s)", role, info["tg_id"],
            )
            await _notify_admin(
                bot, admin_id,
                f"Не удалось отправить ЛС {info['name']} ({role}): {exc}",
                debug_logger,
            )

        await asyncio.sleep(RATE_LIMIT_DELAY)   # защита от 429

    return delivery_state_persisted
