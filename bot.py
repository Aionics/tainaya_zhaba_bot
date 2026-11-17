"""
🎅🐸 Новогодняя Жабилизация 2027 — бот Тайного Санты для чата «Жабилизируемся».

Стек: Python 3.10+, aiogram 3, SQLite.
"""

import asyncio
import json
import logging
import os
import random
import sqlite3
from datetime import datetime, timedelta
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
DB_PATH = os.getenv("DB_PATH", "zhabilizacia.db")

GAME_NAME = "Новогодняя Жабилизация 2027"
GAME_NAME_GEN = "Новогодней Жабилизации 2027"  # родительный падеж: «бот Новогодней Жабилизации»
MIN_PARTICIPANTS = 3

# Лимиты длины ответов
NAME_MAX = 50
TEXT_MAX = 500
MAX_PHOTOS = 10      # столько фото помещается в один альбом Telegram
ALBUM_WAIT = 1.0     # сек: ждём, пока долетят все фото альбома

LOC_PARTY = "party"  # будет на празднике, получит лично
LOC_AWAY = "away"    # в другом месте, нужна доставка


# ───────────────────────── База данных ─────────────────────────

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # Каждая запись сразу и полностью сбрасывается на диск — переживёт отключение света
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db() -> None:
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(db_dir, exist_ok=True)
    with db() as conn:
        # Журнал WAL: при обрыве питания посреди записи база не портится
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS fsm (
                key   TEXT PRIMARY KEY,
                state TEXT,
                data  TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS participants (
                user_id   INTEGER PRIMARY KEY,
                username  TEXT,
                name      TEXT NOT NULL,
                wish      TEXT NOT NULL,
                not_wish  TEXT,
                location  TEXT NOT NULL DEFAULT 'party',
                delivery  TEXT,
                wish_photos TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS pairs (
                santa_id    INTEGER PRIMARY KEY,
                receiver_id INTEGER NOT NULL
            );
            """
        )
        # Если база создана старой версией бота — добавим недостающие колонки
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(participants)")}
        if "location" not in cols:
            conn.execute("ALTER TABLE participants ADD COLUMN location TEXT NOT NULL DEFAULT 'party'")
        if "delivery" not in cols:
            conn.execute("ALTER TABLE participants ADD COLUMN delivery TEXT")
        if "wish_photos" not in cols:
            conn.execute("ALTER TABLE participants ADD COLUMN wish_photos TEXT")


def get_setting(key: str) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_participant(user_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM participants WHERE user_id = ?", (user_id,)
        ).fetchone()


def all_participants() -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM participants ORDER BY name").fetchall()


def save_participant(user_id: int, username: str | None, data: dict) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO participants (user_id, username, name, wish, not_wish, location, delivery, wish_photos)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                name     = excluded.name,
                wish     = excluded.wish,
                not_wish = excluded.not_wish,
                location = excluded.location,
                delivery = excluded.delivery,
                wish_photos = excluded.wish_photos
            """,
            (
                user_id,
                username,
                data["name"],
                data["wish"],
                data.get("not_wish"),
                data.get("location", LOC_PARTY),
                data.get("delivery"),
                data.get("wish_photos"),
            ),
        )


def delete_participant(user_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM participants WHERE user_id = ?", (user_id,))


def draw_done() -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM pairs LIMIT 1").fetchone() is not None


def save_pairs(pairs: list[tuple[int, int]]) -> None:
    with db() as conn:
        conn.execute("DELETE FROM pairs")
        conn.executemany(
            "INSERT INTO pairs (santa_id, receiver_id) VALUES (?, ?)", pairs
        )


def clear_pairs() -> None:
    with db() as conn:
        conn.execute("DELETE FROM pairs")


EDITABLE_FIELDS = {"name", "wish", "wish_photos", "not_wish", "location", "delivery"}


def update_participant(user_id: int, **fields) -> None:
    """Точечно меняет поля анкеты (только из белого списка)."""
    assert fields and set(fields) <= EDITABLE_FIELDS
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        conn.execute(
            f"UPDATE participants SET {cols} WHERE user_id = ?",
            (*fields.values(), user_id),
        )


def refresh_username(user_id: int, username: str | None) -> None:
    """Обновляет @тег участника, если он поменял его в Telegram."""
    with db() as conn:
        conn.execute(
            "UPDATE participants SET username = ? WHERE user_id = ? AND username IS NOT ?",
            (username, user_id, username),
        )


def get_receiver(santa_id: int) -> sqlite3.Row | None:
    """Кому дарит этот Санта."""
    with db() as conn:
        return conn.execute(
            """
            SELECT p.* FROM pairs
            JOIN participants p ON p.user_id = pairs.receiver_id
            WHERE pairs.santa_id = ?
            """,
            (santa_id,),
        ).fetchone()


def get_santa_id(receiver_id: int) -> int | None:
    """Кто Санта у этого участника."""
    with db() as conn:
        row = conn.execute(
            "SELECT santa_id FROM pairs WHERE receiver_id = ?", (receiver_id,)
        ).fetchone()
    return row["santa_id"] if row else None


class SQLiteStorage(BaseStorage):
    """
    Хранит, кто на каком шаге анкеты (и его промежуточные ответы), в той же базе.
    После перезапуска или отключения света человек продолжит с того же вопроса.
    """

    @staticmethod
    def _key(key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id}:{key.destiny}"

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        with db() as conn:
            conn.execute(
                "INSERT INTO fsm (key, state) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET state = excluded.state",
                (self._key(key), value),
            )

    async def get_state(self, key: StorageKey) -> str | None:
        with db() as conn:
            row = conn.execute("SELECT state FROM fsm WHERE key = ?", (self._key(key),)).fetchone()
        return row["state"] if row else None

    async def set_data(self, key: StorageKey, data: dict) -> None:
        with db() as conn:
            conn.execute(
                "INSERT INTO fsm (key, data) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET data = excluded.data",
                (self._key(key), json.dumps(data, ensure_ascii=False)),
            )

    async def get_data(self, key: StorageKey) -> dict:
        with db() as conn:
            row = conn.execute("SELECT data FROM fsm WHERE key = ?", (self._key(key),)).fetchone()
        return json.loads(row["data"]) if row else {}

    async def close(self) -> None:
        pass


# ───────────────────────── Резервные копии ─────────────────────────

BACKUP_KEEP = 7


def backup_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")


def make_backup(today: str | None = None) -> str | None:
    """Копия базы раз в сутки; хранятся последние BACKUP_KEEP копий."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    folder = backup_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"zhabilizacia-{today}.db")
    if os.path.exists(path):
        return None
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)  # безопасная копия даже во время работы бота
    finally:
        dst.close()
        src.close()
    old = sorted(f for f in os.listdir(folder) if f.startswith("zhabilizacia-") and f.endswith(".db"))
    for f in old[:-BACKUP_KEEP]:
        os.remove(os.path.join(folder, f))
    return path


async def backup_loop() -> None:
    while True:
        try:
            path = make_backup()
            if path:
                logging.info("Резервная копия: %s", path)
        except Exception:
            logging.exception("Не удалось сделать резервную копию")
        await asyncio.sleep(3600)


# ───────────────────────── Жеребьёвка ─────────────────────────

def make_pairs(user_ids: list[int]) -> list[tuple[int, int]]:
    """
    Перемешиваем участников и выстраиваем в один круг:
    каждый дарит следующему, последний — первому.
    Так никто не вытянет сам себя, и все цепочки замкнуты.
    """
    ids = list(user_ids)
    random.SystemRandom().shuffle(ids)
    return [(ids[i], ids[(i + 1) % len(ids)]) for i in range(len(ids))]


# ───────────────────────── Тексты ─────────────────────────

def budget_text() -> str:
    budget = get_setting("budget")
    return escape(budget) if budget else "организатор ещё не задал 🤔"


# ───────────────────────── Дата обмена ─────────────────────────
# Время — по часам компьютера, на котором запущен бот.

EVENT_HOUR = 18  # напоминания приходят в 18:00 — за 7 дней и за 1 день до даты обмена


def parse_event_date(text: str) -> datetime | None:
    """«31.12.2026» → дата обмена (в 18:00)."""
    try:
        dt = datetime.strptime(text.strip(), "%d.%m.%Y")
    except ValueError:
        return None
    return dt.replace(hour=EVENT_HOUR)


def get_event_dt() -> datetime | None:
    value = get_setting("event_dt")
    if not value:
        return None
    return datetime.fromisoformat(value).replace(hour=EVENT_HOUR, minute=0, second=0)


def event_text() -> str:
    dt = get_event_dt()
    if not dt:
        return "организатор ещё не задал 🤔"
    return dt.strftime("%d.%m.%Y")


def days_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


def rules_text() -> str:
    return (
        f"📜 <b>Правила «{GAME_NAME}»</b>\n\n"
        "1. Жми «Участвую» и заполни короткую анкету.\n"
        "2. Пока нет жеребьёвки, анкету можно изменить.\n"
        "3. Организатор запускает жеребьёвку — и я пришлю тебе в личку, "
        "кому ты даришь подарок, вместе с его анкетой.\n"
        "4. 🤫 Это тайна! Кто дарит тебе — не знает никто, даже организатор.\n"
        "5. ❓ Что-то непонятно в анкете подопечного? Спроси его анонимно через меня.\n"
        "6. ✈️ Не будешь на празднике? Укажи в анкете, как тебе отправить подарок.\n"
        f"7. 💰 Бюджет подарка: <b>{budget_text()}</b>\n"
        f"8. 📅 Обмен подарками: <b>{event_text()}</b>. "
        "Я напомню за неделю и за день.\n"
        "9. 🎁 На встрече дарим подарки и угадываем, кто чей Санта!"
    )


def name_with_tag(p: sqlite3.Row) -> str:
    """«Имя (@тег)» — как в списке участников; без тега — просто имя."""
    return escape(p["name"]) + (f" (@{escape(p['username'])})" if p["username"] else "")


def photos_of(raw: str | None) -> list[str]:
    """file_id фото к пожеланию (в базе — JSON-список)."""
    return json.loads(raw) if raw else []


def wish_view(wish: str | None, photos_raw: str | None) -> str:
    n = len(photos_of(photos_raw))
    text = escape(wish) if wish else ""
    if n:
        text += (" " if text else "") + f"🖼 <i>+ {n} фото</i>"
    return text or "<i>не указано</i>"


def profile_text(p: sqlite3.Row, for_santa: bool = False) -> str:
    name = name_with_tag(p) if for_santa else escape(p["name"])
    lines = [f"👤 <b>Имя:</b> {name}"]
    lines.append(f"🎁 <b>Хочет получить:</b> {wish_view(p['wish'], p['wish_photos'])}")
    if p["not_wish"]:
        lines.append(f"🚫 <b>Точно НЕ дарить:</b> {escape(p['not_wish'])}")
    if p["location"] == LOC_AWAY:
        lines.append("✈️ <b>Вручение: нужна доставка!</b>")
        if p["delivery"]:
            lines.append(escape(p["delivery"]))
        if for_santa:
            lines.append("⏰ <i>Отправь заранее — доставка в другую страну может занять пару недель.</i>")
    else:
        lines.append("📍 <b>Вручение:</b> лично на празднике 🎄")
    return "\n".join(lines)


def receiver_details(r: sqlite3.Row) -> str:
    return (
        f"{profile_text(r, for_santa=True)}\n\n"
        f"💰 Бюджет: <b>{budget_text()}</b>\n"
        f"📅 Обмен подарками: <b>{event_text()}</b>"
    )


def receiver_text(r: sqlite3.Row) -> str:
    """По кнопке «🎁 Кому я дарю?»."""
    return f"🐸 <b>Тайная Жаба выбрала тебе подопечного:</b>\n\n{receiver_details(r)}"


def draw_text(r: sqlite3.Row) -> str:
    """Пуш после жеребьёвки."""
    return (
        "🐸 <b>Тайная Жаба сделала выбор!</b>\n\n"
        f"Твой подопечный на {GAME_NAME_GEN}:\n\n"
        f"{receiver_details(r)}\n\n"
        "Об этом знаете только ты и Тайная Жаба 🤫"
    )


def participants_list_text(for_admin: bool) -> str:
    """Список участников: имя, @тег и ✈️ — без пожеланий и адресов."""
    people = all_participants()
    if not people:
        return "Пока никто не записался 🐸"
    lines = [
        f"{i}. {name_with_tag(p)}"
        + (" ✈️" if p["location"] == LOC_AWAY else "")
        for i, p in enumerate(people, 1)
    ]
    text = f"👥 <b>Участники ({len(people)}):</b>\n\n" + "\n".join(lines)
    if any(p["location"] == LOC_AWAY for p in people):
        text += "\n\n✈️ — не будет на празднике" + (", нужна доставка" if for_admin else "")
    return text


# ───────────────────────── Клавиатуры ─────────────────────────

def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
            for row in rows
        ]
    )


def main_menu(user_id: int) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    p = get_participant(user_id)
    if draw_done():
        if p:
            rows.append([("🎁 Кому я дарю?", "my_receiver")])
            rows.append([("❓ Спросить подопечного анонимно", "ask")])
            rows.append([("📝 Моя анкета", "my_profile")])
    elif p:
        rows.append([("📝 Моя анкета", "my_profile")])
        rows.append([("✏️ Изменить анкету", "edit")])
        rows.append([("🚪 Выйти из игры", "leave")])
    else:
        rows.append([("🐸 Участвую!", "join")])
    rows.append([("👥 Список участников", "people")])
    rows.append([("📜 Правила", "rules")])
    if user_id in ADMIN_IDS:
        rows.append([("👑 Панель организатора", "admin")])
    return kb(*rows)


BACK_KB = kb([("⬅️ Назад", "menu")])

# Постоянная кнопка внизу экрана — быстро посмотреть подопечного в любой момент
WARD_BTN = "🎁 Кому я дарю?"
MENU_BTN = "📋 Меню"
WARD_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=WARD_BTN), KeyboardButton(text=MENU_BTN)]],
    resize_keyboard=True,
    is_persistent=True,
)
SKIP_KB = kb([("⏭ Пропустить", "skip")])
CANCEL_KB = kb([("❌ Отмена", "cancel")])
LOCATION_KB = kb(
    [("🎄 На празднике с нашими — получу лично", "loc_party")],
    [("✈️ В другом месте — нужна доставка", "loc_away")],
)


def admin_menu() -> InlineKeyboardMarkup:
    rows = [
        [("💰 Задать бюджет", "admin_budget")],
        [("📅 Задать дату обмена", "admin_date")],
        [("👥 Список участников", "admin_list")],
        [("📣 Напомнить всем", "admin_broadcast")],
    ]
    if draw_done():
        rows.append([("♻️ Сбросить жеребьёвку", "admin_reset")])
    else:
        rows.append([("🎲 Запустить жеребьёвку", "admin_draw")])
    rows.append([("⬅️ Назад", "menu")])
    return kb(*rows)


# ───────────────────────── Состояния ─────────────────────────

class Form(StatesGroup):
    name = State()
    wish = State()
    location = State()
    delivery = State()
    not_wish = State()


class Edit(StatesGroup):
    name = State()
    wish = State()
    not_wish = State()
    location = State()
    delivery = State()
    confirm = State()   # показали «было → стало», ждём ✅ / ❌


class Ask(StatesGroup):
    question = State()  # Санта пишет вопрос подопечному
    answer = State()    # подопечный пишет ответ Санте


class AdminForm(StatesGroup):
    budget = State()
    date = State()
    broadcast = State()


async def ask_below(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """
    Задать вопрос новым сообщением внизу чата, а со старого сообщения убрать кнопки.
    Так вопрос не «прячется» наверху, если кнопку нажали в старом сообщении.
    """
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass  # сообщение слишком старое или уже без кнопок — не страшно
    await call.message.answer(text, reply_markup=markup)


async def collect_album(message: Message) -> list[Message] | None:
    """
    Telegram присылает альбом отдельными сообщениями. Первое сообщение ждёт
    остальные и возвращает весь альбом, остальные возвращают None (их уже учли).
    """
    if not message.media_group_id:
        return [message]
    key = f"{message.chat.id}:{message.media_group_id}"
    if key in _albums:
        _albums[key].append(message)
        return None
    _albums[key] = [message]
    await asyncio.sleep(ALBUM_WAIT)
    return _albums.pop(key)


_albums: dict[str, list[Message]] = {}


def text_and_photos(messages: list[Message]) -> tuple[str, list[str]]:
    """Текст (или подпись к фото) и file_id фото — берём самое большое разрешение."""
    text = next((m.text or m.caption for m in messages if m.text or m.caption), "") or ""
    photos = [m.photo[-1].file_id for m in messages if m.photo][:MAX_PHOTOS]
    return text.strip(), photos


async def send_with_photos(bot: Bot, chat_id: int, text: str, photos: list[str], **kwargs) -> bool:
    """Текст + фото. Текст с кнопками всегда последним — внизу чата."""
    try:
        if not photos:
            await bot.send_message(chat_id, text, **kwargs)
        elif len(photos) == 1 and len(text) <= 1024:
            await bot.send_photo(chat_id, photos[0], caption=text, **kwargs)
        else:
            if len(photos) == 1:
                await bot.send_photo(chat_id, photos[0])
            else:
                await bot.send_media_group(chat_id, [InputMediaPhoto(media=f) for f in photos])
            await bot.send_message(chat_id, text, **kwargs)
        return True
    except Exception:
        logging.exception("Не удалось отправить сообщение %s", chat_id)
        return False


async def send_safe(bot: Bot, user_id: int, text: str, **kwargs) -> bool:
    """Отправить сообщение, не падая, если человек заблокировал бота."""
    try:
        await bot.send_message(user_id, text, **kwargs)
        return True
    except Exception:
        logging.exception("Не удалось отправить сообщение %s", user_id)
        return False


router = Router()
# Анкета и меню работают только в личке, чтобы пожелания не светились в чате
router.message.filter(F.chat.type == ChatType.PRIVATE)


# ───────────────────────── Групповой чат ─────────────────────────

group_router = Router()
group_router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


@group_router.message(Command("start", "santa", "count"))
async def group_info(message: Message, bot: Bot) -> None:
    me = await bot.get_me()
    count = len(all_participants())
    status = "🎲 Жеребьёвка уже прошла!" if draw_done() else "Запись открыта!"
    await message.answer(
        f"🎅🐸 Стартует <b>{GAME_NAME}</b>!\n\n"
        f"Уже жабилизировались: <b>{count}</b> 🐸\n"
        f"💰 Бюджет подарка: <b>{budget_text()}</b>\n"
        f"📅 Обмен подарками: <b>{event_text()}</b>\n"
        f"{status}\n\n"
        f"Чтобы участвовать, напиши мне в личку: @{me.username}",
    )


# ───────────────────────── Общие команды ─────────────────────────

@router.message(CommandStart())
@router.message(F.text == MENU_BTN)
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        f"🎅🐸 Привет! Я бот <b>{GAME_NAME_GEN}</b>.\n\n"
        "Запишись, расскажи, о чём мечтаешь, — а после жеребьёвки "
        "я пришлю тебе того, кому ты даришь подарок 🎁",
        reply_markup=main_menu(message.from_user.id),
    )


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


# Стоит до анкеты и вопросов, чтобы кнопка срабатывала из любого состояния
@router.message(Command("my"))
@router.message(F.text == WARD_BTN)
async def cmd_my(message: Message, state: FSMContext) -> None:
    await state.clear()
    r = get_receiver(message.from_user.id)
    if not r:
        await message.answer(
            "Жеребьёвки ещё не было 🐸\n"
            f"📅 Обмен подарками: <b>{event_text()}</b>",
            reply_markup=main_menu(message.from_user.id),
        )
        return
    await send_with_photos(
        message.bot, message.chat.id, receiver_text(r), photos_of(r["wish_photos"]),
        reply_markup=kb([("❓ Спросить анонимно", "ask")], [("⬅️ В меню", "menu")]),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await call.message.edit_text(
        f"🎅🐸 <b>{GAME_NAME}</b>\nЧто делаем?",
        reply_markup=main_menu(call.from_user.id),
    )



@router.callback_query(F.data == "people")
async def cb_people(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(participants_list_text(for_admin=False), reply_markup=BACK_KB)


@router.callback_query(F.data == "rules")
async def cb_rules(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(rules_text(), reply_markup=BACK_KB)



@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(
        "Окей, отменил 🐸", reply_markup=main_menu(call.from_user.id)
    )
    await call.answer()


# ───────────────────────── Анкета ─────────────────────────

@router.callback_query(F.data == "join")
async def cb_join(call: CallbackQuery, state: FSMContext) -> None:
    if draw_done():
        await call.answer("Жеребьёвка уже прошла, запись закрыта 🐸", show_alert=True)
        return
    if get_participant(call.from_user.id):
        await cb_edit(call, state)  # старая кнопка «Изменить анкету» → меню правок
        return
    await call.answer()
    await state.clear()
    await state.set_state(Form.name)
    await ask_below(
        call,
        f"🎄 <b>{GAME_NAME}</b>\n"
        f"💰 Бюджет подарка: <b>{budget_text()}</b>\n"
        f"📅 Обмен подарками: <b>{event_text()}</b>\n\n"
        "📝 <b>Анкета участника</b> (1/4)\n\n"
        "👤 <b>Как тебя зовут?</b>\n"
        "<i>Это имя увидит твой Тайный Санта.</i>",
        CANCEL_KB,
    )


@router.message(Form.name, F.text)
async def form_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name or len(name) > NAME_MAX:
        await message.answer(f"Имя должно быть от 1 до {NAME_MAX} символов 🐸")
        return
    await state.update_data(name=name)
    await state.set_state(Form.wish)
    await message.answer(
        "📝 <b>Анкета участника</b> (2/4)\n\n"
        "🎁 <b>Что бы тебе хотелось получить?</b>\n"
        f"{WISH_HINT}",
        reply_markup=CANCEL_KB,
    )


WISH_HINT = (
    "<i>Можно несколько идей или общее направление.\n"
    f"🖼 Можно приложить фото (до {MAX_PHOTOS}) — картинку с маркетплейса или скриншот. "
    "Текст тогда пиши в подписи к фото.</i>"
)


async def read_wish(message: Message, markup=None) -> tuple[str, list[str]] | None:
    """Пожелание: текст и/или фото. None — если это хвост альбома или ошибка ввода."""
    album = await collect_album(message)
    if album is None:
        return None
    wish, photos = text_and_photos(album)
    if (not wish and not photos) or len(wish) > TEXT_MAX:
        await message.answer(
            f"Напиши хоть что-нибудь или пришли фото (текст — не длиннее {TEXT_MAX} символов) 🐸",
            reply_markup=markup,
        )
        return None
    return wish, photos


@router.message(Form.wish, F.text | F.photo)
async def form_wish(message: Message, state: FSMContext) -> None:
    got = await read_wish(message)
    if not got:
        return
    wish, photos = got
    await state.update_data(wish=wish, wish_photos=json.dumps(photos) if photos else None)
    await state.set_state(Form.location)
    await message.answer(
        "📝 <b>Анкета участника</b> (3/4)\n\n"
        "📍 <b>Где будешь встречать Новый год?</b>",
        reply_markup=LOCATION_KB,
    )


@router.message(Form.location)
async def form_location_text(message: Message) -> None:
    await message.answer("Выбери, пожалуйста, кнопкой 👇", reply_markup=LOCATION_KB)


async def ask_not_wish(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.not_wish)
    await message.answer(
        "📝 <b>Анкета участника</b> (4/4) — <i>необязательно</i>\n\n"
        "🚫 <b>Чего точно НЕ дарить?</b>\n"
        "<i>Например: никаких сладостей, не кружки, аллергия на…</i>",
        reply_markup=SKIP_KB,
    )


@router.callback_query(Form.location, F.data == "loc_party")
async def cb_loc_party(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.update_data(location=LOC_PARTY, delivery=None)
    await call.message.edit_text(
        "📍 <b>Где будешь встречать Новый год?</b>\n🎄 На празднике — получу лично"
    )
    await ask_not_wish(call.message, state)



@router.callback_query(Form.location, F.data == "loc_away")
async def cb_loc_away(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.update_data(location=LOC_AWAY)
    await state.set_state(Form.delivery)
    await call.message.edit_text(
        "📍 <b>Где будешь встречать Новый год?</b>\n✈️ В другом месте — нужна доставка"
    )
    await call.message.answer(
        "✈️ <b>Как тебе удобнее получить подарок?</b>\n"
        "<i>Напиши страну, город и способ: адрес, пункт выдачи, маркетплейс "
        "или электронный подарок (сертификат, подписка). "
        "Это увидит только твой Тайный Санта.</i>",
        reply_markup=CANCEL_KB,
    )



@router.message(Form.delivery, F.text)
async def form_delivery(message: Message, state: FSMContext) -> None:
    delivery = message.text.strip()
    if not delivery or len(delivery) > TEXT_MAX:
        await message.answer(f"Напиши, как тебе отправить подарок (до {TEXT_MAX} символов) 🐸")
        return
    await state.update_data(delivery=delivery)
    await ask_not_wish(message, state)


async def finish_form(message: Message, state: FSMContext, user, bot: Bot) -> None:
    if draw_done():
        await state.clear()
        await message.answer("Ой, жеребьёвка прошла, пока ты заполнял анкету 😿")
        return
    data = await state.get_data()
    is_new = get_participant(user.id) is None
    save_participant(user.id, user.username, data)
    await state.clear()
    p = get_participant(user.id)
    header = "🎉 Ты жабилизировался 🐸" if is_new else "✅ Анкета обновлена!"
    await message.answer(
        f"{header}\n\n{profile_text(p)}\n\n"
        "Когда организатор запустит жеребьёвку, я пришлю тебе подопечного 🎁",
        reply_markup=main_menu(user.id),
    )
    if is_new:
        # Рассылаем в фоне, чтобы новичок не ждал, пока пуш дойдёт до всех
        task = asyncio.create_task(announce_newcomer(bot, user.id, p["name"]))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


_background_tasks: set[asyncio.Task] = set()


async def announce_newcomer(bot: Bot, newcomer_id: int, name: str) -> None:
    """Пуш всем участникам и организаторам (кроме самого новичка): «В болоте пополнение!»."""
    people = all_participants()
    recipients = ({p["user_id"] for p in people} | ADMIN_IDS) - {newcomer_id}
    text = (
        "🎉 <b>В болоте пополнение!</b>\n"
        f"{escape(name)} жабилизировался 🐸\n"
        f"Нас уже <b>{len(people)}</b>"
    )
    markup = kb([("👥 Кто участвует", "people")])
    for uid in recipients:
        await send_safe(bot, uid, text, reply_markup=markup)
        await asyncio.sleep(0.05)


@router.message(Form.not_wish, F.text)
async def form_not_wish(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.update_data(not_wish=message.text.strip()[:TEXT_MAX] or None)
    await finish_form(message, state, message.from_user, bot)


@router.callback_query(Form.not_wish, F.data == "skip")
async def skip_not_wish(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await call.answer()
    await state.update_data(not_wish=None)
    await call.message.edit_reply_markup(reply_markup=None)
    await finish_form(call.message, state, call.from_user, bot)



@router.message(StateFilter(Form))
async def form_not_text(message: Message) -> None:
    await message.answer("Пришли, пожалуйста, текстом 🐸")


# ───────────────────────── Правка анкеты по полям ─────────────────────────

EDIT_MENU_KB = kb(
    [("👤 Изменить имя", "edit_name")],
    [("🎁 Изменить, что хочу", "edit_wish")],
    [("📍 Изменить способ получения", "edit_location")],
    [("🚫 Изменить «что не дарить»", "edit_not_wish")],
    [("⬅️ В меню", "menu")],
)
EDIT_CANCEL_KB = kb([("❌ Отмена", "edit")])
EDIT_LOCATION_KB = kb(
    [("🎄 На празднике с нашими — получу лично", "edit_loc_party")],
    [("✈️ В другом месте — нужна доставка", "edit_loc_away")],
    [("❌ Отмена", "edit")],
)
EDIT_NOT_WISH_KB = kb(
    [("🗑 Убрать — дарите что угодно", "edit_not_wish_clear")],
    [("❌ Отмена", "edit")],
)
CONFIRM_KB = kb([("✅ Сохранить", "edit_save"), ("❌ Отмена", "edit")])

FIELD_TITLES = {
    "name": "👤 Имя",
    "wish": "🎁 Что хочешь получить",
    "not_wish": "🚫 Что точно НЕ дарить",
    "location": "📍 Способ получения",
}


def field_view(field: str, values: dict) -> str:
    if field == "location":
        if values.get("location") == LOC_AWAY:
            d = values.get("delivery")
            return "✈️ доставка" + (f": {escape(d)}" if d else "")
        return "🎄 лично на празднике"
    if field == "wish":
        return wish_view(values.get("wish"), values.get("wish_photos"))
    v = values.get(field)
    return escape(v) if v else "<i>не указано</i>"


async def edit_allowed(call: CallbackQuery, state: FSMContext) -> sqlite3.Row | None:
    """Анкету можно править, пока не было жеребьёвки и человек записан."""
    if draw_done():
        await state.clear()
        await call.answer("Жеребьёвка уже прошла — анкету не поменять 🐸", show_alert=True)
        return None
    p = get_participant(call.from_user.id)
    if not p:
        await state.clear()
        await call.answer("Ты ещё не записан 🐸", show_alert=True)
        return None
    return p


@router.callback_query(F.data == "edit")
async def cb_edit(call: CallbackQuery, state: FSMContext) -> None:
    p = await edit_allowed(call, state)
    if not p:
        return
    await call.answer()
    await state.clear()
    await call.message.edit_text(
        f"✏️ <b>Что поменять в анкете?</b>\n\n{profile_text(p)}",
        reply_markup=EDIT_MENU_KB,
    )


async def start_edit(call: CallbackQuery, state: FSMContext, st: State, question: str, markup) -> None:
    if not await edit_allowed(call, state):
        return
    await call.answer()
    await state.clear()
    await state.set_state(st)
    await call.message.edit_text(question, reply_markup=markup)


@router.callback_query(F.data == "edit_name")
async def cb_edit_name(call: CallbackQuery, state: FSMContext) -> None:
    await start_edit(call, state, Edit.name,
                     "👤 <b>Напиши новое имя</b>\n<i>Это имя увидит твой Тайный Санта.</i>",
                     EDIT_CANCEL_KB)


@router.callback_query(F.data == "edit_wish")
async def cb_edit_wish(call: CallbackQuery, state: FSMContext) -> None:
    await start_edit(call, state, Edit.wish,
                     "🎁 <b>Что бы тебе хотелось получить?</b>\n"
                     "<i>Напиши заново — новое пожелание (текст и фото) заменит старое.</i>\n\n"
                     f"{WISH_HINT}",
                     EDIT_CANCEL_KB)


@router.callback_query(F.data == "edit_not_wish")
async def cb_edit_not_wish(call: CallbackQuery, state: FSMContext) -> None:
    await start_edit(call, state, Edit.not_wish,
                     "🚫 <b>Чего точно НЕ дарить?</b>\n"
                     "<i>Напиши заново или убери этот пункт совсем.</i>",
                     EDIT_NOT_WISH_KB)


@router.callback_query(F.data == "edit_location")
async def cb_edit_location(call: CallbackQuery, state: FSMContext) -> None:
    await start_edit(call, state, Edit.location,
                     "📍 <b>Где будешь встречать Новый год?</b>", EDIT_LOCATION_KB)


async def ask_confirm(message: Message, state: FSMContext, user_id: int, field: str, new: dict) -> None:
    """Показать «было → стало» и ждать подтверждения."""
    p = get_participant(user_id)
    old = dict(p) if p else {}
    await state.set_state(Edit.confirm)
    await state.update_data(new=new)
    await message.answer(
        f"{FIELD_TITLES[field]}\n\n"
        f"<b>Было:</b> {field_view(field, old)}\n"
        f"<b>Стало:</b> {field_view(field, new)}\n\n"
        "Сохранить?",
        reply_markup=CONFIRM_KB,
    )


@router.message(Edit.name, F.text)
async def edit_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name or len(name) > NAME_MAX:
        await message.answer(f"Имя должно быть от 1 до {NAME_MAX} символов 🐸", reply_markup=EDIT_CANCEL_KB)
        return
    await ask_confirm(message, state, message.from_user.id, "name", {"name": name})


@router.message(Edit.wish, F.text | F.photo)
async def edit_wish(message: Message, state: FSMContext) -> None:
    got = await read_wish(message, EDIT_CANCEL_KB)
    if not got:
        return
    wish, photos = got
    await ask_confirm(message, state, message.from_user.id, "wish",
                      {"wish": wish, "wish_photos": json.dumps(photos) if photos else None})


@router.message(Edit.not_wish, F.text)
async def edit_not_wish(message: Message, state: FSMContext) -> None:
    text = message.text.strip()[:TEXT_MAX] or None
    await ask_confirm(message, state, message.from_user.id, "not_wish", {"not_wish": text})


@router.callback_query(Edit.not_wish, F.data == "edit_not_wish_clear")
async def cb_edit_not_wish_clear(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await ask_confirm(call.message, state, call.from_user.id, "not_wish", {"not_wish": None})


@router.callback_query(Edit.location, F.data == "edit_loc_party")
async def cb_edit_loc_party(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await ask_confirm(call.message, state, call.from_user.id, "location",
                      {"location": LOC_PARTY, "delivery": None})


@router.callback_query(Edit.location, F.data == "edit_loc_away")
async def cb_edit_loc_away(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(Edit.delivery)
    await call.message.edit_text(
        "✈️ <b>Как тебе удобнее получить подарок?</b>\n"
        "<i>Напиши страну, город и способ: адрес, пункт выдачи, маркетплейс "
        "или электронный подарок (сертификат, подписка). "
        "Это увидит только твой Тайный Санта.</i>",
        reply_markup=EDIT_CANCEL_KB,
    )


@router.message(Edit.location)
async def edit_location_text(message: Message) -> None:
    await message.answer("Выбери, пожалуйста, кнопкой 👇", reply_markup=EDIT_LOCATION_KB)


@router.message(Edit.delivery, F.text)
async def edit_delivery(message: Message, state: FSMContext) -> None:
    delivery = message.text.strip()
    if not delivery or len(delivery) > TEXT_MAX:
        await message.answer(f"Напиши, как тебе отправить подарок (до {TEXT_MAX} символов) 🐸",
                             reply_markup=EDIT_CANCEL_KB)
        return
    await ask_confirm(message, state, message.from_user.id, "location",
                      {"location": LOC_AWAY, "delivery": delivery})


@router.callback_query(Edit.confirm, F.data == "edit_save")
async def cb_edit_save(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not await edit_allowed(call, state):
        return
    await call.answer("Сохранено ✅")
    update_participant(call.from_user.id, **data["new"])
    await state.clear()
    await call.message.edit_text(
        "✅ <b>Сохранено!</b> Что-нибудь ещё поменять?\n\n"
        f"{profile_text(get_participant(call.from_user.id))}",
        reply_markup=EDIT_MENU_KB,
    )


@router.message(Edit.confirm)
async def edit_confirm_text(message: Message) -> None:
    await message.answer("Нажми «✅ Сохранить» или «❌ Отмена» 👇", reply_markup=CONFIRM_KB)


@router.message(StateFilter(Edit))
async def edit_not_text(message: Message) -> None:
    await message.answer("Пришли, пожалуйста, текстом 🐸", reply_markup=EDIT_CANCEL_KB)


# ───────────────────────── Профиль и подопечный ─────────────────────────

@router.callback_query(F.data == "my_profile")
async def cb_my_profile(call: CallbackQuery) -> None:
    await call.answer()
    p = get_participant(call.from_user.id)
    text = f"📝 <b>Твоя анкета</b>\n\n{profile_text(p)}" if p else "Ты ещё не записан 🐸"
    photos = photos_of(p["wish_photos"]) if p else []
    if photos:
        await call.message.edit_reply_markup(reply_markup=None)
        await send_with_photos(call.bot, call.from_user.id, text, photos, reply_markup=BACK_KB)
    else:
        await call.message.edit_text(text, reply_markup=BACK_KB)



@router.callback_query(F.data == "leave")
async def cb_leave(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(
        "Точно выйти из игры? Анкета удалится 😿",
        reply_markup=kb([("Да, выйти", "leave_yes"), ("Нет, остаюсь", "menu")]),
    )



@router.callback_query(F.data == "leave_yes")
async def cb_leave_yes(call: CallbackQuery) -> None:
    if draw_done():
        await call.answer("После жеребьёвки выйти нельзя — тебя уже ждут с подарком!", show_alert=True)
        return
    delete_participant(call.from_user.id)
    await call.message.edit_text(
        "Ты вышел из игры. Передумаешь — возвращайся! 🐸",
        reply_markup=main_menu(call.from_user.id),
    )
    await call.answer()


@router.callback_query(F.data == "my_receiver")
async def cb_my_receiver(call: CallbackQuery) -> None:
    r = get_receiver(call.from_user.id)
    if not r:
        await call.answer("Жеребьёвки ещё не было 🐸", show_alert=True)
        return
    markup = kb([("❓ Спросить анонимно", "ask")], [("⬅️ Назад", "menu")])
    photos = photos_of(r["wish_photos"])
    if photos:
        await call.message.edit_reply_markup(reply_markup=None)
        await send_with_photos(call.bot, call.from_user.id, receiver_text(r), photos, reply_markup=markup)
    else:
        await call.message.edit_text(receiver_text(r), reply_markup=markup)
    await call.answer()


# ───────────────────────── Анонимные вопросы ─────────────────────────

@router.callback_query(F.data == "ask")
async def cb_ask(call: CallbackQuery, state: FSMContext) -> None:
    r = get_receiver(call.from_user.id)
    if not r:
        await call.answer("Жеребьёвки ещё не было 🐸", show_alert=True)
        return
    await state.set_state(Ask.question)
    await call.message.answer(
        f"❓ <b>Вопрос для {escape(r['name'])}</b>\n"
        "<i>Напиши вопрос — я перешлю его без твоего имени. Можно приложить фото 🖼</i>",
        reply_markup=CANCEL_KB,
    )
    await call.answer()


async def read_anon(message: Message) -> tuple[str, list[str]] | None:
    album = await collect_album(message)
    if album is None:
        return None
    text, photos = text_and_photos(album)
    if not text and not photos:
        return None
    return text[:TEXT_MAX], photos


def anon_text(head: str, text: str, photos: list[str]) -> str:
    """«Санта спрашивает: «…»» — или просто «прислал фото», если текста нет."""
    if text:
        return f"{head}\n\n«{escape(text)}»"
    return f"{head}\n\n🖼 <i>фото</i>"


@router.message(Ask.question, F.text | F.photo)
async def ask_question(message: Message, state: FSMContext, bot: Bot) -> None:
    got = await read_anon(message)
    if not got:
        return
    text, photos = got
    await state.clear()
    r = get_receiver(message.from_user.id)
    if not r:
        await message.answer("Жеребьёвка была сброшена — вопрос не отправлен 🐸")
        return
    ok = await send_with_photos(
        bot, r["user_id"],
        anon_text("🎅 <b>Твой Тайный Санта спрашивает:</b>", text, photos), photos,
        reply_markup=kb([("✉️ Ответить Санте", "reply_santa")]),
    )
    await message.answer(
        "✅ Вопрос отправлен анонимно. Ответ придёт сюда 🐸" if ok
        else "😿 Не получилось отправить — похоже, подопечный заблокировал бота.",
        reply_markup=main_menu(message.from_user.id),
    )


@router.callback_query(F.data == "reply_santa")
async def cb_reply_santa(call: CallbackQuery, state: FSMContext) -> None:
    if not get_santa_id(call.from_user.id):
        await call.answer("Жеребьёвка была сброшена 🐸", show_alert=True)
        return
    await state.set_state(Ask.answer)
    await call.message.answer(
        "✉️ <b>Ответ Санте</b>\n<i>Напиши ответ — я передам его твоему Санте. Можно приложить фото 🖼</i>",
        reply_markup=CANCEL_KB,
    )
    await call.answer()


@router.message(Ask.answer, F.text | F.photo)
async def ask_answer(message: Message, state: FSMContext, bot: Bot) -> None:
    got = await read_anon(message)
    if not got:
        return
    text, photos = got
    await state.clear()
    santa_id = get_santa_id(message.from_user.id)
    if not santa_id:
        await message.answer("Жеребьёвка была сброшена — ответ не отправлен 🐸")
        return
    ok = await send_with_photos(
        bot, santa_id,
        anon_text("📩 <b>Подопечный ответил:</b>", text, photos), photos,
        reply_markup=kb([("❓ Спросить ещё", "ask")]),
    )
    await message.answer(
        "✅ Ответ передан Санте 🎅" if ok else "😿 Не получилось отправить ответ.",
        reply_markup=main_menu(message.from_user.id),
    )


@router.message(StateFilter(Ask))
async def ask_not_text(message: Message) -> None:
    await message.answer("Пришли, пожалуйста, текст или фото 🐸")


# ───────────────────────── Организатор ─────────────────────────

admin_router = Router()
admin_router.message.filter(F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


@admin_router.callback_query(F.data == "admin")
async def cb_admin(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    people = all_participants()
    away = sum(1 for p in people if p["location"] == LOC_AWAY)
    await call.message.edit_text(
        f"👑 <b>Панель организатора</b>\n\n"
        f"Участников: <b>{len(people)}</b>"
        + (f" (✈️ с доставкой: {away})" if away else "")
        + f"\n💰 Бюджет: <b>{budget_text()}</b>\n"
        f"📅 Обмен подарками: <b>{event_text()}</b>\n"
        f"🎲 Жеребьёвка: <b>{'прошла' if draw_done() else 'ещё не было'}</b>",
        reply_markup=admin_menu(),
    )



@admin_router.callback_query(F.data == "admin_budget")
async def cb_admin_budget(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminForm.budget)
    await ask_below(
        call,
        "💰 <b>Какой бюджет подарка?</b>\n"
        "<i>Напиши как есть, например: «до 1500 ₽» или «1000–2000 ₽».</i>",
        kb([("❌ Отмена", "admin")]),
    )



@admin_router.message(AdminForm.budget, F.text)
async def admin_set_budget(message: Message, state: FSMContext) -> None:
    budget = message.text.strip()[:100]
    set_setting("budget", budget)
    await state.clear()
    await message.answer(
        f"✅ Бюджет установлен: <b>{escape(budget)}</b>",
        reply_markup=admin_menu(),
    )


@admin_router.callback_query(F.data == "admin_date")
async def cb_admin_date(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminForm.date)
    await ask_below(
        call,
        "📅 <b>Когда обмен подарками?</b>\n"
        "<i>Напиши дату в формате «31.12.2026».\n"
        "Участникам придёт напоминание за 7 дней и за 1 день.</i>",
        kb([("❌ Отмена", "admin")]),
    )



@admin_router.message(AdminForm.date, F.text)
async def admin_set_date(message: Message, state: FSMContext) -> None:
    dt = parse_event_date(message.text)
    if not dt:
        await message.answer(
            "Не понял дату 🤔 Напиши так: «31.12.2026»",
            reply_markup=kb([("❌ Отмена", "admin")]),
        )
        return
    if dt.date() <= datetime.now().date():
        await message.answer(
            "Эта дата уже прошла 🐸 Напиши дату в будущем.",
            reply_markup=kb([("❌ Отмена", "admin")]),
        )
        return
    set_setting("event_dt", dt.isoformat())
    # Новая дата — напоминания отправятся заново
    for flag in ("reminder_7_sent", "reminder_1_sent",
                 "reminder_7_admin_warned", "reminder_1_admin_warned"):
        set_setting(flag, "0")
    await state.clear()
    await message.answer(
        f"✅ Дата обмена: <b>{event_text()}</b>\n"
        "Напомню участникам за 7 дней и за 1 день.",
        reply_markup=admin_menu(),
    )


@admin_router.callback_query(F.data == "admin_list")
async def cb_admin_list(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(
        participants_list_text(for_admin=True),
        reply_markup=kb([("⬅️ Назад", "admin")]),
    )



@admin_router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminForm.broadcast)
    await ask_below(
        call,
        f"📣 <b>Сообщение всем участникам ({len(all_participants())})</b>\n"
        "<i>Например: «Через неделю праздник — не забудьте подарки!»</i>",
        kb([("❌ Отмена", "admin")]),
    )



@admin_router.message(AdminForm.broadcast, F.text)
async def admin_broadcast(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    people = all_participants()
    text = f"📣 <b>Сообщение от организатора:</b>\n\n{escape(message.text.strip())}"
    sent = 0
    for p in people:
        if await send_safe(bot, p["user_id"], text):
            sent += 1
        await asyncio.sleep(0.05)
    await message.answer(
        f"✅ Отправлено: <b>{sent}</b> из <b>{len(people)}</b>",
        reply_markup=admin_menu(),
    )


@admin_router.callback_query(F.data == "admin_draw")
async def cb_admin_draw(call: CallbackQuery) -> None:
    count = len(all_participants())
    if count < MIN_PARTICIPANTS:
        await call.answer(f"Нужно минимум {MIN_PARTICIPANTS} участника, сейчас {count} 🐸", show_alert=True)
        return
    if not get_setting("budget"):
        await call.answer("Сначала задай бюджет 💰", show_alert=True)
        return
    if not get_event_dt():
        await call.answer("Сначала задай дату обмена 📅", show_alert=True)
        return
    await call.message.edit_text(
        f"🎲 Запускаем жеребьёвку для <b>{count}</b> участников?\n"
        "После этого запись и изменение анкет закроются.",
        reply_markup=kb([("✅ Да, запускаем!", "admin_draw_yes"), ("❌ Нет", "admin")]),
    )
    await call.answer()


@admin_router.callback_query(F.data == "admin_draw_yes")
async def cb_admin_draw_yes(call: CallbackQuery, bot: Bot) -> None:
    if draw_done():
        await call.answer("Жеребьёвка уже прошла", show_alert=True)
        return
    people = {p["user_id"]: p for p in all_participants()}
    if len(people) < MIN_PARTICIPANTS:
        await call.answer("Недостаточно участников", show_alert=True)
        return

    pairs = make_pairs(list(people))
    save_pairs(pairs)

    failed = []
    for santa_id, receiver_id in pairs:
        ok = await send_with_photos(
            bot, santa_id,
            draw_text(people[receiver_id])
            + f"\n\n👇 Подопечного всегда можно посмотреть кнопкой «{WARD_BTN}» внизу",
            photos_of(people[receiver_id]["wish_photos"]),
            reply_markup=WARD_KB,
        )
        if not ok:
            failed.append(people[santa_id]["name"])
        await asyncio.sleep(0.05)

    text = f"✅ Жеребьёвка прошла! Пар: <b>{len(pairs)}</b>."
    if failed:
        text += (
            "\n\n⚠️ Не смог написать: "
            + ", ".join(escape(n) for n in failed)
            + "\nПусть они откроют бота и нажмут «🎁 Кому я дарю?»."
        )
    await call.message.edit_text(text, reply_markup=admin_menu())
    await call.answer()


@admin_router.callback_query(F.data == "admin_reset")
async def cb_admin_reset(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(
        "♻️ Сбросить жеребьёвку? Пары удалятся, запись снова откроется.\n"
        "<i>Всем участникам придёт уведомление, тянуть придётся заново.</i>",
        reply_markup=kb([("Да, сбросить", "admin_reset_yes"), ("❌ Нет", "admin")]),
    )



@admin_router.callback_query(F.data == "admin_reset_yes")
async def cb_admin_reset_yes(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    clear_pairs()
    for p in all_participants():
        await send_safe(
            bot, p["user_id"],
            "♻️ <b>Жеребьёвку перезапустят.</b>\n"
            "Предыдущий подопечный больше не действителен — дождись новой жеребьёвки 🐸",
            reply_markup=main_menu(p["user_id"]),
        )
        await asyncio.sleep(0.05)
    await call.message.edit_text(
        "♻️ Жеребьёвка сброшена, участники уведомлены.", reply_markup=admin_menu()
    )



@router.callback_query(F.data.startswith("admin"))
async def cb_not_admin(call: CallbackQuery) -> None:
    await call.answer("Это только для организатора 👑", show_alert=True)


# ───────────────────────── Напоминания ─────────────────────────

REMINDER_CHECK_SECONDS = 300


def when_text(dt: datetime, now: datetime) -> str:
    days = (dt.date() - now.date()).days
    if days <= 0:
        return "уже сегодня"
    if days == 1:
        return "уже завтра"
    return f"через {days} {days_word(days)}"


def reminder_text(kind: int, r: sqlite3.Row, dt: datetime, now: datetime) -> str:
    days = (dt.date() - now.date()).days
    name = escape(r["name"])
    away = r["location"] == LOC_AWAY
    if kind == 7:
        left = "неделя" if days == 7 else f"{days} {days_word(days)}"
        text = (
            f"🐸 <b>До {GAME_NAME_GEN} {left}!</b>\n"
            f"📅 {event_text()}\n\n"
            f"🎁 Ты даришь: <b>{name}</b>\n"
            f"Самое время выбрать подарок. Бюджет: <b>{budget_text()}</b>"
        )
        if away:
            text += "\n\n✈️ Подопечному нужна доставка — отправь подарок уже сейчас!"
        return text
    head = "Сегодня жабилизируемся!" if days <= 0 else "Завтра жабилизируемся!"
    text = f"🎄 <b>{head}</b>\n📅 {event_text()}\n\n"
    if away:
        text += f"🎁 Ты даришь: <b>{name}</b>\n✈️ Надеемся, подарок уже в пути!"
    else:
        text += f"🎁 Подарок упакован? Ты даришь: <b>{name}</b>\nНе забудь взять его с собой!"
    return text


async def send_reminder(bot: Bot, kind: int, dt: datetime, now: datetime) -> None:
    for p in all_participants():
        uid = p["user_id"]
        r = get_receiver(uid)
        if not r:
            continue
        text = reminder_text(kind, r, dt, now)
        await send_safe(bot, uid, text, reply_markup=WARD_KB)
        await asyncio.sleep(0.05)


async def check_reminders(bot: Bot, now: datetime | None = None) -> None:
    now = now or datetime.now()
    dt = get_event_dt()
    if not dt or now >= dt:
        return
    left = dt - now
    if left <= timedelta(days=1) and get_setting("reminder_1_sent") != "1":
        kind = 1
    elif left <= timedelta(days=7) and get_setting("reminder_7_sent") != "1":
        kind = 7
    else:
        return

    if not draw_done():
        # Напоминать участникам пока не о чем — предупредим организатора (один раз на каждое)
        flag = f"reminder_{kind}_admin_warned"
        if get_setting(flag) != "1":
            for admin_id in ADMIN_IDS:
                await send_safe(
                    bot, admin_id,
                    f"⚠️ Обмен подарками {when_text(dt, now)} ({event_text()}), "
                    "а жеребьёвки ещё не было! Участники получат напоминание после неё.",
                )
            set_setting(flag, "1")
        return

    await send_reminder(bot, kind, dt, now)
    set_setting(f"reminder_{kind}_sent", "1")
    if kind == 1:
        # Недельное уже неактуально — не шлём два напоминания подряд
        set_setting("reminder_7_sent", "1")


async def reminder_loop(bot: Bot) -> None:
    while True:
        try:
            await check_reminders(bot)
        except Exception:
            logging.exception("Ошибка при проверке напоминаний")
        await asyncio.sleep(REMINDER_CHECK_SECONDS)


# ───────────────────────── Запуск ─────────────────────────

async def username_middleware(handler, event, data):
    """При любом сообщении/нажатии освежаем @тег — он нужен Санте в ссылке на профиль."""
    user = getattr(event, "from_user", None)
    if user and not user.is_bot:
        refresh_username(user.id, user.username)
    return await handler(event, data)


def make_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=SQLiteStorage())
    dp.message.outer_middleware(username_middleware)
    dp.callback_query.outer_middleware(username_middleware)
    # Порядок важен: сначала организатор, потом группа, потом все остальные
    dp.include_routers(admin_router, group_router, router)
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN в файле .env")
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS пуст — панель организатора никому не доступна")

    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = make_dispatcher()
    # Команды в синей кнопке «Меню» Telegram
    await bot.set_my_commands(
        [
            BotCommand(command="my", description="🎁 Кому я дарю"),
            BotCommand(command="start", description="📋 Главное меню"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    logging.info("База: %s, часовой пояс: %s", os.path.abspath(DB_PATH), datetime.now().astimezone().tzname())
    tasks = [asyncio.create_task(reminder_loop(bot)), asyncio.create_task(backup_loop())]
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
