"""
Huquq test boti — Milliy sertifikat uslubidagi testlar uchun Telegram bot.

Ishga tushirish:  python bot.py (yoki python main.py)
Sozlamalar:       .env fayli (BOT_TOKEN, ADMIN_IDS, SHUFFLE_QUESTIONS)
Testlar:          tests/ papkasidagi .json fayllar
"""

import asyncio
from contextlib import contextmanager
import html
import json
import logging
import os
import random
import re
import sqlite3
import time
from pathlib import Path
from typing import Generator

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Sozlamalar va Yo'llar
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SHUFFLE_QUESTIONS = os.getenv("SHUFFLE_QUESTIONS", "1").strip() == "1"

TESTS_DIR = BASE_DIR / "tests"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "natijalar.db"

# Agar eski joyda natijalar.db bo'lsa va yangi joyda hali yo'q bo'lsa ko'chirish
_old_db = BASE_DIR / "natijalar.db"
if _old_db.exists() and not DB_PATH.exists():
    try:
        import shutil
        shutil.copy2(str(_old_db), str(DB_PATH))
    except Exception as _e:
        log.warning("Bazani ko'chirishda xatolik: %s", _e)

ADMINS_FILE = BASE_DIR / "admins.txt"
HARFLAR = ["A", "B", "C", "D", "E", "F"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("huquq_bot")

# ---------------------------------------------------------------------------
# Adminlarni yuklash
# ---------------------------------------------------------------------------
ADMIN_IDS: set[int] = set()


def load_admins() -> set[int]:
    """Adminlar ID sini .env va admins.txt dan yuklaydi."""
    global ADMIN_IDS
    ids = set()

    # .env dan
    env_admins = os.getenv("ADMIN_IDS", "").replace(" ", "").split(",")
    for item in env_admins:
        if item.isdigit():
            ids.add(int(item))

    # admins.txt dan
    if ADMINS_FILE.exists():
        try:
            content = ADMINS_FILE.read_text(encoding="utf-8")
            for item in content.replace(",", "\n").split():
                val = item.strip()
                if val.isdigit():
                    ids.add(int(val))
        except Exception as e:
            log.warning("admins.txt o'qishda xatolik: %s", e)

    ADMIN_IDS = ids
    log.info("Yuklangan adminlar soni: %d", len(ADMIN_IDS))
    return ADMIN_IDS


# ---------------------------------------------------------------------------
# Testlarni yuklash
# ---------------------------------------------------------------------------
TESTS: dict[str, dict] = {}


def load_tests() -> list[str]:
    """tests/ papkasidagi barcha .json fayllarni o'qiydi. Xatolar ro'yxatini qaytaradi."""
    TESTS.clear()
    errors: list[str] = []

    if not TESTS_DIR.exists():
        TESTS_DIR.mkdir(parents=True, exist_ok=True)

    for path in sorted(TESTS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            test_id = path.stem
            savollar = data.get("savollar", [])
            if not savollar:
                raise ValueError("Savollar ro'yxati bo'sh")

            for i, s in enumerate(savollar, 1):
                variantlar = s.get("variantlar", [])
                if not variantlar:
                    raise ValueError(f"{i}-savolda variantlar yo'q")
                togri = int(s.get("togri", -1))
                if not (0 <= togri < len(variantlar)):
                    raise ValueError(f"{i}-savolda 'togri' indeksi ({togri}) noto'g'ri")

            TESTS[test_id] = {
                "id": test_id,
                "nomi": data.get("nomi", test_id),
                "vaqt_daqiqa": int(data.get("vaqt_daqiqa", 0)),
                "savollar": savollar,
            }
        except Exception as e:  # noqa: BLE001
            errors.append(f"{path.name}: {e}")
            log.error("Testni yuklashda xato %s: %s", path.name, e)

    log.info("%d ta test yuklandi", len(TESTS))
    return errors


# ---------------------------------------------------------------------------
# Ma'lumotlar bazasi (SQLite xavfsiz context manager va indekslar)
# ---------------------------------------------------------------------------
def init_db() -> None:
    """Ma'lumotlar bazasi jadvallari va indekslarini yaratadi."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS natijalar (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ism TEXT,
                test_id TEXT NOT NULL,
                togri INTEGER NOT NULL,
                jami INTEGER NOT NULL,
                sekund INTEGER NOT NULL,
                sana TEXT DEFAULT (datetime('now', 'localtime'))
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_natijalar_user ON natijalar(user_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_natijalar_test ON natijalar(test_id);")
        conn.commit()


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Har bir chaqiriqda alohida ulanish beradi va uni avtomatik yopadi."""
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_result(user_id: int, ism: str, test_id: str, togri: int, jami: int, sekund: int) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO natijalar (user_id, ism, test_id, togri, jami, sekund) VALUES (?,?,?,?,?,?)",
            (user_id, ism, test_id, togri, jami, sekund),
        )


# ---------------------------------------------------------------------------
# Foydalanuvchi sessiyalari (xotirada)
# ---------------------------------------------------------------------------
# user_id -> {test_id, tartib, idx, javoblar, boshlangan}
SESSIONS: dict[int, dict] = {}


def cleanup_expired_sessions() -> None:
    """24 soatdan ortiq faol bo'lmagan sessiyalarni xotiradan tozalaydi."""
    now = time.time()
    expired = [uid for uid, s in SESSIONS.items() if now - s.get("boshlangan", 0) > 86400]
    for uid in expired:
        SESSIONS.pop(uid, None)


def tests_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"📘 {t['nomi']} ({len(t['savollar'])} ta savol)",
            callback_data=f"test:{t['id']}",
        )]
        for t in TESTS.values()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def question_view(session: dict) -> tuple[str, InlineKeyboardMarkup]:
    test = TESTS[session["test_id"]]
    idx = session["idx"]
    q = test["savollar"][session["tartib"][idx]]
    jami = len(session["tartib"])

    lines = [f"<b>{idx + 1}/{jami}-savol</b>", "", html.escape(q["savol"]), ""]
    for i, v in enumerate(q["variantlar"]):
        lines.append(f"<b>{HARFLAR[i]})</b> {html.escape(v)}")

    if test["vaqt_daqiqa"]:
        qolgan = test["vaqt_daqiqa"] * 60 - int(time.time() - session["boshlangan"])
        qolgan = max(qolgan, 0)
        lines += ["", f"⏱ Qolgan vaqt: {qolgan // 60} daq {qolgan % 60} son"]

    buttons = [
        InlineKeyboardButton(text=HARFLAR[i], callback_data=f"ans:{idx}:{i}")
        for i in range(len(q["variantlar"]))
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        buttons,
        [InlineKeyboardButton(text="⛔ Testni yakunlash", callback_data="finish")],
    ])
    return "\n".join(lines), kb


def chunks(text: str, limit: int = 3800) -> list[str]:
    """Uzun matnni Telegram xabari chegarasidan oshmaydigan qismlarga bo'ladi."""
    parts: list[str] = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            if cur.strip():
                parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


def get_display_name(user) -> str:
    """Foydalanuvchi ismini xavfsiz aniqlaydi."""
    parts = [user.first_name, user.last_name]
    full = " ".join(filter(None, parts)).strip()
    if full:
        return full
    if user.username:
        return f"@{user.username}"
    return f"Foydalanuvchi_{user.id}"


# ---------------------------------------------------------------------------
# Bot dispatcher
# ---------------------------------------------------------------------------
dp = Dispatcher()


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMIN_IDS)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    cleanup_expired_sessions()
    ism = html.escape(message.from_user.first_name or "foydalanuvchi")
    await message.answer(
        f"Assalomu alaykum, <b>{ism}</b>! 👋\n\n"
        "Bu bot orqali huquq fanidan <b>Milliy sertifikat</b> uslubidagi testlarni yechishingiz mumkin.\n\n"
        "📋 /testlar — testlar ro'yxati\n"
        "📊 /natijalarim — mening natijalarim\n"
        "🏆 /reyting — eng yaxshi natijalar\n"
        "⛔ /bekor — joriy testni to'xtatish"
    )


@dp.message(Command("testlar"))
async def cmd_tests(message: Message) -> None:
    cleanup_expired_sessions()
    if not TESTS:
        await message.answer("Hozircha testlar yo'q.")
        return
    await message.answer("Qaysi testni yechasiz?", reply_markup=tests_keyboard())


@dp.callback_query(F.data.startswith("test:"))
async def cb_start_test(call: CallbackQuery) -> None:
    test_id = call.data.split(":", 1)[1]
    test = TESTS.get(test_id)
    if not test:
        await call.answer("Bu test topilmadi.", show_alert=True)
        return

    tartib = list(range(len(test["savollar"])))
    if SHUFFLE_QUESTIONS:
        random.shuffle(tartib)

    SESSIONS[call.from_user.id] = {
        "test_id": test_id,
        "tartib": tartib,
        "idx": 0,
        "javoblar": {},
        "boshlangan": time.time(),
    }

    vaqt = f"\n⏱ Ajratilgan vaqt: {test['vaqt_daqiqa']} daqiqa" if test["vaqt_daqiqa"] else ""
    await call.message.answer(
        f"<b>{html.escape(test['nomi'])}</b>\nSavollar soni: {len(tartib)}{vaqt}\n\nOmad tilaymiz! 🍀"
    )
    text, kb = question_view(SESSIONS[call.from_user.id])
    await call.message.answer(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("ans:"))
async def cb_answer(call: CallbackQuery) -> None:
    session = SESSIONS.get(call.from_user.id)
    if not session:
        await call.answer("Faol test mavjud emas. /testlar orqali boshlang.", show_alert=True)
        return

    try:
        _, idx_s, choice_s = call.data.split(":")
        idx, choice = int(idx_s), int(choice_s)
    except (ValueError, IndexError):
        await call.answer("Noto'g'ri so'rov.")
        return

    if idx != session["idx"]:
        await call.answer("Bu savolga allaqachon javob bergansiz.")
        return

    test = TESTS.get(session["test_id"])
    if not test:
        await call.answer("Test topilmadi.")
        return

    if test["vaqt_daqiqa"] and (time.time() - session["boshlangan"]) > (test["vaqt_daqiqa"] * 60):
        await call.answer("⏱ Vaqt tugadi!", show_alert=True)
        await finish_test(call.message, call.from_user)
        return

    session["javoblar"][idx] = choice

    # Tugma bosilgandan so'ng xabarni xavfsiz yangilash
    try:
        current_text = call.message.html_text or call.message.text or ""
        await call.message.edit_text(
            f"{current_text}\n\n✅ Tanlangan javob: <b>{HARFLAR[choice]}</b>"
        )
    except TelegramBadRequest:
        pass  # Xabar o'zgarmagan bo'lsa yoki tez bosilsa bot to'xtab qolmaydi

    session["idx"] += 1
    await call.answer()

    if session["idx"] >= len(session["tartib"]):
        await finish_test(call.message, call.from_user)
    else:
        text, kb = question_view(session)
        await call.message.answer(text, reply_markup=kb)


@dp.callback_query(F.data == "finish")
async def cb_finish(call: CallbackQuery) -> None:
    if call.from_user.id not in SESSIONS:
        await call.answer("Faol test yo'q.")
        return
    await call.answer()
    await finish_test(call.message, call.from_user)


async def finish_test(message: Message, user) -> None:
    session = SESSIONS.pop(user.id, None)
    if not session:
        return

    test = TESTS.get(session["test_id"])
    if not test:
        return

    jami = len(session["tartib"])
    sekund = int(time.time() - session["boshlangan"])

    togri = 0
    xatolar = []
    for idx, q_index in enumerate(session["tartib"]):
        q = test["savollar"][q_index]
        javob = session["javoblar"].get(idx)
        if javob == q["togri"]:
            togri += 1
        else:
            belgilangan = HARFLAR[javob] if javob is not None else "—"
            line = (
                f"\n<b>{idx + 1}.</b> {html.escape(q['savol'])}\n"
                f"Sizning javobingiz: {belgilangan} | To'g'ri javob: <b>{HARFLAR[q['togri']]}) "
                f"{html.escape(q['variantlar'][q['togri']])}</b>"
            )
            if q.get("izoh"):
                line += f"\n💡 <i>Izoh:</i> {html.escape(q['izoh'])}"
            xatolar.append(line)

    ism = get_display_name(user)
    save_result(user.id, ism, test["id"], togri, jami, sekund)

    foiz = round((togri / jami) * 100) if jami else 0
    text = (
        f"🏁 <b>Test yakunlandi!</b>\n\n"
        f"📘 {html.escape(test['nomi'])}\n"
        f"✅ To'g'ri javoblar: <b>{togri}</b> / {jami} ({foiz}%)\n"
        f"⏱ Sarflangan vaqt: {sekund // 60} daq {sekund % 60} son"
    )
    if xatolar:
        text += "\n\n<b>❌ Xatolar tahlili:</b>" + "".join(xatolar)
    else:
        text += "\n\n🎉 Ajoyib natija! Barcha javoblar to'g'ri!"

    for part in chunks(text):
        await message.answer(part)


@dp.message(Command("bekor"))
async def cmd_cancel(message: Message) -> None:
    if SESSIONS.pop(message.from_user.id, None):
        await message.answer("Test to'xtatildi. Natija saqlanmadi.")
    else:
        await message.answer("Hozirda faol test yo'q.")


@dp.message(Command("natijalarim"))
async def cmd_my_results(message: Message) -> None:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT test_id, togri, jami, sana FROM natijalar WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (message.from_user.id,),
        ).fetchall()

    if not rows:
        await message.answer("Siz hali test yechmagansiz. /testlar buyrug'i orqali testlarni boshlang.")
        return

    lines = ["📊 <b>Oxirgi natijalaringiz:</b>\n"]
    for test_id, togri, jami, sana in rows:
        nomi = TESTS.get(test_id, {}).get("nomi", test_id)
        sana_qisqa = sana[:16] if sana else ""
        lines.append(f"• {html.escape(nomi)} — <b>{togri}/{jami}</b> ({sana_qisqa})")
    await message.answer("\n".join(lines))


@dp.message(Command("reyting"))
async def cmd_rating(message: Message) -> None:
    if not TESTS:
        await message.answer("Hozircha testlar yo'q.")
        return

    lines = ["🏆 <b>Reyting</b> (har bir test bo'yicha eng yaxshi natijalar)\n"]
    with get_db() as conn:
        for t in TESTS.values():
            rows = conn.execute(
                """SELECT ism, MAX(togri) AS best, jami FROM natijalar
                   WHERE test_id=? GROUP BY user_id ORDER BY best DESC, sekund ASC LIMIT 10""",
                (t["id"],),
            ).fetchall()
            if not rows:
                continue
            lines.append(f"\n<b>📘 {html.escape(t['nomi'])}</b>")
            medals = ["🥇", "🥈", "🥉"]
            for i, (ism, best, jami) in enumerate(rows):
                belgi = medals[i] if i < 3 else f"{i + 1}."
                lines.append(f"{belgi} {html.escape(ism or 'Foydalanuvchi')} — {best}/{jami}")

    if len(lines) == 1:
        lines.append("Hali hech kim test topshirmagan.")

    for part in chunks("\n".join(lines)):
        await message.answer(part)


# --- Admin FSM va Boshqaruv Paneli ----------------------------------------
class AdminStates(StatesGroup):
    waiting_for_json = State()


SAMPLE_TEST_DATA = {
    "nomi": "Konstitutsiyaviy huquq asoslari (Namuna)",
    "vaqt_daqiqa": 15,
    "savollar": [
        {
            "savol": "O'zbekiston Respublikasining yangi tahrirdagi Konstitutsiyasi qachon qabul qilingan?",
            "variantlar": [
                "1992-yil 8-dekabrda",
                "2023-yil 30-aprelda",
                "2021-yil 24-oktyabrda",
                "2017-yil 7-fevralda"
            ],
            "togri": 1,
            "izoh": "Yangi tahrirdagi Konstitutsiya 2023-yil 30-aprel kuni referendum orqali qabul qilingan."
        },
        {
            "savol": "Konstitutsiyaga ko'ra O'zbekistonda davlat hokimiyatining birdan bir manbai kim?",
            "variantlar": [
                "Prezident",
                "Oliy Majlis",
                "Xalq",
                "Vazirlar Mahkamasi"
            ],
            "togri": 2,
            "izoh": "Konstitutsiyaning 7-moddasiga ko'ra, xalq davlat hokimiyatining birdan bir manbaidir."
        }
    ]
}


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📥 Yangi test yuklash (.json)", callback_data="adm:upload")],
            [InlineKeyboardButton(text="📄 Namuna JSON yuklab olish", callback_data="adm:sample")],
            [InlineKeyboardButton(text="📋 Mavjud testlar ro'yxati", callback_data="adm:list")],
            [
                InlineKeyboardButton(text="📈 Statistika", callback_data="adm:stats"),
                InlineKeyboardButton(text="🔄 Yangilash", callback_data="adm:reload"),
            ],
            [InlineKeyboardButton(text="❌ Yopish", callback_data="adm:close")],
        ]
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    await state.clear()
    await message.answer(
        "🛠 <b>Boshqaruv paneli (Admin)</b>\n\n"
        "Quyidagi menyu orqali botga yangi test qo'shishingiz, "
        "namuna yuklab olishingiz yoki mavjud testlarni boshqarishingiz mumkin:",
        reply_markup=admin_keyboard(),
    )


@dp.callback_query(F.data == "adm:menu")
async def cb_admin_menu(call: CallbackQuery, state: FSMContext) -> None:
    if not (call.from_user and call.from_user.id in ADMIN_IDS):
        await call.answer("Ruxsat berilmagan.", show_alert=True)
        return
    await state.clear()
    await call.message.edit_text(
        "🛠 <b>Boshqaruv paneli (Admin)</b>\n\n"
        "Quyidagi menyu orqali kerakli bo'limni tanlang:",
        reply_markup=admin_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data == "adm:sample")
async def cb_admin_sample(call: CallbackQuery) -> None:
    if not (call.from_user and call.from_user.id in ADMIN_IDS):
        await call.answer("Ruxsat berilmagan.", show_alert=True)
        return

    json_bytes = json.dumps(SAMPLE_TEST_DATA, ensure_ascii=False, indent=2).encode("utf-8")
    sample_file = BufferedInputFile(json_bytes, filename="namuna_test.json")

    await call.message.answer_document(
        document=sample_file,
        caption=(
            "📄 <b>Namuna JSON fayli</b>\n\n"
            "Ushbu faylni yuklab olib, o'z test savollaringizni shu formatda kiriting:\n"
            "• <code>nomi</code>: Test mavzusi nomi\n"
            "• <code>vaqt_daqiqa</code>: Ajratilgan vaqt (0 bo'lsa cheklanmagan)\n"
            "• <code>savollar</code>: Savollar ro'yxati\n"
            "• <code>togri</code>: To'g'ri javob indeksi (0=A, 1=B, 2=C, 3=D)\n"
            "• <code>izoh</code>: Savol tahlili yoki modda (ixtiyoriy)\n\n"
            "Tayyor bo'lgach, 'Yangi test yuklash' orqali botga yuboring."
        ),
    )
    await call.answer()


@dp.callback_query(F.data == "adm:upload")
async def cb_admin_upload(call: CallbackQuery, state: FSMContext) -> None:
    if not (call.from_user and call.from_user.id in ADMIN_IDS):
        await call.answer("Ruxsat berilmagan.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_for_json)
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="adm:menu")]]
    )
    await call.message.edit_text(
        "📥 <b>Yangi test yuklash</b>\n\n"
        "Iltimos, tayyorlangan <b>.json</b> faylni Telegram orqali hujjat (fayl) ko'rinishida yuboring.\n\n"
        "<i>Bekor qilish uchun pastdagi tugmani bosing yoki /cancel yozing.</i>",
        reply_markup=cancel_kb,
    )
    await call.answer()


@dp.message(AdminStates.waiting_for_json, F.document)
async def process_test_upload(message: Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message):
        return

    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".json"):
        await message.answer("⚠ Iltimos, faqat <b>.json</b> formatidagi fayl yuboring!")
        return

    try:
        # Faylni yuklab olish
        file_io = await bot.download(doc.file_id)
        raw_content = file_io.read().decode("utf-8")
        data = json.loads(raw_content)

        # 1. Struktura tekshiruvi
        if not isinstance(data, dict):
            raise ValueError("JSON fayl asosiy obyekti dict ({}) ko'rinishida bo'lishi kerak.")

        nomi = str(data.get("nomi", "")).strip()
        if not nomi:
            raise ValueError("'nomi' maydoni kiritilmagan yoki bo'sh.")

        vaqt = int(data.get("vaqt_daqiqa", 0))
        savollar = data.get("savollar")
        if not isinstance(savollar, list) or len(savollar) == 0:
            raise ValueError("'savollar' ro'yxati topilmadi yoki bo'sh.")

        # 2. Har bir savolni tekshirish
        for i, s in enumerate(savollar, 1):
            if not isinstance(s, dict):
                raise ValueError(f"{i}-savol noto'g'ri shakllantirilgan.")
            q_text = str(s.get("savol", "")).strip()
            if not q_text:
                raise ValueError(f"{i}-savolda 'savol' matni bo'sh.")
            variantlar = s.get("variantlar")
            if not isinstance(variantlar, list) or len(variantlar) < 2:
                raise ValueError(f"{i}-savolda 'variantlar' kamida 2 ta bo'lishi kerak.")
            togri = s.get("togri")
            if togri is None or not str(togri).isdigit() or not (0 <= int(togri) < len(variantlar)):
                raise ValueError(f"{i}-savolda 'togri' javob indeksi noto'g'ri (0 dan {len(variantlar)-1} gacha bo'lishi kerak).")

        # 3. Faylni xavfsiz nom bilan saqlash
        safe_name = Path(doc.file_name).stem
        safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", safe_name).strip("_") or "test"
        final_file_name = f"{safe_name}.json"

        # Dublikat nom bo'lsa son qo'shish
        target_path = TESTS_DIR / final_file_name
        counter = 1
        while target_path.exists():
            # Agar bir xil fayl qayta yuklansa yoki yangilanayotgan bo'lsa
            final_file_name = f"{safe_name}_{counter}.json"
            target_path = TESTS_DIR / final_file_name
            counter += 1

        target_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 4. Bot xotirasidagi testlarni qayta yuklash
        load_tests()
        await state.clear()

        await message.answer(
            f"✅ <b>Yangi test muvaffaqiyatli saqlandi va faollashtirildi!</b>\n\n"
            f"📘 <b>Nomi:</b> {html.escape(nomi)}\n"
            f"❓ <b>Savollar soni:</b> {len(savollar)} ta\n"
            f"⏱ <b>Ajratilgan vaqt:</b> {vaqt if vaqt else 'Cheklanmagan'} daqiqa\n"
            f"📁 <b>Fayl:</b> <code>tests/{final_file_name}</code>",
            reply_markup=admin_keyboard(),
        )

    except Exception as e:
        log.warning("Admin JSON yuklashda xatolik: %s", e)
        await message.answer(
            f"❌ <b>Faylda xatolik aniqlandi:</b>\n<code>{html.escape(str(e))}</code>\n\n"
            "Iltimos, faylni to'g'rilab qaytadan yuboring yoki /cancel bosing."
        )


@dp.message(AdminStates.waiting_for_json, Command("cancel"))
async def cancel_upload_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Yuklash bekor qilindi.", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "adm:list")
async def cb_admin_list(call: CallbackQuery) -> None:
    if not (call.from_user and call.from_user.id in ADMIN_IDS):
        await call.answer("Ruxsat berilmagan.", show_alert=True)
        return

    if not TESTS:
        await call.message.edit_text(
            "📋 <b>Hozirda hech qanday test mavjud emas.</b>",
            reply_markup=admin_keyboard(),
        )
        return

    rows = []
    for t_id, t in TESTS.items():
        rows.append([
            InlineKeyboardButton(text=f"📘 {t['nomi']} ({len(t['savollar'])} ta)", callback_data="noop"),
            InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"adm:del:{t_id}"),
        ])
    rows.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="adm:menu")])

    await call.message.edit_text(
        "📋 <b>Mavjud testlar ro'yxati:</b>\n"
        "Testni o'chirish uchun uning yonidagi '🗑 O'chirish' tugmasini bosing:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm:del:"))
async def cb_admin_del(call: CallbackQuery) -> None:
    if not (call.from_user and call.from_user.id in ADMIN_IDS):
        await call.answer("Ruxsat berilmagan.", show_alert=True)
        return

    test_id = call.data.split(":", 2)[2]
    test = TESTS.get(test_id)
    if not test:
        await call.answer("Test topilmadi.", show_alert=True)
        return

    confirm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Ha, o'chirilsin", callback_data=f"adm:confirm_del:{test_id}"),
                InlineKeyboardButton(text="❌ Yo'q, bekor qilish", callback_data="adm:list"),
            ]
        ]
    )
    await call.message.edit_text(
        f"⚠ Haqiqatan ham <b>\"{html.escape(test['nomi'])}\"</b> testini o'chirmoqchimisiz?\n"
        "Bu amalni ortga qaytarib bo'lmaydi!",
        reply_markup=confirm_kb,
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm:confirm_del:"))
async def cb_admin_confirm_del(call: CallbackQuery) -> None:
    if not (call.from_user and call.from_user.id in ADMIN_IDS):
        await call.answer("Ruxsat berilmagan.", show_alert=True)
        return

    test_id = call.data.split(":", 2)[2]
    target_file = TESTS_DIR / f"{test_id}.json"

    if target_file.exists():
        try:
            target_file.unlink()
        except Exception as e:
            await call.answer(f"O'chirishda xatolik: {e}", show_alert=True)
            return

    load_tests()
    await call.answer("Test o'chirildi!", show_alert=True)
    await cb_admin_list(call)


@dp.callback_query(F.data == "adm:stats")
async def cb_admin_stats(call: CallbackQuery) -> None:
    if not (call.from_user and call.from_user.id in ADMIN_IDS):
        await call.answer("Ruxsat berilmagan.", show_alert=True)
        return

    with get_db() as conn:
        users = conn.execute("SELECT COUNT(DISTINCT user_id) FROM natijalar").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM natijalar").fetchone()[0]

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Orqaga", callback_data="adm:menu")]]
    )
    await call.message.edit_text(
        f"📈 <b>Bot Statistikasi</b>\n\n"
        f"👤 Test yechgan o'quvchilar: <b>{users}</b> ta\n"
        f"📝 Jami urinishlar: <b>{total}</b> marta\n"
        f"📚 Mavjud testlar soni: <b>{len(TESTS)}</b> ta",
        reply_markup=back_kb,
    )
    await call.answer()


@dp.callback_query(F.data == "adm:reload")
async def cb_admin_reload(call: CallbackQuery) -> None:
    if not (call.from_user and call.from_user.id in ADMIN_IDS):
        await call.answer("Ruxsat berilmagan.", show_alert=True)
        return

    load_admins()
    errors = load_tests()
    msg = f"🔄 <b>{len(TESTS)} ta test</b> va adminlar ro'yxati qayta yuklandi."
    if errors:
        msg += "\n\n⚠ Xatolar:\n" + "\n".join(html.escape(e) for e in errors)

    await call.answer("Qayta yuklandi!", show_alert=False)
    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Orqaga", callback_data="adm:menu")]]
    )
    await call.message.edit_text(msg, reply_markup=back_kb)


@dp.callback_query(F.data == "adm:close")
async def cb_admin_close(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await call.message.delete()
    except TelegramBadRequest:
        await call.message.edit_text("Boshqaruv paneli yopildi.")
    await call.answer()


@dp.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


# --- Buyruqlar --------------------------------------------------------------
@dp.message(Command("yangilash"))
async def cmd_reload(message: Message) -> None:
    if not is_admin(message):
        return
    load_admins()
    errors = load_tests()
    text = f"🔄 <b>{len(TESTS)} ta test</b> va adminlar ro'yxati qayta yuklandi."
    if errors:
        text += "\n\n⚠ Xatolar:\n" + "\n".join(html.escape(e) for e in errors)
    await message.answer(text)


@dp.message(Command("statistika"))
async def cmd_stats(message: Message) -> None:
    if not is_admin(message):
        return
    with get_db() as conn:
        users = conn.execute("SELECT COUNT(DISTINCT user_id) FROM natijalar").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM natijalar").fetchone()[0]
    await message.answer(
        f"📈 <b>Bot Statistikasi</b>\n\n"
        f"👤 Test yechgan o'quvchilar: <b>{users}</b>\n"
        f"📝 Jami urinishlar soni: <b>{total}</b>\n"
        f"📚 Mavjud testlar: <b>{len(TESTS)} ta</b>"
    )


@dp.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else "Nomaʼlum"
    await message.answer(f"Sizning Telegram ID raqamingiz: <code>{user_id}</code>")


# ---------------------------------------------------------------------------
# Asosiy ishga tushirish funksiyasi
# ---------------------------------------------------------------------------
async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "XATOLIK: BOT_TOKEN topilmadi!\n"
            ".env.example faylidan nusxa olib .env yarating "
            "va unga @BotFather bergan tokenni yozing."
        )

    # 1. Baza va testlarni tayyorlash
    init_db()
    load_admins()
    load_tests()

    # 2. Botni sozlash
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    # 3. Telegram menyu buyruqlarini ro'yxatdan o'tkazish
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Botni ishga tushirish"),
            BotCommand(command="testlar", description="Mavjud testlar ro'yxati"),
            BotCommand(command="natijalarim", description="Mening natijalarim"),
            BotCommand(command="reyting", description="Eng yaxshi natijalar"),
            BotCommand(command="bekor", description="Joriy testni to'xtatish"),
        ])
    except Exception as e:
        log.warning("Menyu buyruqlarini o'rnatishda xatolik: %s", e)

    # 4. Server yoqilganda eski navbatdagi so'rovlarni tozalash (drop pending updates)
    await bot.delete_webhook(drop_pending_updates=True)

    log.info("Bot muvaffaqiyatli ishga tushdi.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot to'xtatildi.")
