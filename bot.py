import json
import os
import re
import threading
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Tuple

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

DATE, TIME, NAME, PHONE, CONFIRM = range(5)

BOOKINGS_FILE = os.path.join(os.path.dirname(__file__), "bookings.json")


_storage_lock = threading.Lock()

# test comment
# Класс Booking описывает одну подтвержденную запись на приём.
# Экземпляры сохраняются в JSON как bookings[DD-MM-YYYY][HH:MM].
# Поля включают данные пользователя, дату/время приёма и штамп создания
@dataclass
class Booking:
    user_id: int
    username: Optional[str]
    full_name: str
    phone: str
    date: str  # DD-MM-YYYY
    time: str  # HH:MM
    created_at: str  # ISO timestamp


# Загружает все бронирования из локального JSON-файла.
def load_bookings() -> Dict[str, Dict[str, Dict[str, str]]]:
    """Load bookings from disk. Structure: {date: {time: booking_dict}}"""
    if not os.path.exists(BOOKINGS_FILE):
        return {}
    with _storage_lock:
        try:
            with open(BOOKINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
                return {}
        except Exception:
            return {}


# Сохраняет все бронирования в локальный JSON-файл (потокобезопасно).
def save_bookings(data: Dict[str, Dict[str, Dict[str, str]]]) -> None:
    with _storage_lock:
        with open(BOOKINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# Возвращает список рабочих дат на ближайшие n_days (без воскресений).
def get_working_days(n_days: int = 14) -> List[date]:
    """Return next n_days dates including today, excluding Sundays."""
    days: List[date] = []
    today = date.today()
    d = today
    while len(days) < n_days:
        if d.weekday() != 6:  # 0=Mon ... 6=Sun
            days.append(d)
        d += timedelta(days=1)
    return days


# Форматирует дату в краткий человекочитаемый вид на русском (день недели, число, месяц).
def format_date_human(d: date) -> str:
    months = [
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    ]
    weekdays = [
        "Пн",
        "Вт",
        "Ср",
        "Чт",
        "Пт",
        "Сб",
        "Вс",
    ]
    return f"{weekdays[d.weekday()]} {d.day} {months[d.month - 1]}"


# Генерирует тайм-слоты каждые 30 минут с 10:00 до 18:00.
def generate_time_slots() -> List[str]:
    """Return half-hour slots between 10:00 and 18:00 (last start 17:30)."""
    start = dtime(hour=10, minute=0)
    end = dtime(hour=18, minute=0)
    slots: List[str] = []
    current = datetime.combine(date.today(), start)
    end_dt = datetime.combine(date.today(), end)
    while current < end_dt:
        slots.append(current.strftime("%H:%M"))
        current += timedelta(minutes=30)
    return slots


# Создаёт клавиатуру выбора даты из ближайших рабочих дней.
def build_dates_keyboard() -> ReplyKeyboardMarkup:
    days = get_working_days(14)
    rows: List[List[str]] = []
    row: List[str] = []
    for d in days:
        label = f"{format_date_human(d)} ({d.strftime('%d-%m-%Y')})"
        row.append(label)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


# Создаёт клавиатуру выбора времени для выбранной даты с учётом занятых/прошедших слотов.
def build_times_keyboard(selected_date: str, now: Optional[datetime] = None) -> ReplyKeyboardMarkup:
    bookings = load_bookings()
    booked_for_day = set((bookings.get(selected_date) or {}).keys())
    slots = generate_time_slots()

    # Disable past slots if selected date is today
    if now is None:
        now = datetime.now()
    if selected_date == now.strftime("%d-%m-%Y"):
        slots = [s for s in slots if datetime.combine(now.date(), datetime.strptime(s, "%H:%M").time()) >= now.replace(second=0, microsecond=0)]

    available = [s for s in slots if s not in booked_for_day]
    rows: List[List[str]] = []
    row: List[str] = []
    for t in available:
        row.append(t)
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if not rows:
        rows = [["Нет доступного времени, выберите другую дату"], ["⬅ Назад"]]
    else:
        rows.append(["⬅ Назад"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


# Проверяет валидность номера телефона по количеству цифр (допускает разные форматы ввода).
def is_valid_phone(text: str) -> bool:
    # Accept formats like +7xxxxxxxxxx, 8xxxxxxxxxx, or with spaces/dashes/parentheses
    digits = re.sub(r"\D", "", text)
    return 10 <= len(digits) <= 12


# Обрабатывает /start и /help: выводит приветствие и доступные команды.
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.effective_user.first_name or ""
    text = (
        "Здравствуйте! Я бот записи к стоматологу.\n\n"
        "Доступные команды:\n"
        "/book — записаться\n"
        "/mybookings — мои записи\n"
        "/cancel — отменить текущий диалог"
    )
    if name:
        text = f"{name}, " + text
    await update.message.reply_text(text)
    return ConversationHandler.END


# Точка входа в сценарий записи: предлагает выбрать дату.
async def book_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Выберите дату приема:", reply_markup=build_dates_keyboard()
    )
    return DATE


# Обработка выбранной даты: проверка формата, переход к выбору времени.
async def date_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message.text.strip()
    if "(" in msg and ")" in msg and msg.endswith(")"):
        iso = msg[msg.rfind("(") + 1 : -1]
    else:
        iso = msg
    try:
        datetime.strptime(iso, "%d-%m-%Y")
    except Exception:
        await update.message.reply_text(
            "Пожалуйста, выберите дату с клавиатуры.", reply_markup=build_dates_keyboard()
        )
        return DATE
    context.user_data["date"] = iso
    await update.message.reply_text(
        f"Выбрана дата: {iso}. Теперь выберите время:",
        reply_markup=build_times_keyboard(iso),
    )
    return TIME


# Обработка выбранного времени: проверка доступности, запрос ФИО.
async def time_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chosen = update.message.text.strip()
    if chosen == "⬅ Назад":
        return await book_entry(update, context)
    if not re.match(r"^\d{2}:\d{2}$", chosen):
        if "Нет доступного времени" in chosen:
            await update.message.reply_text(
                "Выберите другую дату:", reply_markup=build_dates_keyboard()
            )
            return DATE
        await update.message.reply_text(
            "Пожалуйста, выберите время с клавиатуры.",
            reply_markup=build_times_keyboard(context.user_data.get("date", "")),
        )
        return TIME
    # Double-check availability
    day = context.user_data.get("date")
    if not day:
        await update.message.reply_text("Сначала выберите дату.")
        return await book_entry(update, context)
    bookings = load_bookings()
    if chosen in (bookings.get(day) or {}):
        await update.message.reply_text(
            "Это время только что заняли. Выберите другое:",
            reply_markup=build_times_keyboard(day),
        )
        return TIME
    context.user_data["time"] = chosen
    await update.message.reply_text(
        "Введите ваше ФИО (как в документе):", reply_markup=ReplyKeyboardRemove()
    )
    return NAME


# Обработка введённого ФИО: минимальная валидация, запрос телефона.
async def name_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    fullname = update.message.text.strip()
    if len(fullname) < 3:
        await update.message.reply_text("Пожалуйста, введите корректное ФИО.")
        return NAME
    context.user_data["name"] = fullname
    await update.message.reply_text(
        "Введите номер телефона (например, +7 999 123-45-67):"
    )
    return PHONE


# Обработка телефона: валидация и показ экрана подтверждения.
async def phone_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    if not is_valid_phone(phone):
        await update.message.reply_text("Пожалуйста, введите корректный номер телефона.")
        return PHONE
    context.user_data["phone"] = phone

    day = context.user_data.get("date")
    t = context.user_data.get("time")
    name = context.user_data.get("name")
    confirm_text = (
        "Проверьте данные:\n\n"
        f"Дата: <b>{day}</b>\n"
        f"Время: <b>{t}</b>\n"
        f"ФИО: <b>{name}</b>\n"
        f"Телефон: <b>{phone}</b>\n\n"
        "Подтвердить запись? (Да/Нет)"
    )
    await update.message.reply_text(
        confirm_text,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup([["Да"], ["Нет"]], resize_keyboard=True, one_time_keyboard=True),
    )
    return CONFIRM


# Подтверждение записи: повторная проверка слота, сохранение бронирования, итоговое сообщение.
async def confirm_booking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answer = (update.message.text or "").strip().lower()
    if answer not in {"да", "нет"}:
        await update.message.reply_text(
            "Пожалуйста, ответьте 'Да' или 'Нет'."
        )
        return CONFIRM
    if answer == "нет":
        await update.message.reply_text(
            "Запись отменена. Вы можете начать заново командой /book",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    # Persist booking
    day = context.user_data.get("date")
    t = context.user_data.get("time")
    name = context.user_data.get("name")
    phone = context.user_data.get("phone")
    user = update.effective_user

    bookings = load_bookings()
    if day not in bookings:
        bookings[day] = {}
    # Check if slot was taken meanwhile
    if t in bookings[day]:
        await update.message.reply_text(
            "К сожалению, это время только что заняли. Выберите другое:",
            reply_markup=build_times_keyboard(day),
        )
        return TIME

    b = Booking(
        user_id=user.id,
        username=user.username,
        full_name=name,
        phone=phone,
        date=day,
        time=t,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    bookings[day][t] = asdict(b)
    save_bookings(bookings)

    await update.message.reply_text(
        f"Запись подтверждена!\n\nДата: {day}\nВремя: {t}\nФИО: {name}\nТелефон: {phone}",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# Команда /mybookings: показывает будущие записи текущего пользователя.
async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    bookings = load_bookings()
    upcoming: List[Tuple[str, str]] = []
    now = datetime.now()
    for day, times in bookings.items():
        for t, payload in times.items():
            if payload.get("user_id") == user.id:
                try:
                    dt = datetime.strptime(f"{day} {t}", "%d-%m-%Y %H:%M")
                except Exception:
                    continue
                if dt >= now:
                    upcoming.append((day, t))
    if not upcoming:
        await update.message.reply_text("У вас нет будущих записей.")
        return
    upcoming.sort(key=lambda x: datetime.strptime(f"{x[0]} {x[1]}", "%d-%m-%Y %H:%M"))
    text_lines = ["Ваши записи:"] + [f"• {d} в {t}" for d, t in upcoming]
    await update.message.reply_text("\n".join(text_lines))


# Команда /cancel: завершает текущий диалог и скрывает клавиатуру.
async def cancel_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Диалог отменен.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# Собирает и настраивает Telegram-приложение и обработчики команд/сценариев.
def build_application(token: str):
    app = ApplicationBuilder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("book", book_entry)],
        states={
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, date_chosen)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, time_chosen)],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_entered)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_entered)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_booking)],
        },
        fallbacks=[CommandHandler("cancel", cancel_dialog)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("mybookings", my_bookings))
    app.add_handler(CommandHandler("cancel", cancel_dialog))
    app.add_handler(conv)
    return app


# Точка входа: читает токен из переменной окружения и запускает бота в polling-режиме.
def main() -> None:
    # Пытаемся получить токен из локального файла secrets.py, затем из переменной окружения
    token = None
    try:
        # type: ignore[attr-defined]
        import secrets  # локальный файл (не коммитить)

        token = getattr(secrets, "TELEGRAM_BOT_TOKEN", None)
    except Exception:
        token = None
    if not token:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не найден токен: укажите TELEGRAM_BOT_TOKEN в secrets.py или как переменную окружения."
        )
    app = build_application(token)
    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()


