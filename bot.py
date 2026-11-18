import asyncio
import logging
import os
from io import BytesIO
from zipfile import ZipFile, BadZipFile
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.fsm.strategy import FSMStrategy
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.token import TokenValidationError
from dotenv import load_dotenv

from contracts import ROOT, Contract, Field, load_catalog, merge_fields, render


class Form(StatesGroup):
    menu = State()
    filling = State()


def keyboard(items):
    builder = InlineKeyboardBuilder()
    for label, callback in items:
        builder.button(text=label, callback_data=callback)
    return builder.adjust(1).as_markup()


def prompt(fields, intro):
    field = fields[0]
    formats = {"date": "Дата в формате ДД.ММ.ГГГГ.", "inn": "ИНН должен содержать 10 или 12 цифр.",
               "ogrn": "ОГРН — 13 цифр, ОГРНИП — 15.", "kpp": "КПП — 9 цифр.",
               "account": "Номер счёта — 20 цифр.", "bic": "БИК — 9 цифр."}
    hint = formats.get(field.type, "")
    return escape(f"{intro}\n\nУточните: {field.label}. {hint}\nМожно прислать значение отдельным сообщением или отправить дополнительные реквизиты целиком.")


def make_router(catalog: dict[str, Contract], threshold: int) -> Router:
    router = Router()
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    another = keyboard([("Заполнить другой договор", "contracts:new")])
    retry = keyboard([("Повторить отправку", "contracts:retry")])

    async def show_menu(message: Message, state: FSMContext, group: str | None = None):
        if group is None and len(catalog) > threshold:
            groups = dict.fromkeys(contract.group for contract in catalog.values())
            options = [(name, f"group:{name}") for name in groups]
            title = "Выберите РАО или ВОИС."
        else:
            options = [(contract.title, f"template:{contract.id}")
                       for contract in catalog.values() if group is None or contract.group == group]
            if group is not None:
                options.append(("К выбору организации", "contracts:new"))
            title = "Выберите шаблон договора."
        sent = await message.answer(title, reply_markup=keyboard(options))
        await state.set_data({"menu_message": sent.message_id})
        await state.set_state(Form.menu)

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await show_menu(message, state)

    @router.callback_query(F.data == "contracts:new")
    async def restart(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        await show_menu(callback.message, state)

    @router.callback_query(Form.menu, F.data.startswith("group:") | F.data.startswith("template:"))
    async def select(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if callback.message.message_id != data["menu_message"]:
            await callback.answer("Выберите вариант в последнем меню.")
            return
        action, selected = callback.data.split(":", 1)
        if action == "group":
            await callback.answer()
            await show_menu(callback.message, state, selected)
            return
        contract = catalog.get(selected)
        if contract is None:
            await callback.answer("Шаблон недоступен. Откройте меню командой /start.")
            return
        await callback.answer()
        await callback.message.answer(
            f"{contract.title}. Пришлите карточку компании в привычном виде: "
            "текстом или файлом DOCX. Я распознаю название, адреса и банковские реквизиты "
            "и заполню договор. Недостающие сведения уточню после обработки."
        )
        await state.set_data({"contract": contract.id, "values": {}})
        await state.set_state(Form.filling)

    async def send_prompt(message, fields, intro):
        await message.answer(prompt(fields, intro), parse_mode="HTML")

    async def deliver(message: Message, state: FSMContext, contract: Contract, values: dict[str, str]):
        try:
            content = await asyncio.to_thread(render, contract, values)
            await message.answer_document(
                BufferedInputFile(content, filename=f"dogovor-{contract.id}.docx"),
                reply_markup=another,
            )
        except Exception:
            await message.answer(
                "Не удалось отправить договор. Введённые реквизиты сохранены до перезапуска бота.",
                reply_markup=retry,
            )
            return
        await state.clear()

    @router.callback_query(Form.filling, F.data == "contracts:retry")
    async def retry_document(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        contract = catalog[data["contract"]]
        if len(data["values"]) != len(contract.fields):
            await callback.answer("Сначала заполните оставшиеся поля.")
            return
        await callback.answer()
        await deliver(callback.message, state, contract, data["values"])

    @router.message(Form.filling)
    async def receive(message: Message, state: FSMContext):
        data = await state.get_data()
        contract = catalog[data["contract"]]
        text = message.text or message.caption or ""
        if message.document:
            attachment = message.document
            if not (attachment.file_name or "").lower().endswith(".docx"):
                await message.answer("Пришлите карточку текстом или в формате DOCX.")
                return
            if attachment.file_size is None or attachment.file_size > 5 * 1024 * 1024:
                await message.answer("Размер DOCX должен быть не более 5 МБ.")
                return
            try:
                stream = BytesIO()
                await message.bot.download(attachment, destination=stream)
                stream.seek(0)
                with ZipFile(stream) as archive:
                    if sum(item.file_size for item in archive.infolist()) > 20 * 1024 * 1024:
                        raise ValueError("Document too large")
                stream.seek(0)
                document = Document(stream)
                parts = []
                for element in document.element.body:
                    if element.tag.endswith("}p"):
                        parts.append(Paragraph(element, document).text)
                    elif element.tag.endswith("}tbl"):
                        for row in Table(element, document).rows:
                            cells = list(dict.fromkeys(cell.text for cell in row.cells))
                            parts.append(": ".join(cells))
                text = "\n".join([*parts, text])
            except Exception:
                await message.answer("Не удалось прочитать DOCX. Скопируйте реквизиты из карточки и пришлите текстом.")
                return
        values = merge_fields(contract, data["values"], text, requested_key=data.get("requested_key"))
        await state.update_data(values=values)
        pending = [field for field in contract.fields if field.key not in values]
        if pending:
            await state.update_data(requested_key=pending[0].key)
            await send_prompt(message, pending, "Карточка обработана. Принятые сведения сохранены.")
            return
        await deliver(message, state, contract, values)

    @router.callback_query()
    async def expired(callback: CallbackQuery):
        await callback.answer("Откройте актуальное меню командой /start.")

    @router.message()
    async def inactive(message: Message):
        await message.answer("Для выбора договора отправьте /start.")

    return router


async def main():
    load_dotenv(ROOT / ".env")
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Укажите BOT_TOKEN в файле .env.")
    try:
        catalog, threshold = load_catalog()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"Ошибка конфигурации: {exc}") from None

    dispatcher = Dispatcher(
        storage=MemoryStorage(),
        events_isolation=SimpleEventIsolation(),
        fsm_strategy=FSMStrategy.USER_IN_CHAT,
    )
    dispatcher.include_router(make_router(catalog, threshold))
    try:
        async with Bot(token=token) as bot:
            print("Бот запущен. Для остановки нажмите Ctrl+C.", flush=True)
            await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    except (TelegramAPIError, TokenValidationError):
        raise SystemExit("Не удалось запустить Telegram polling. Проверьте токен и подключение.") from None


if __name__ == "__main__":
    # Framework exceptions can include request details. Keep update and exception logs disabled.
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
