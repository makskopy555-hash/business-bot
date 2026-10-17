import asyncio
import html
import logging
import os
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
    CREATE TABLE IF NOT EXISTS away_log(
        conn_id TEXT, chat_id INTEGER, ts INTEGER,
        PRIMARY KEY(conn_id, chat_id)
    );
    """
)
db.commit()


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
    "🔎 поиск по сохранённым сообщениям\n"
    "🕶 подсказки по настройкам приватности Telegram\n\n"
    "🔒 Уведомления видите только вы. Бот хранит текст сообщений "
    f"{KEEP_DAYS} дней, фото/видео/файлы не скачивает."
)


def tariffs_kb():
    kb = InlineKeyboardBuilder()
    for key, (title, _) in TARIFFS.items():
        kb.button(text=f"💳 {title}", callback_data=f"buy_{key}")
    kb.adjust(1)
    return kb.as_markup()


def cabinet_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⚙️ Настройки", callback_data="settings")
    kb.button(text="🔌 Как подключить", callback_data="howto")
    kb.button(text="📊 Статистика", callback_data="stats")
    kb.button(text="🕶 Приватность", callback_data="privacy")
    kb.button(text="💳 Тарифы", callback_data="tariffs")
    kb.adjust(2, 2, 1)
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


@dp.message(CommandStart())
async def start(message: Message):
    ensure_user(message.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Начать", callback_data="begin")
    await message.answer(ABOUT, reply_markup=kb.as_markup())


@dp.message(Command("menu"))
async def menu_cmd(message: Message):
    if is_active(message.from_user.id):
        await message.answer(cabinet_text(message.from_user.id), reply_markup=cabinet_kb())
    else:
        await message.answer("Выберите тариф:", reply_markup=tariffs_kb())


@dp.callback_query(F.data == "begin")
async def begin(cb: CallbackQuery):
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


@dp.callback_query(F.data == "privacy")
async def privacy(cb: CallbackQuery):
    if not require_active(cb):
        await cb.answer("Нужна активная подписка", show_alert=True)
        return
    await safe_edit(
        cb,
        "🕶 <b>Приватность в Telegram</b>\n\n"
        "Бот не может менять ваш статус, но Telegram умеет это сам:\n\n"
        "• <b>Скрыть «был(а) в сети»:</b> Настройки → Конфиденциальность → Время захода\n"
        "• <b>Кто видит фото, звонки, пересылки:</b> там же, в разделе «Конфиденциальность»\n"
        "• <b>Время прочтения</b> — настройка доступна владельцам Premium\n"
        "• <b>Тихие сообщения:</b> зажмите кнопку отправки → «Без звука»\n"
        "• <b>Отложенные сообщения:</b> зажмите отправку → «Запланировать»\n\n"
        "ℹ️ Статус «печатает…» скрыть нельзя — такой настройки в Telegram нет.",
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
    q(
        "INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?)",
        (
            m.business_connection_id, m.chat.id, m.message_id,
            sender.id if sender else 0,
            sender.full_name if sender else "Неизвестный",
            describe(m), int(time.time()),
        ),
    )

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
        await message.answer("Команды: /start, /menu, /find, /setaway")
        return
    await message.answer(
        "🛠 <b>Админ-команды</b>\n\n"
        "/give id дни — выдать/продлить доступ (например /give 123456 30)\n"
        "/revoke id — отозвать доступ\n"
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