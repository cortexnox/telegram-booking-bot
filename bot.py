"""
Demo appointment booking bot (aiogram 3 + SQLite).

Features:
- Welcome message with an image and contact details
- Service selection -> date -> available time slots
- Prevents double booking of the same time slot
- "My Bookings" section with cancellation
- Reminder sent to the client one hour before the appointment
- Notifications to the owner about new and cancelled bookings
- /admin: list of upcoming appointments (owner only)
"""

import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "bookings.db")

# ---- Client-specific settings: can be changed in just a few minutes ----
SERVICES = {
    "cut": ("Haircut", "25 €"),
    "color": ("Hair Coloring", "60 €"),
    "style": ("Hair Styling", "20 €"),
}
WORK_HOURS = range(10, 18)   # time slots: 10:00 ... 17:00
DAYS_AHEAD = 7
CLOSED_WEEKDAYS = {6}        # 6 = Sunday
REMIND_BEFORE = timedelta(hours=1)

WELCOME_IMAGE = Path(__file__).parent / "welcome.png"
WELCOME_TEXT = (
    "👋 Welcome to the Beauty Studio!\n\n"
    "You can book an appointment online in just a minute: "
    "choose a service, date, and convenient time. "
    "I'll remind you about your appointment one hour in advance.\n\n"
    "What would you like to book?"
)
CONTACTS_TEXT = (
    "📍 Contact Information\n\n"
    "Address: 1 Example Street\n"
    "Phone: +49 000 0000000\n"
    "Opening hours: Mon–Sat, 10:00–18:00"
)
# -----------------------------------------------------------------------

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]

router = Router()


def fmt_day(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def fmt_slot(slot: str) -> str:
    dt = datetime.fromisoformat(slot)
    return (
        f"{WEEKDAYS[dt.weekday()]}, {dt.day} "
        f"{MONTHS[dt.month - 1]}, {dt:%H:%M}"
    )


def client_label(user) -> str:
    return user.full_name + (f" (@{user.username})" if user.username else "")


# ---------- Database ----------
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                service TEXT NOT NULL,
                slot TEXT NOT NULL UNIQUE,   -- 'YYYY-MM-DD HH:MM'
                reminded INTEGER NOT NULL DEFAULT 0
            )"""
        )
        await db.commit()


async def taken_slots(day: str) -> set[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT slot FROM bookings WHERE slot LIKE ?",
            (f"{day}%",)
        )
        return {row[0][11:] for row in await cur.fetchall()}


async def add_booking(
    user_id: int, name: str, service: str, slot: str
) -> int | None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO bookings (user_id, user_name, service, slot) "
                "VALUES (?,?,?,?)",
                (user_id, name, service, slot),
            )
            await db.commit()
            return cur.lastrowid
    except aiosqlite.IntegrityError:
        return None  # The time slot is already booked


async def user_bookings(user_id: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, service, slot FROM bookings "
            "WHERE user_id=? AND slot>=? ORDER BY slot",
            (user_id, now),
        )
        return await cur.fetchall()


async def cancel_booking(booking_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT service, slot, user_name FROM bookings "
            "WHERE id=? AND user_id=?",
            (booking_id, user_id),
        )
        row = await cur.fetchone()
        if row:
            await db.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
            await db.commit()
        return row


# ---------- Keyboards ----------
def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Book an Appointment", callback_data="menu:book")
    kb.button(text="🗂 My Bookings", callback_data="menu:my")
    kb.button(text="📍 Contact Information", callback_data="menu:contacts")
    kb.adjust(1)
    return kb.as_markup()


def services_kb():
    kb = InlineKeyboardBuilder()
    for key, (title, price) in SERVICES.items():
        kb.button(
            text=f"{title} — {price}",
            callback_data=f"svc:{key}"
        )
    kb.adjust(1)
    return kb.as_markup()


def dates_kb(service: str):
    kb = InlineKeyboardBuilder()
    today = date.today()
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        if d.weekday() in CLOSED_WEEKDAYS:
            continue
        kb.button(
            text=f"{WEEKDAYS[d.weekday()]} {d:%d.%m}",
            callback_data=f"date:{service}:{d.isoformat()}"
        )
    kb.adjust(3)
    return kb.as_markup()


async def times_kb(service: str, day: str):
    busy = await taken_slots(day)
    now = datetime.now()
    kb = InlineKeyboardBuilder()
    count = 0
    for h in WORK_HOURS:
        t = f"{h:02d}:00"
        if t in busy:
            continue
        if datetime.fromisoformat(f"{day} {t}") <= now:
            continue
        kb.button(
            text=t,
            callback_data=f"time:{service}:{day}:{h:02d}00"
        )
        count += 1
    kb.adjust(4)
    return kb.as_markup(), count


# ---------- Handlers ----------
@router.message(CommandStart())
async def cmd_start(m: Message):
    # Send the image as a separate message so edit_text works in the menu
    if WELCOME_IMAGE.exists():
        await m.answer_photo(FSInputFile(WELCOME_IMAGE))
    await m.answer(WELCOME_TEXT, reply_markup=main_menu())


@router.callback_query(F.data == "menu:home")
async def menu_home(c: CallbackQuery):
    await c.message.edit_text(WELCOME_TEXT, reply_markup=main_menu())
    await c.answer()


@router.callback_query(F.data == "menu:contacts")
async def menu_contacts(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Back to Menu", callback_data="menu:home")
    await c.message.edit_text(CONTACTS_TEXT, reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data == "menu:book")
async def menu_book(c: CallbackQuery):
    await c.message.edit_text(
        "Please choose a service:",
        reply_markup=services_kb()
    )
    await c.answer()


@router.callback_query(F.data.startswith("svc:"))
async def choose_service(c: CallbackQuery):
    service = c.data.split(":")[1]
    title, price = SERVICES[service]
    await c.message.edit_text(
        f"{title} — {price}\n\nPlease choose a date:",
        reply_markup=dates_kb(service)
    )
    await c.answer()


@router.callback_query(F.data.startswith("date:"))
async def choose_date(c: CallbackQuery):
    _, service, day = c.data.split(":")
    kb, count = await times_kb(service, day)
    if count == 0:
        await c.answer(
            "There are no available time slots on this day.",
            show_alert=True
        )
        return
    await c.message.edit_text(
        f"{SERVICES[service][0]}\n"
        f"📅 {fmt_day(day)}\n\n"
        "Please choose a time:",
        reply_markup=kb
    )
    await c.answer()


@router.callback_query(F.data.startswith("time:"))
async def choose_time(c: CallbackQuery, bot: Bot):
    _, service, day, hhmm = c.data.split(":")
    slot = f"{day} {hhmm[:2]}:{hhmm[2:]}"
    booking_id = await add_booking(
        c.from_user.id,
        c.from_user.full_name,
        service,
        slot
    )
    if booking_id is None:
        await c.answer(
            "This time slot has just been booked. Please choose another one.",
            show_alert=True
        )
        kb, _ = await times_kb(service, day)
        await c.message.edit_reply_markup(reply_markup=kb)
        return

    title = SERVICES[service][0]
    await c.message.edit_text(
        f"✅ Your appointment has been booked!\n\n"
        f"{title}\n"
        f"📅 {fmt_slot(slot)}\n\n"
        "I'll remind you one hour before your appointment."
    )
    if ADMIN_ID:
        await bot.send_message(
            ADMIN_ID,
            f"🆕 New Booking\n"
            f"{title}\n"
            f"📅 {fmt_slot(slot)}\n"
            f"👤 {client_label(c.from_user)}"
        )
    await c.answer()


async def show_my(user_id: int, message: Message, edit: bool = False):
    rows = await user_bookings(user_id)
    if not rows:
        text, markup = "You have no upcoming appointments.", main_menu()
    else:
        kb = InlineKeyboardBuilder()
        lines = []
        for bid, service, slot in rows:
            lines.append(
                f"• {SERVICES[service][0]} — {fmt_slot(slot)}"
            )
            kb.button(
                text=f"❌ Cancel: {fmt_slot(slot)}",
                callback_data=f"cancel:{bid}"
            )
        kb.adjust(1)
        text, markup = "Your Bookings:\n\n" + "\n".join(lines), kb.as_markup()

    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.message(Command("my"))
async def cmd_my(m: Message):
    await show_my(m.from_user.id, m)


@router.callback_query(F.data == "menu:my")
async def menu_my(c: CallbackQuery):
    await show_my(c.from_user.id, c.message, edit=True)
    await c.answer()


@router.callback_query(F.data.startswith("cancel:"))
async def cancel(c: CallbackQuery, bot: Bot):
    row = await cancel_booking(
        int(c.data.split(":")[1]),
        c.from_user.id
    )
    if not row:
        await c.answer("Booking not found.", show_alert=True)
        return

    service, slot, _ = row
    if ADMIN_ID:
        await bot.send_message(
            ADMIN_ID,
            f"❌ Booking Cancelled\n"
            f"{SERVICES[service][0]}\n"
            f"📅 {fmt_slot(slot)}\n"
            f"👤 {client_label(c.from_user)}"
        )
    await c.answer("Your booking has been cancelled.")
    await show_my(c.from_user.id, c.message, edit=True)


@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if m.from_user.id != ADMIN_ID:
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT service, slot, user_name FROM bookings "
            "WHERE slot>=? ORDER BY slot LIMIT 30",
            (now,),
        )
        rows = await cur.fetchall()

    if not rows:
        await m.answer("There are no upcoming appointments.")
        return

    await m.answer(
        "Upcoming Appointments:\n\n" + "\n".join(
            f"{fmt_slot(slot)} — {SERVICES[s][0]} — {name}"
            for s, slot, name in rows
        )
    )


# ---------- Reminders ----------
async def reminder_loop(bot: Bot):
    while True:
        try:
            limit = datetime.now() + REMIND_BEFORE
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT id, user_id, service, slot "
                    "FROM bookings WHERE reminded=0"
                )
                for bid, uid, service, slot in await cur.fetchall():
                    when = datetime.fromisoformat(slot)
                    if datetime.now() < when <= limit:
                        await bot.send_message(
                            uid,
                            f"⏰ Appointment Reminder\n"
                            f"{SERVICES[service][0]} today at {slot[11:]}."
                        )
                        await db.execute(
                            "UPDATE bookings SET reminded=1 WHERE id=?",
                            (bid,)
                        )
                await db.commit()
        except Exception:
            logging.exception("reminder_loop error")
        await asyncio.sleep(60)


async def main():
    if not TOKEN:
        raise SystemExit("Please set BOT_TOKEN in the .env file")

    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    reminder_task = asyncio.create_task(reminder_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
