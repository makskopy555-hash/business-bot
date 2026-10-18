import asyncio
import html
import logging
import os
import random
import re
import sqlite3
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ─────────────────────────────────────────────
# НАСТРОЙКИ
# ─────────────────────────────────────────────

TOKEN = "8624909808:AAGtF-pSUSRXhwwz2P0mlw0hZq_XtgLBnyg"

ADMIN_IDS = [6541834711]

DATA_DIR = os.getenv("DATA_DIR", ".")  # на Railway укажите /data (Volume)
DB_PATH = os.path.join(DATA_DIR, "business_bot.db")

KEEP_DAYS = 30            # сколько дней хранить кэш сообщений
AWAY_COOLDOWN = 6 * 3600  # автоответчик не чаще раза в 6 часов на один чат
MUTE_NOTICE_COOLDOWN = 20  # «Пользователь в муте» не чаще раза в 20 секунд на один чат

# Тарифы: ключ -> (название, дней). Цены добавите, когда заработает оплата.
TARIFFS = {
    "7": ("7 дней", 7),
    "30": ("1 месяц", 30),
    "90": ("3 месяца", 90),
}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
BOT_USERNAME = ""
font_wait = set()  # кто сейчас в разделе «Шрифты» и ждёт текст
font_fail_notified = set()  # кому уже сообщили, что автошрифт не применился

# ─────────────────────────────────────────────
# БАЗА (sqlite — файл рядом с ботом / на Volume)
# ─────────────────────────────────────────────

os.makedirs(DATA_DIR, exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        sub_until INTEGER DEFAULT 0,
        away_on INTEGER DEFAULT 0,
        away_text TEXT DEFAULT 'Здравствуйте! Сейчас я не на связи, отвечу позже.',
        notify_deleted INTEGER DEFAULT 1,
        notify_edited INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS connections(
        conn_id TEXT PRIMARY KEY,
        owner_id INTEGER,
        enabled INTEGER
    );
    CREATE TABLE IF NOT EXISTS messages(
        conn_id TEXT, chat_id INTEGER, msg_id INTEGER,
        sender_id INTEGER, sender_name TEXT, text TEXT, ts INTEGER,
        PRIMARY KEY(conn_id, chat_id, msg_id)
    );
    CREATE TABLE IF NOT EXISTS mutes(
        conn_id TEXT, chat_id INTEGER, name TEXT, last_notice INTEGER DEFAULT 0,
        PRIMARY KEY(conn_id, chat_id)
    );
    CREATE TABLE IF NOT EXISTS away_log(
        conn_id TEXT, chat_id INTEGER, ts INTEGER,
        PRIMARY KEY(conn_id, chat_id)
    );
    """
)
db.commit()
try:
    db.execute("ALTER TABLE users ADD COLUMN font_idx INTEGER DEFAULT -1")
    db.commit()
except sqlite3.OperationalError:
    pass  # колонка уже есть
try:
    db.execute("ALTER TABLE users ADD COLUMN verified INTEGER DEFAULT 0")
    db.execute("UPDATE users SET verified=1 WHERE sub_until > 0")
    db.commit()
except sqlite3.OperationalError:
    pass  # колонка уже есть


def q(sql: str, args: tuple = ()):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def ensure_user(uid: int) -> sqlite3.Row:
    q("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
    return q("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def sub_left(uid: int) -> int:
    row = ensure_user(uid)
    return max(0, row["sub_until"] - int(time.time()))


def is_active(uid: int) -> bool:
    return is_admin(uid) or sub_left(uid) > 0


def fmt_left(sec: int) -> str:
    days, rem = divmod(sec, 86400)
    hours = rem // 3600
    return f"{days} д. {hours} ч."


def esc(s) -> str:
    return html.escape(str(s or ""))


def person_name(u) -> str:
    if not u:
        return "Неизвестный"
    name = u.full_name or "Без имени"
    return f"{esc(name)} (@{esc(u.username)})" if u.username else esc(name)


def chat_label(chat) -> str:
    return esc(chat.full_name or chat.title or chat.username or chat.id)


def describe(m: Message) -> str:
    """Сохраняем только текст/подпись и тип вложения — сами файлы бот не скачивает."""
    if m.text:
        return m.text
    kinds = [
        ("photo", "📷 фото"), ("video", "🎞 видео"), ("voice", "🎤 голосовое"),
        ("video_note", "⭕ кружок"), ("sticker", "🧩 стикер"), ("document", "📎 файл"),
        ("audio", "🎵 аудио"), ("animation", "GIF"), ("location", "📍 геопозиция"),
        ("contact", "👤 контакт"),
    ]
    for attr, label in kinds:
        if getattr(m, attr, None):
            return label + (f": {m.caption}" if m.caption else "")
    return "[сообщение]"


def owner_of(conn_id: str):
    row = q("SELECT owner_id, enabled FROM connections WHERE conn_id=?", (conn_id,)).fetchone()
    return row["owner_id"] if row and row["enabled"] else None


async def resolve_owner(conn_id: str):
    owner = owner_of(conn_id)
    if owner:
        return owner
    try:
        conn = await bot.get_business_connection(conn_id)
        q("INSERT OR REPLACE INTO connections VALUES(?,?,?)", (conn.id, conn.user.id, int(conn.is_enabled)))
        return conn.user.id if conn.is_enabled else None
    except Exception:
        return None


async def safe_edit(cb: CallbackQuery, text: str, kb=None):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass  # "message is not modified" при двойном клике — не страшно
    await cb.answer()


# ─────────────────────────────────────────────
# ЭКРАНЫ БОТА
# ─────────────────────────────────────────────

ABOUT = (
    "🕵️ <b>Business Assistant</b>\n\n"
    "Умный помощник для вашего Telegram. Подключается через официальную функцию "
    "<b>Telegram Business</b> и работает только в тех чатах, "
    "которые вы сами выберете.\n\n"
    "<b>Что умеет:</b>\n"
    "🗑 сообщает, когда собеседник удалил сообщение, и показывает его текст\n"
    "✏️ показывает, как собеседник изменил сообщение (было → стало)\n"
    "🤖 автоответчик, когда вы не на связи\n"
    "🔇 мут собеседников: бот отвечает в чате «Пользователь в муте»\n"
    "🔤 красивые шрифты для текста (доступны всем)\n"
    "🔎 поиск по сохранённым сообщениям\n\n"
    "🔒 Уведомления видите только вы. Бот хранит текст сообщений "
    f"{KEEP_DAYS} дней, фото/видео/файлы не скачивает."
)


def tariffs_kb():
    kb = InlineKeyboardBuilder()
    for key, (title, _) in TARIFFS.items():
        kb.button(text=f"💳 {title}", callback_data=f"buy_{key}")
    kb.button(text="🔤 Шрифты", callback_data="fonts")
    kb.adjust(1)
    return kb.as_markup()


def cabinet_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⚙️ Настройки", callback_data="settings")
    kb.button(text="🔌 Как подключить", callback_data="howto")
    kb.button(text="📊 Статистика", callback_data="stats")
    kb.button(text="🔇 Мут", callback_data="mute_menu")
    kb.button(text="🔤 Шрифты", callback_data="fonts")
    kb.button(text="💳 Тарифы", callback_data="tariffs")
    kb.adjust(2, 2, 2)
    return kb.as_markup()


def back_kb(target="cabinet"):
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=target)
    return kb.as_markup()


def cabinet_text(uid: int) -> str:
    if is_admin(uid):
        status = "♾ администратор"
    else:
        status = f"осталось {fmt_left(sub_left(uid))}"
    return f"🕵️ <b>Business Assistant</b>\n\n✅ Подписка активна: <b>{status}</b>\n\nВыберите раздел:"


# ─────────────────────────────────────────────
# ПРОВЕРКА НА БОТА (капча при /start)
# ─────────────────────────────────────────────

CAPTCHA_STEPS = 3
CAPTCHA_STEP_TIME = 60    # секунд на каждый шаг
CAPTCHA_MIN_DELAY = 1.0   # ответ быстрее секунды — так отвечает скрипт, а не человек
CAPTCHA_MAX_FAILS = 3     # ошибок до блокировки
CAPTCHA_LOCK = 600        # блокировка на 10 минут

captcha = {}        # user_id -> текущая проверка
captcha_fails = {}  # user_id -> число ошибок (не сбрасывается повторным /start)
captcha_lock = {}   # user_id -> время, до которого проверка заблокирована

_POOLS = [
    ["🍎", "🍌", "🍇", "🍓", "🍉", "🍒", "🍑", "🥝"],
    ["🐶", "🐱", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼"],
    ["🚗", "🚕", "🚌", "🚓", "🚑", "🚒", "🚜", "🚲"],
    ["⚽", "🏀", "🏈", "⚾", "🎾", "🏐", "🏉", "🎱"],
]
_WORDS = ["телефон", "сообщение", "ключ", "планета", "программа", "бот", "клавиатура", "дорога", "облако", "мышь"]


def _numeric_options(ans: int, extra: list):
    pool = [x for x in dict.fromkeys(extra + [ans + 1, ans - 1, ans + 2, ans - 2, ans + 3]) if x != ans and x > 0]
    opts = [ans] + pool[:3]
    random.shuffle(opts)
    return [str(o) for o in opts], opts.index(ans)


def _puzzle_math():
    if random.random() < 0.5:
        a, b, c = random.randint(3, 9), random.randint(2, 9), random.randint(2, 5)
        ans = a + b * c
        opts, idx = _numeric_options(ans, [(a + b) * c])  # типичная ошибка: считать слева направо
        return f"Сколько будет <b>{a} + {b} × {c}</b>?", opts, idx, 2
    a, b = random.randint(3, 9), random.randint(2, 6)
    c = random.randint(1, min(9, a * b - 1))
    ans = a * b - c
    opts, idx = _numeric_options(ans, [a * (b - c)])
    return f"Сколько будет <b>{a} × {b} − {c}</b>?", opts, idx, 2


def _puzzle_odd():
    main, other = random.sample(_POOLS, 2)
    items = random.sample(main, 5)
    odd = random.choice(other)
    opts = items + [odd]
    random.shuffle(opts)
    return "Нажмите на <b>лишний</b> предмет:", opts, opts.index(odd), 3


def _puzzle_word():
    w = random.choice(_WORDS)
    opts, idx = _numeric_options(len(w), [])
    return f"Сколько букв в слове <b>«{w}»</b>?", opts, idx, 2


_PUZZLES = [_puzzle_math, _puzzle_odd, _puzzle_word]


def _next_puzzle(sess: dict) -> None:
    text, opts, correct, cols = _PUZZLES[sess["step"] - 1]()
    sess.update(
        text=text, opts=opts, correct=correct, cols=cols,
        nonce="".join(random.choices("0123456789abcdef", k=6)),
        shown=time.time(),
    )


def _captcha_view(sess: dict, note: str = ""):
    kb = InlineKeyboardBuilder()
    for i, o in enumerate(sess["opts"]):
        # в callback_data только номер кнопки и одноразовый код, сам ответ там не виден
        kb.button(text=o, callback_data=f"cap_{sess['nonce']}_{i}")
    kb.adjust(sess["cols"])
    text = (
        f"🛡 <b>Проверка на бота</b> — шаг {sess['step']}/{CAPTCHA_STEPS}\n\n"
        f"{note}{sess['text']}\n\n"
        f"⏱ На ответ {CAPTCHA_STEP_TIME} сек. Ошибок осталось: {CAPTCHA_MAX_FAILS - sess['fails']}"
    )
    return text, kb.as_markup()


async def begin_captcha(message: Message):
    uid = message.from_user.id
    left = captcha_lock.get(uid, 0) - time.time()
    if left > 0:
        await message.answer(f"🚫 Слишком много ошибок. Повторите через {int(left // 60) + 1} мин.")
        return
    sess = {"step": 1, "fails": captcha_fails.get(uid, 0)}
    captcha[uid] = sess
    _next_puzzle(sess)
    text, kb = _captcha_view(sess)
    await message.answer(text, reply_markup=kb)


def needs_captcha(uid: int) -> bool:
    return not is_admin(uid) and not ensure_user(uid)["verified"]


@dp.callback_query(F.data.startswith("cap_"))
async def captcha_answer(cb: CallbackQuery):
    uid = cb.from_user.id
    try:
        _, nonce, idx_s = cb.data.split("_")
        idx = int(idx_s)
    except ValueError:
        await cb.answer()
        return
    sess = captcha.get(uid)
    if not sess or sess["nonce"] != nonce:
        await cb.answer("Эта проверка устарела. Нажмите /start", show_alert=True)
        return
    now = time.time()
    if now - sess["shown"] > CAPTCHA_STEP_TIME:
        captcha.pop(uid, None)
        await cb.answer("Время вышло. Нажмите /start, чтобы начать заново.", show_alert=True)
        return

    if idx == sess["correct"] and now - sess["shown"] >= CAPTCHA_MIN_DELAY:
        if sess["step"] >= CAPTCHA_STEPS:
            captcha.pop(uid, None)
            captcha_fails.pop(uid, None)
            ensure_user(uid)
            q("UPDATE users SET verified=1 WHERE user_id=?", (uid,))
            kb = InlineKeyboardBuilder()
            kb.button(text="🚀 Начать", callback_data="begin")
            await safe_edit(cb, "✅ <b>Проверка пройдена!</b>\n\n" + ABOUT, kb.as_markup())
            return
        sess["step"] += 1
        _next_puzzle(sess)
        text, kb = _captcha_view(sess, "✅ Верно!\n\n")
        await safe_edit(cb, text, kb)
        return

    # ошибка (или слишком быстрый ответ)
    fails = captcha_fails.get(uid, 0) + 1
    captcha_fails[uid] = fails
    if fails >= CAPTCHA_MAX_FAILS:
        captcha.pop(uid, None)
        captcha_fails.pop(uid, None)
        captcha_lock[uid] = now + CAPTCHA_LOCK
        try:
            await cb.message.edit_text(f"🚫 Слишком много ошибок. Повторите через {CAPTCHA_LOCK // 60} мин.")
        except TelegramBadRequest:
            pass
        await cb.answer("Проверка не пройдена", show_alert=True)
        return
    sess["fails"] = fails
    sess["step"] = 1
    _next_puzzle(sess)
    text, kb = _captcha_view(sess, "❌ Неверно, начинаем заново.\n\n")
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await cb.answer("❌ Неверно")


@dp.message(CommandStart())
async def start(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    if needs_captcha(uid):
        await begin_captcha(message)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Начать", callback_data="begin")
    await message.answer(ABOUT, reply_markup=kb.as_markup())


@dp.message(Command("menu"))
async def menu_cmd(message: Message):
    if needs_captcha(message.from_user.id):
        await begin_captcha(message)
        return
    if is_active(message.from_user.id):
        await message.answer(cabinet_text(message.from_user.id), reply_markup=cabinet_kb())
    else:
        await message.answer("Выберите тариф:", reply_markup=tariffs_kb())


@dp.callback_query(F.data == "begin")
async def begin(cb: CallbackQuery):
    font_wait.discard(cb.from_user.id)
    if is_active(cb.from_user.id):
        await safe_edit(cb, cabinet_text(cb.from_user.id), cabinet_kb())
    else:
        await safe_edit(cb, "💳 <b>Выберите тариф</b>\n\nДоступ ко всем функциям бота:", tariffs_kb())


@dp.callback_query(F.data == "tariffs")
async def tariffs(cb: CallbackQuery):
    kb = InlineKeyboardBuilder()
    for key, (title, _) in TARIFFS.items():
        kb.button(text=f"💳 {title}", callback_data=f"buy_{key}")
    kb.button(text="⬅️ Назад", callback_data="cabinet")
    kb.adjust(1)
    await safe_edit(cb, "💳 <b>Тарифы</b>\n\nПродлить доступ:", kb.as_markup())


@dp.callback_query(F.data.startswith("buy_"))
async def buy(cb: CallbackQuery):
    key = cb.data.split("_", 1)[1]
    title = TARIFFS.get(key, ("тариф", 0))[0]
    back = "cabinet" if is_active(cb.from_user.id) else "begin"
    await safe_edit(
        cb,
        f"💳 <b>Тариф: {esc(title)}</b>\n\n"
        "❌ Оплата пока недоступна.\n"
        "Мы сообщим, когда приём платежей заработает.",
        back_kb(back),
    )


@dp.callback_query(F.data == "cabinet")
async def cabinet(cb: CallbackQuery):
    font_wait.discard(cb.from_user.id)
    if not is_active(cb.from_user.id):
        await safe_edit(cb, "💳 <b>Выберите тариф</b>", tariffs_kb())
        return
    await safe_edit(cb, cabinet_text(cb.from_user.id), cabinet_kb())


def require_active(cb: CallbackQuery) -> bool:
    return is_active(cb.from_user.id)


@dp.callback_query(F.data == "howto")
async def howto(cb: CallbackQuery):
    if not require_active(cb):
        await cb.answer("Нужна активная подписка", show_alert=True)
        return
    await safe_edit(
        cb,
        "🔌 <b>Как подключить бота</b>\n\n"
        "1. Откройте <b>Настройки Telegram → Telegram для бизнеса → Чат-боты</b>\n"
        f"2. Укажите бота: <b>@{esc(BOT_USERNAME)}</b>\n"
        "3. Выберите, в каких чатах он может работать\n"
        "4. Включите право «Отвечать на сообщения» (нужно для автоответчика)\n\n"
        "Готово — бот пришлёт подтверждение. Отключить можно там же в любой момент.\n\n"
        "ℹ️ Если в настройках нет раздела «Telegram для бизнеса» — обновите Telegram до последней версии.",
        back_kb(),
    )


@dp.callback_query(F.data == "stats")
async def stats(cb: CallbackQuery):
    if not require_active(cb):
        await cb.answer("Нужна активная подписка", show_alert=True)
        return
    conns = [r["conn_id"] for r in q("SELECT conn_id FROM connections WHERE owner_id=?", (cb.from_user.id,))]
    total = chats = 0
    for c in conns:
        total += q("SELECT COUNT(*) n FROM messages WHERE conn_id=?", (c,)).fetchone()["n"]
        chats += q("SELECT COUNT(DISTINCT chat_id) n FROM messages WHERE conn_id=?", (c,)).fetchone()["n"]
    await safe_edit(
        cb,
        "📊 <b>Статистика</b>\n\n"
        f"💬 Сообщений в кэше: <b>{total}</b>\n"
        f"👥 Чатов: <b>{chats}</b>\n"
        f"🕒 Хранение: {KEEP_DAYS} дней",
        back_kb(),
    )


def settings_kb(uid: int):
    u = ensure_user(uid)
    mark = lambda v: "✅" if v else "❌"
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{mark(u['notify_deleted'])} Удалённые сообщения", callback_data="tg_deleted")
    kb.button(text=f"{mark(u['notify_edited'])} Изменённые сообщения", callback_data="tg_edited")
    kb.button(text=f"{mark(u['away_on'])} Автоответчик", callback_data="tg_away")
    kb.button(text="⬅️ Назад", callback_data="cabinet")
    kb.adjust(1)
    return kb.as_markup()


def settings_text(uid: int) -> str:
    u = ensure_user(uid)
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"Текст автоответчика:\n<i>{esc(u['away_text'])}</i>\n\n"
        "Изменить текст: <code>/setaway ваш текст</code>"
    )


@dp.callback_query(F.data == "settings")
async def settings(cb: CallbackQuery):
    if not require_active(cb):
        await cb.answer("Нужна активная подписка", show_alert=True)
        return
    await safe_edit(cb, settings_text(cb.from_user.id), settings_kb(cb.from_user.id))


@dp.callback_query(F.data.in_({"tg_deleted", "tg_edited", "tg_away"}))
async def toggle(cb: CallbackQuery):
    if not require_active(cb):
        await cb.answer("Нужна активная подписка", show_alert=True)
        return
    col = {"tg_deleted": "notify_deleted", "tg_edited": "notify_edited", "tg_away": "away_on"}[cb.data]
    ensure_user(cb.from_user.id)
    q(f"UPDATE users SET {col} = 1 - {col} WHERE user_id=?", (cb.from_user.id,))
    await safe_edit(cb, settings_text(cb.from_user.id), settings_kb(cb.from_user.id))


@dp.message(Command("setaway"))
async def set_away(message: Message):
    if not is_active(message.from_user.id):
        await message.answer("Нужна активная подписка. Нажмите /start")
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.answer("Использование: /setaway текст автоответа")
        return
    ensure_user(message.from_user.id)
    q("UPDATE users SET away_text=? WHERE user_id=?", (text[:500], message.from_user.id))
    await message.answer("✅ Текст автоответчика обновлён.")


@dp.message(Command("find"))
async def find(message: Message):
    uid = message.from_user.id
    if not is_active(uid):
        await message.answer("Нужна активная подписка. Нажмите /start")
        return
    word = message.text.partition(" ")[2].strip()
    if len(word) < 2:
        await message.answer("Использование: /find слово")
        return
    conns = [r["conn_id"] for r in q("SELECT conn_id FROM connections WHERE owner_id=?", (uid,))]
    lines = []
    for c in conns:
        rows = q(
            "SELECT sender_name, text, ts FROM messages WHERE conn_id=? AND text LIKE ? ORDER BY ts DESC LIMIT 10",
            (c, f"%{word}%"),
        ).fetchall()
        for r in rows:
            when = time.strftime("%d.%m %H:%M", time.localtime(r["ts"]))
            lines.append(f"{when} — <b>{esc(r['sender_name'])}</b>: {esc(r['text'])[:200]}")
    await message.answer("\n".join(lines[:15]) if lines else "Ничего не найдено.")


# ─────────────────────────────────────────────
# ШРИФТЫ
# ─────────────────────────────────────────────

_LAT = "abcdefghijklmnopqrstuvwxyz"
_LAT_UP = _LAT.upper()
FONT_MAX = 80


def _offset_map(upper_start, lower_start, digit_start=None, exceptions=None):
    m = {}
    for i, ch in enumerate(_LAT_UP):
        m[ch] = chr(upper_start + i)
    for i, ch in enumerate(_LAT):
        m[ch] = chr(lower_start + i)
    if digit_start:
        for i, ch in enumerate("0123456789"):
            m[ch] = chr(digit_start + i)
    if exceptions:
        m.update(exceptions)
    return m


def _custom_map(lat_target, cyr, fold=True, overrides=None):
    assert len(lat_target) == 26, "в шрифте должно быть ровно 26 латинских букв"
    m = {}
    for a, b in zip(_LAT, lat_target):
        m[a] = b
        if fold:
            m[a.upper()] = b
    for a, b in cyr.items():
        m[a] = b
        if fold:
            m[a.upper()] = b
    if overrides:
        m.update(overrides)
    return m


_CIRCLED = _offset_map(0x24B6, 0x24D0)
_CIRCLED.update({str(i): chr(0x2460 + i - 1) for i in range(1, 10)})
_CIRCLED["0"] = "\u24ea"

FONT_STYLES = [
    ("Жирный", _offset_map(0x1D400, 0x1D41A, 0x1D7CE)),
    ("Курсив", _offset_map(0x1D608, 0x1D622)),
    ("Жирный курсив", _offset_map(0x1D63C, 0x1D656)),
    ("Моноширинный", _offset_map(0x1D670, 0x1D68A, 0x1D7F6)),
    ("Двойной", _offset_map(
        0x1D538, 0x1D552, 0x1D7D8,
        {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"},
    )),
    ("Рукописный", _offset_map(0x1D4D0, 0x1D4EA)),
    ("Готика", _offset_map(0x1D56C, 0x1D586)),
    ("В кружках", _CIRCLED),
    ("ξα Греческий", _custom_map(
        "αβϲԁεƒɡɦιʝκɭϻηορɋгѕτυνω𑀌γζ",
        {"т": "τ", "е": "ε", "к": "ӄ", "с": "ϲ", "а": "α", "о": "ο", "р": "ρ",
         "и": "ι", "н": "η", "в": "β", "м": "ϻ", "х": "χ", "ё": "ε"},
        overrides={"E": "ξ", "Т": "Ͳ", "Е": "ξ"},
    )),
    ("ϱχ Греческий 2", _custom_map(
        "αβϲԁϱƒɡɦιʝκɭɱηορɋгѕτυνωχγζ",
        {"т": "τ", "е": "ϱ", "к": "κ", "с": "с", "а": "α", "о": "ο", "р": "ρ",
         "и": "ι", "н": "η", "в": "β", "м": "ɱ", "х": "χ", "ё": "ϱ"},
    )),
    ("ᘓ᙭ Канадский", _custom_map(
        "ᕕᗷᑕᗪᘓᖴᘜᕼᏆᒍᖉᒐᕬᑎᗝᖘᑫᖇᔕᎢᑌᐯᗯ᙭Ꭹᘔ",
        {"т": "Ꭲ", "е": "ᘓ", "к": "ᖉ", "с": "ᑕ", "а": "ᕕ", "в": "ᗷ", "м": "ᕬ",
         "н": "ᕼ", "о": "ᗝ", "р": "ᖘ", "х": "᙭", "у": "Ꭹ", "ё": "ᘓ"},
    )),
    ("ᴀʙᴄ Малые капсы", _custom_map(
        "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋlʍɴᴏᴩǫʀꜱᴛᴜᴠᴡxʏᴢ",
        {"е": "ᴇ", "к": "ᴋ", "с": "ᴄ", "т": "ᴛ", "а": "ᴀ", "о": "ᴏ", "р": "ᴩ",
         "м": "ʍ", "н": "ʜ", "в": "ʙ"},
        fold=False,
    )),
]


def stylize(mapping, text: str) -> str:
    return "".join(mapping.get(ch, ch) for ch in text)


def fonts_text(text: str) -> str:
    text = text.strip()[:FONT_MAX]
    parts = ["🔤 <b>Ваш текст в шрифтах</b>\n(нажмите на вариант, чтобы скопировать)\n"]
    for title, mp in FONT_STYLES:
        parts.append(f"\n<b>{esc(title)}</b>\n<code>{esc(stylize(mp, text))}</code>")
    return "".join(parts)


_SKIP_PREFIX = ("http://", "https://", "www.", "#", "/", "$")
_ALLOWED_ENT = {"url", "mention", "hashtag", "cashtag", "bot_command", "email", "phone_number"}


def stylize_smart(mapping, text: str) -> str:
    # ссылки, @упоминания, #теги и команды не трогаем, чтобы они не сломались
    out = []
    for p in re.split(r"(\s+)", text):
        if not p or p.isspace() or p.lower().startswith(_SKIP_PREFIX) or "://" in p or "@" in p:
            out.append(p)
        else:
            out.append(stylize(mapping, p))
    return "".join(out)


def entities_ok(m: Message) -> bool:
    # сообщения с жирным/курсивом/ссылками-текстом и т.п. пропускаем: при правке форматирование бы потерялось
    return all(e.type in _ALLOWED_ENT for e in (m.entities or []))


def current_font(uid: int) -> int:
    idx = ensure_user(uid)["font_idx"]
    return idx if idx is not None and 0 <= idx < len(FONT_STYLES) else -1


def fonts_screen_text(uid: int) -> str:
    idx = current_font(uid)
    now = esc(FONT_STYLES[idx][0]) if idx >= 0 else "выключен"
    return (
        "🔤 <b>Шрифты</b>\n\n"
        f"<b>Автошрифт сейчас: {now}</b>\n"
        "Выберите шрифт ниже — и все ваши сообщения в подключённых чатах бот будет автоматически "
        "переписывать этим шрифтом.\n\n"
        "ℹ️ Как это работает: бот правит ваше сообщение после отправки, поэтому у него появится пометка "
        "«изменено», а собеседник может на миг увидеть оригинал. Ссылки, @упоминания и команды не меняются, "
        "сообщения с форматированием пропускаются. Нужна подписка и подключённый бот.\n\n"
        f"Просто перевести текст: отправьте его сюда (до {FONT_MAX} символов) — пришлю во всех шрифтах. "
        f"Без захода в бота: <code>@{esc(BOT_USERNAME)} ваш текст</code> в любом чате."
    )


def fonts_screen_kb(uid: int):
    idx = current_font(uid)
    kb = InlineKeyboardBuilder()
    for i, (title, _) in enumerate(FONT_STYLES):
        kb.button(text=("✅ " if i == idx else "") + title, callback_data=f"fs_{i}")
    kb.button(text="❌ Выключить автошрифт", callback_data="fs_off")
    kb.button(text="⬅️ Назад", callback_data="cabinet")
    kb.adjust(*([2] * ((len(FONT_STYLES) + 1) // 2)), 1, 1)
    return kb.as_markup()


@dp.callback_query(F.data == "fonts")
async def fonts(cb: CallbackQuery):
    font_wait.add(cb.from_user.id)
    await safe_edit(cb, fonts_screen_text(cb.from_user.id), fonts_screen_kb(cb.from_user.id))


@dp.callback_query(F.data.startswith("fs_"))
async def font_set(cb: CallbackQuery):
    if not require_active(cb):
        await cb.answer("Автошрифт доступен с подпиской", show_alert=True)
        return
    val = cb.data[3:]
    idx = -1
    if val != "off":
        try:
            idx = int(val)
        except ValueError:
            idx = -1
        if not 0 <= idx < len(FONT_STYLES):
            idx = -1
    ensure_user(cb.from_user.id)
    q("UPDATE users SET font_idx=? WHERE user_id=?", (idx, cb.from_user.id))
    font_fail_notified.discard(cb.from_user.id)
    await safe_edit(cb, fonts_screen_text(cb.from_user.id), fonts_screen_kb(cb.from_user.id))


@dp.message(Command("font"))
async def font_cmd(message: Message):
    text = message.text.partition(" ")[2].strip()
    if not text:
        font_wait.add(message.from_user.id)
        await message.answer(
            fonts_screen_text(message.from_user.id),
            reply_markup=fonts_screen_kb(message.from_user.id),
        )
        return
    await message.answer(fonts_text(text), reply_markup=back_kb())


@dp.message(F.text, F.chat.type == "private", ~F.text.startswith("/"))
async def font_plain(message: Message):
    if message.from_user.id in font_wait:
        await message.answer(fonts_text(message.text), reply_markup=back_kb())


@dp.inline_query()
async def inline_fonts(iq: InlineQuery):
    text = iq.query.strip()[:FONT_MAX]
    if not text:
        await iq.answer([], cache_time=1)
        return
    results = []
    for i, (title, mp) in enumerate(FONT_STYLES):
        styled = stylize(mp, text)
        results.append(
            InlineQueryResultArticle(
                id=str(i),
                title=title,
                description=styled,
                input_message_content=InputTextMessageContent(message_text=styled, parse_mode=None),
            )
        )
    await iq.answer(results, cache_time=1, is_personal=False)


# ─────────────────────────────────────────────
# МУТ
# ─────────────────────────────────────────────

def owner_conns(uid: int) -> list:
    return [r["conn_id"] for r in q("SELECT conn_id FROM connections WHERE owner_id=?", (uid,))]


def muted_list(uid: int) -> list:
    conns = owner_conns(uid)
    if not conns:
        return []
    marks = ",".join("?" * len(conns))
    return q(
        f"SELECT conn_id, chat_id, name FROM mutes WHERE conn_id IN ({marks}) ORDER BY rowid DESC",
        tuple(conns),
    ).fetchall()


def mute_screen(uid: int):
    rows = muted_list(uid)
    kb = InlineKeyboardBuilder()
    for r in rows[:15]:
        kb.button(text=f"🔊 Размут: {r['name'][:25]}", callback_data=f"um_{r['chat_id']}")
    kb.button(text="➕ Замутить собеседника", callback_data="mute_pick")
    kb.button(text="⬅️ Назад", callback_data="cabinet")
    kb.adjust(1)
    listing = "\n".join(f"• {esc(r['name'])}" for r in rows) if rows else "никого"
    text = (
        "🔇 <b>Мут</b>\n\n"
        "Замученный собеседник по-прежнему может вам писать, но на его сообщения бот отвечает "
        "в чате: «Пользователь в муте» с кнопкой «Размут». Нажать её может только вы.\n\n"
        "Как замутить:\n"
        "• кнопкой «➕ Замутить собеседника» ниже\n"
        "• или командой <code>/mute</code> прямо в чате с человеком "
        "(<code>/unmute</code> — снять). Саму команду собеседник увидит, удалите её сами.\n\n"
        f"<b>В муте сейчас:</b>\n{listing}"
    )
    return text, kb.as_markup()


@dp.callback_query(F.data == "mute_menu")
async def mute_menu(cb: CallbackQuery):
    if not require_active(cb):
        await cb.answer("Нужна активная подписка", show_alert=True)
        return
    text, kb = mute_screen(cb.from_user.id)
    await safe_edit(cb, text, kb)


@dp.callback_query(F.data == "mute_pick")
async def mute_pick(cb: CallbackQuery):
    if not require_active(cb):
        await cb.answer("Нужна активная подписка", show_alert=True)
        return
    uid = cb.from_user.id
    conns = owner_conns(uid)
    if not conns:
        await safe_edit(
            cb,
            "Сначала подключите бота в Telegram Business (раздел «Как подключить»).",
            back_kb("mute_menu"),
        )
        return
    marks = ",".join("?" * len(conns))
    rows = q(
        f"SELECT chat_id, sender_name, MAX(ts) AS t FROM messages "
        f"WHERE conn_id IN ({marks}) AND sender_id != ? "
        f"AND chat_id NOT IN (SELECT chat_id FROM mutes WHERE conn_id IN ({marks})) "
        f"GROUP BY chat_id ORDER BY t DESC LIMIT 8",
        (*conns, uid, *conns),
    ).fetchall()
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"🔇 {r['sender_name'][:30]}", callback_data=f"md_{r['chat_id']}")
    kb.button(text="⬅️ Назад", callback_data="mute_menu")
    kb.adjust(1)
    text = (
        "Выберите, кого замутить (последние собеседники):"
        if rows else
        "Пока нет сообщений от собеседников. Как только вам напишут, они появятся здесь."
    )
    await safe_edit(cb, text, kb.as_markup())


@dp.callback_query(F.data.startswith("md_"))
async def mute_do(cb: CallbackQuery):
    if not require_active(cb):
        await cb.answer("Нужна активная подписка", show_alert=True)
        return
    uid = cb.from_user.id
    chat_id = int(cb.data[3:])
    conns = owner_conns(uid)
    row = None
    if conns:
        marks = ",".join("?" * len(conns))
        row = q(
            f"SELECT conn_id, sender_name FROM messages WHERE chat_id=? AND sender_id != ? "
            f"AND conn_id IN ({marks}) ORDER BY ts DESC LIMIT 1",
            (chat_id, uid, *conns),
        ).fetchone()
    if not row:
        await cb.answer("Собеседник не найден", show_alert=True)
        return
    q("INSERT OR REPLACE INTO mutes VALUES(?,?,?,0)", (row["conn_id"], chat_id, row["sender_name"]))
    text, kb = mute_screen(uid)
    await safe_edit(cb, text, kb)


@dp.callback_query(F.data.startswith("um_"))
async def unmute(cb: CallbackQuery):
    chat_id = int(cb.data[3:])
    conns = owner_conns(cb.from_user.id)
    if not conns:
        await cb.answer("Размутить может только владелец чата", show_alert=True)
        return
    marks = ",".join("?" * len(conns))
    cur = q(f"DELETE FROM mutes WHERE chat_id=? AND conn_id IN ({marks})", (chat_id, *conns))
    if cur.rowcount == 0:
        await cb.answer("Мут уже снят или это не ваш чат", show_alert=True)
        return
    if cb.message is None:
        await cb.answer("Мут снят")
        return
    if getattr(cb.message, "business_connection_id", None):
        try:
            await cb.message.edit_text("🔊 Мут снят")
        except Exception:
            pass
        await cb.answer("Мут снят")
    else:
        text, kb = mute_screen(cb.from_user.id)
        await safe_edit(cb, text, kb)


# ─────────────────────────────────────────────
# BUSINESS: подключение, сообщения, удаления, правки
# ─────────────────────────────────────────────

@dp.business_connection()
async def on_connection(conn: BusinessConnection):
    q(
        "INSERT OR REPLACE INTO connections VALUES(?,?,?)",
        (conn.id, conn.user.id, int(conn.is_enabled)),
    )
    ensure_user(conn.user.id)
    if conn.is_enabled:
        text = "✅ Бот подключён к вашему аккаунту."
        if not is_active(conn.user.id):
            text += "\n\n⚠️ Подписка не активна — функции заработают после оформления тарифа (/start)."
    else:
        text = "⚠️ Бот отключён от вашего аккаунта."
    try:
        await bot.send_message(conn.user_chat_id, text)
    except Exception:
        pass


@dp.business_message()
async def on_business_message(m: Message):
    owner = await resolve_owner(m.business_connection_id)
    if not owner or not is_active(owner):
        return

    sender = m.from_user
    conn = m.business_connection_id

    # команды владельца прямо в чате: /mute и /unmute
    if sender and sender.id == owner and m.text:
        cmd = m.text.split()[0].split("@")[0].lower()
        if cmd in ("/mute", "/unmute"):
            name = m.chat.full_name or m.chat.username or str(m.chat.id)
            if cmd == "/mute":
                q("INSERT OR REPLACE INTO mutes VALUES(?,?,?,0)", (conn, m.chat.id, name))
                note = f"🔇 {esc(name)} в муте."
            else:
                q("DELETE FROM mutes WHERE conn_id=? AND chat_id=?", (conn, m.chat.id))
                note = f"🔊 Мут с {esc(name)} снят."
            try:
                await bot.send_message(owner, note)
            except Exception:
                pass
            return

    q(
        "INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?)",
        (
            m.business_connection_id, m.chat.id, m.message_id,
            sender.id if sender else 0,
            sender.full_name if sender else "Неизвестный",
            describe(m), int(time.time()),
        ),
    )

    # мут: на сообщения замученного бот отвечает в чате и дальше ничего не делает
    if sender and sender.id != owner:
        muted = q(
            "SELECT last_notice FROM mutes WHERE conn_id=? AND chat_id=?", (conn, m.chat.id)
        ).fetchone()
        if muted:
            if time.time() - muted["last_notice"] > MUTE_NOTICE_COOLDOWN:
                kb = InlineKeyboardBuilder()
                kb.button(text="🔊 Размут", callback_data=f"um_{m.chat.id}")
                try:
                    await bot.send_message(
                        m.chat.id,
                        "🔇 Пользователь в муте",
                        reply_markup=kb.as_markup(),
                        business_connection_id=conn,
                    )
                    q(
                        "UPDATE mutes SET last_notice=? WHERE conn_id=? AND chat_id=?",
                        (int(time.time()), conn, m.chat.id),
                    )
                except Exception:
                    logging.exception("mute notice failed")
            return

    # автошрифт: переписываем сообщение владельца выбранным шрифтом
    if sender and sender.id == owner and m.text and not m.text.startswith("/"):
        idx = current_font(owner)
        if idx >= 0 and entities_ok(m):
            new_text = stylize_smart(FONT_STYLES[idx][1], m.text)
            if new_text != m.text and len(new_text) <= 4096:
                try:
                    await bot.edit_message_text(
                        new_text,
                        business_connection_id=conn,
                        chat_id=m.chat.id,
                        message_id=m.message_id,
                        parse_mode=None,
                    )
                except Exception as e:
                    logging.exception("autofont edit failed")
                    if owner not in font_fail_notified:
                        font_fail_notified.add(owner)
                        try:
                            await bot.send_message(
                                owner,
                                "⚠️ Не удалось применить автошрифт к сообщению.\n\n"
                                "Что проверить:\n"
                                "1. Настройки → Telegram для бизнеса → Чат-боты → у бота включено "
                                "«Отвечать на сообщения».\n"
                                "2. Чат входит в список, где бот разрешён.\n"
                                "3. Telegram даёт боту править сообщения только в чатах, где собеседник "
                                "писал за последние 24 часа.\n\n"
                                f"Ответ Telegram: {esc(str(e))[:300]}",
                            )
                        except Exception:
                            pass

    # автоответчик: только на сообщения собеседника, не чаще раза в AWAY_COOLDOWN
    u = ensure_user(owner)
    if u["away_on"] and sender and sender.id != owner and not sender.is_bot:
        last = q(
            "SELECT ts FROM away_log WHERE conn_id=? AND chat_id=?",
            (m.business_connection_id, m.chat.id),
        ).fetchone()
        if not last or time.time() - last["ts"] > AWAY_COOLDOWN:
            try:
                await bot.send_message(
                    m.chat.id, u["away_text"],
                    business_connection_id=m.business_connection_id,
                )
                q(
                    "INSERT OR REPLACE INTO away_log VALUES(?,?,?)",
                    (m.business_connection_id, m.chat.id, int(time.time())),
                )
            except Exception:
                logging.exception("away reply failed")


@dp.edited_business_message()
async def on_business_edit(m: Message):
    owner = await resolve_owner(m.business_connection_id)
    if not owner or not is_active(owner):
        return
    sender = m.from_user
    old = q(
        "SELECT text FROM messages WHERE conn_id=? AND chat_id=? AND msg_id=?",
        (m.business_connection_id, m.chat.id, m.message_id),
    ).fetchone()
    new_text = describe(m)
    q(
        "INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?)",
        (
            m.business_connection_id, m.chat.id, m.message_id,
            sender.id if sender else 0,
            sender.full_name if sender else "Неизвестный",
            new_text, int(time.time()),
        ),
    )
    if not old or old["text"] == new_text or (sender and sender.id == owner):
        return
    if not ensure_user(owner)["notify_edited"]:
        return
    try:
        await bot.send_message(
            owner,
            f"✏️ <b>{person_name(sender)}</b> изменил(а) сообщение\n"
            f"💬 Чат: {chat_label(m.chat)}\n\n"
            f"<b>Было:</b>\n{esc(old['text'])[:1500]}\n\n"
            f"<b>Стало:</b>\n{esc(new_text)[:1500]}",
        )
    except Exception:
        logging.exception("edit notify failed")


@dp.deleted_business_messages()
async def on_business_delete(ev: BusinessMessagesDeleted):
    owner = await resolve_owner(ev.business_connection_id)
    if not owner or not is_active(owner):
        return
    if not ensure_user(owner)["notify_deleted"]:
        return

    items = []
    for mid in ev.message_ids:
        row = q(
            "SELECT sender_id, sender_name, text, ts FROM messages WHERE conn_id=? AND chat_id=? AND msg_id=?",
            (ev.business_connection_id, ev.chat.id, mid),
        ).fetchone()
        if row and row["sender_id"] != owner:
            items.append(row)

    if not items:
        return

    header = f"🗑 <b>Удалены сообщения</b>\n💬 Чат: {chat_label(ev.chat)}\n"
    chunk = header
    for r in items:
        when = time.strftime("%H:%M", time.localtime(r["ts"]))
        line = f"\n👤 <b>{esc(r['sender_name'])}</b> ({when}):\n{esc(r['text'])[:1000]}\n"
        if len(chunk) + len(line) > 3800:
            await bot.send_message(owner, chunk)
            chunk = header
        chunk += line
    try:
        await bot.send_message(owner, chunk)
    except Exception:
        logging.exception("delete notify failed")


# ─────────────────────────────────────────────
# АДМИН-КОМАНДЫ (выдача тарифов)
# ─────────────────────────────────────────────

@dp.message(Command("help"))
async def help_cmd(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Команды: /start, /menu, /find, /setaway, /font, /mute, /unmute")
        return
    await message.answer(
        "🛠 <b>Админ-команды</b>\n\n"
        "Для владельцев: /mute и /unmute в чате с человеком, /font текст\n\n"
        "/give id дни — выдать/продлить доступ (например /give 123456 30)\n"
        "/revoke id — отозвать доступ\n"
        "/verify id — пропустить проверку на бота для пользователя\n"
        "/check id — статус пользователя\n"
        "/stats_admin — общая статистика\n"
        "/users — последние пользователи"
    )


@dp.message(Command("give"))
async def give(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, uid_s, days_s = message.text.split()
        uid, days = int(uid_s), int(days_s)
        if days <= 0 or days > 3650:
            raise ValueError
    except ValueError:
        await message.answer("Использование: /give <id> <дни> (1–3650)")
        return
    ensure_user(uid)
    base = max(int(time.time()), ensure_user(uid)["sub_until"])
    new_until = base + days * 86400
    q("UPDATE users SET sub_until=? WHERE user_id=?", (new_until, uid))
    await message.answer(f"✅ Пользователю {uid} выдано {days} дн. Осталось: {fmt_left(new_until - int(time.time()))}")
    try:
        await bot.send_message(uid, f"🎉 Вам выдан доступ на <b>{days} дн.</b>\nОткройте /menu и подключите бота (кнопка «Как подключить»).")
    except Exception:
        await message.answer("(пользователь ещё не писал боту — уведомление не доставлено, но доступ выдан)")


@dp.message(Command("verify"))
async def verify_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, uid_s = message.text.split()
        uid = int(uid_s)
    except ValueError:
        await message.answer("Использование: /verify <id>")
        return
    ensure_user(uid)
    q("UPDATE users SET verified=1 WHERE user_id=?", (uid,))
    captcha.pop(uid, None)
    captcha_fails.pop(uid, None)
    captcha_lock.pop(uid, None)
    await message.answer(f"Пользователь {uid} отмечен как проверенный.")


@dp.message(Command("revoke"))
async def revoke(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, uid_s = message.text.split()
        uid = int(uid_s)
    except ValueError:
        await message.answer("Использование: /revoke <id>")
        return
    ensure_user(uid)
    q("UPDATE users SET sub_until=0 WHERE user_id=?", (uid,))
    await message.answer(f"Доступ пользователя {uid} отозван.")


@dp.message(Command("check"))
async def check(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, uid_s = message.text.split()
        uid = int(uid_s)
    except ValueError:
        await message.answer("Использование: /check <id>")
        return
    left = sub_left(uid)
    await message.answer(f"ID {uid}: " + (f"активен, осталось {fmt_left(left)}" if left else "подписки нет"))


@dp.message(Command("stats_admin"))
async def stats_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    now = int(time.time())
    total = q("SELECT COUNT(*) n FROM users").fetchone()["n"]
    active = q("SELECT COUNT(*) n FROM users WHERE sub_until>?", (now,)).fetchone()["n"]
    conns = q("SELECT COUNT(*) n FROM connections WHERE enabled=1").fetchone()["n"]
    msgs = q("SELECT COUNT(*) n FROM messages").fetchone()["n"]
    await message.answer(
        f"📊 Пользователей: {total}\n✅ С активной подпиской: {active}\n"
        f"🔌 Подключений: {conns}\n💬 Сообщений в кэше: {msgs}"
    )


@dp.message(Command("users"))
async def users(message: Message):
    if not is_admin(message.from_user.id):
        return
    rows = q("SELECT user_id, sub_until FROM users ORDER BY rowid DESC LIMIT 20").fetchall()
    now = int(time.time())
    lines = [
        f"{r['user_id']} — " + (f"до {time.strftime('%d.%m.%Y', time.localtime(r['sub_until']))}" if r["sub_until"] > now else "нет подписки")
        for r in rows
    ]
    await message.answer("\n".join(lines) or "Пока никого.")


# ─────────────────────────────────────────────
# ОЧИСТКА И ЗАПУСК
# ─────────────────────────────────────────────

async def cleanup_loop():
    while True:
        cutoff = int(time.time()) - KEEP_DAYS * 86400
        q("DELETE FROM messages WHERE ts<?", (cutoff,))
        q("DELETE FROM away_log WHERE ts<?", (cutoff,))
        await asyncio.sleep(6 * 3600)


async def main():
    global BOT_USERNAME
    BOT_USERNAME = (await bot.get_me()).username
    asyncio.create_task(cleanup_loop())
    print(f"Business bot @{BOT_USERNAME} запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
