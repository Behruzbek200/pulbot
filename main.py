#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Referral / Pul ishlash boti
PyTelegramBotAPI (telebot) + SQLite
Barcha funksiyalar bitta faylda
"""

import os
import telebot
from telebot import types
from telebot.handler_backends import State, StatesGroup
from telebot.storage import StateMemoryStorage
from telebot.apihelper import ApiTelegramException
from telebot.custom_filters import StateFilter
import sqlite3
import threading
import time
import datetime
from datetime import timezone
import re
import logging
import traceback
from typing import Optional, List, Dict, Any, Tuple
from functools import wraps

# Flask — webhook (Render) uchun
try:
    from flask import Flask, request
    FLASK_OK = True
except ImportError:
    FLASK_OK = False


def utc_now() -> datetime.datetime:
    """Timezone-aware UTC now (replaces deprecated utcnow)."""
    return datetime.datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()

# ==================== CONFIG ====================
# Render da Environment Variables orqali bering.
# Lokal/Termux da pastdagi qiymatlarni to'ldiring.

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
_admin_env = os.environ.get("ADMIN_IDS", "8261542613")
ADMIN_IDS = [int(x.strip()) for x in _admin_env.split(",") if x.strip().isdigit()]
if not ADMIN_IDS:
    ADMIN_IDS = [8261542613]

BOT_USERNAME = os.environ.get("BOT_USERNAME", "YourBotUsername")

# Webhook (Render): https://YOUR-APP.onrender.com
# Bo'sh qoldirilsa → polling (Termux/lokal)
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
PORT = int(os.environ.get("PORT", "10000"))

# Default sozlamalar (admin panel orqali o'zgartiriladi)
DEFAULT_SETTINGS = {
    "referral_sum": "800",
    "referral_wait_hours": "1",
    "min_withdraw": "8000",
    "admin_username": "admin",
    "payment_proof_channel": "",
    "bot_username": BOT_USERNAME,
    "withdraw_methods": "karta,telefon,boshqa",  # vergul bilan
    "mandatory_sub_enabled": "0",  # 0=o'chiq (kanal to'g'ri sozlangach admin yoqadi)
}

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ==================== BOT ====================
storage = StateMemoryStorage()
bot = telebot.TeleBot(BOT_TOKEN, state_storage=storage, parse_mode="HTML")
# State (kiritish jarayonlari) ishlashi uchun MAJBURIY
bot.add_custom_filter(StateFilter(bot))

# ==================== STATES ====================
class UserStates(StatesGroup):
    wait_phone = State()
    wait_card = State()
    wait_withdraw_amount = State()
    wait_withdraw_account = State()
    wait_withdraw_confirm = State()
    wait_bonus_claim = State()

class AdminStates(StatesGroup):
    wait_channel_username = State()
    wait_channel_forward = State()
    wait_channel_invite = State()
    wait_referral_sum = State()
    wait_referral_wait = State()
    wait_min_withdraw = State()
    wait_admin_username = State()
    wait_payment_channel = State()
    wait_bot_username = State()
    wait_withdraw_methods = State()
    wait_add_admin = State()
    wait_broadcast = State()
    wait_user_search = State()
    wait_balance_change = State()
    wait_user_message = State()
    wait_bonus_name = State()
    wait_bonus_sum = State()
    wait_bonus_start = State()
    wait_bonus_end = State()
    wait_bonus_limit = State()
    wait_reject_reason = State()

# ==================== DATABASE ====================
DB_PATH = "bot_database.db"
db_lock = threading.Lock()

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with db_lock:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            phone TEXT,
            card_number TEXT,
            balance REAL DEFAULT 0,
            referral_balance REAL DEFAULT 0,
            withdrawn_amount REAL DEFAULT 0,
            referred_by INTEGER,
            is_blocked INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # Eski bazaga card_number qo'shish
        try:
            cur.execute("ALTER TABLE users ADD COLUMN card_number TEXT")
        except Exception:
            pass

        cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inviter_id INTEGER NOT NULL,
            referred_id INTEGER UNIQUE NOT NULL,
            status TEXT DEFAULT 'pending',
            reward REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            confirmed_at TEXT,
            FOREIGN KEY (inviter_id) REFERENCES users(telegram_id),
            FOREIGN KEY (referred_id) REFERENCES users(telegram_id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            method TEXT NOT NULL,
            account TEXT NOT NULL,
            masked_account TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            paid_at TEXT,
            admin_id INTEGER,
            reject_reason TEXT,
            FOREIGN KEY (user_id) REFERENCES users(telegram_id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS mandatory_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            type TEXT NOT NULL,  -- public / private
            username TEXT,
            chat_id INTEGER,
            invite_link TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            role TEXT DEFAULT 'admin',  -- super / admin
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS bonuses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            start_time TEXT,
            end_time TEXT,
            max_users INTEGER DEFAULT 0,
            claimed_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS bonus_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bonus_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            claimed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(bonus_id, user_id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            message_type TEXT,
            content TEXT,
            sent_count INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # Default settings
        for k, v in DEFAULT_SETTINGS.items():
            cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

        # Super admins
        for aid in ADMIN_IDS:
            cur.execute(
                "INSERT OR IGNORE INTO admins (telegram_id, role) VALUES (?, 'super')",
                (aid,)
            )

        conn.commit()
        conn.close()
        logger.info("Database initialized")

def db_execute(query: str, params: tuple = (), fetchone=False, fetchall=False, commit=True):
    with db_lock:
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            result = None
            if fetchone:
                result = cur.fetchone()
            elif fetchall:
                result = cur.fetchall()
            if commit:
                conn.commit()
            return result
        except Exception as e:
            conn.rollback()
            logger.error(f"DB error: {e}\n{traceback.format_exc()}")
            raise
        finally:
            conn.close()

def get_setting(key: str, default: str = "") -> str:
    row = db_execute("SELECT value FROM settings WHERE key=?", (key,), fetchone=True)
    return row["value"] if row else default

def set_setting(key: str, value: str):
    db_execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value))
    )

def is_admin(telegram_id: int) -> bool:
    # Config dagi ADMIN_IDS ham tekshiriladi (DB dan oldin)
    if telegram_id in ADMIN_IDS:
        return True
    row = db_execute("SELECT id FROM admins WHERE telegram_id=?", (telegram_id,), fetchone=True)
    return row is not None

def is_super_admin(telegram_id: int) -> bool:
    if telegram_id in ADMIN_IDS:
        return True
    row = db_execute("SELECT role FROM admins WHERE telegram_id=?", (telegram_id,), fetchone=True)
    return row and row["role"] == "super"


def sync_admins_from_config():
    """Har ishga tushganda ADMIN_IDS ni bazaga yozadi."""
    for aid in ADMIN_IDS:
        try:
            db_execute(
                "INSERT OR IGNORE INTO admins (telegram_id, role) VALUES (?, 'super')",
                (int(aid),)
            )
            # Allaqachon bor bo'lsa ham super qilish
            db_execute(
                "UPDATE admins SET role='super' WHERE telegram_id=?",
                (int(aid),)
            )
        except Exception as e:
            logger.warning(f"Admin sync {aid}: {e}")

def get_user(telegram_id: int) -> Optional[sqlite3.Row]:
    return db_execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,), fetchone=True)

def create_user(telegram_id: int, username: str, first_name: str, referred_by: int = None):
    db_execute(
        """INSERT OR IGNORE INTO users (telegram_id, username, first_name, referred_by)
           VALUES (?, ?, ?, ?)""",
        (telegram_id, username, first_name, referred_by)
    )

def update_user_activity(telegram_id: int, username: str = None, first_name: str = None):
    if username or first_name:
        db_execute(
            "UPDATE users SET username=COALESCE(?, username), first_name=COALESCE(?, first_name), is_active=1 WHERE telegram_id=?",
            (username, first_name, telegram_id)
        )
    else:
        db_execute("UPDATE users SET is_active=1 WHERE telegram_id=?", (telegram_id,))

# ==================== HELPERS ====================
def fmt_money(amount) -> str:
    try:
        return f"{int(float(amount)):,}".replace(",", " ")
    except:
        return str(amount)

def mask_account(account: str, method: str) -> str:
    account = account.strip().replace(" ", "")
    if method == "karta" and len(account) >= 8:
        return account[:4] + "****" + account[-4:]
    if method == "telefon" and len(account) >= 7:
        return account[:4] + "****" + account[-3:]
    if len(account) > 6:
        return account[:3] + "****" + account[-3:]
    return "****"

def mark_user_inactive(telegram_id: int, reason: str = ""):
    """Deactivated / blocked userlarni bazada inactive qilish."""
    try:
        db_execute("UPDATE users SET is_active=0 WHERE telegram_id=?", (telegram_id,))
        logger.info(f"User {telegram_id} inactive qilindi. {reason}")
    except Exception as e:
        logger.warning(f"mark_user_inactive error: {e}")


def is_user_unreachable_error(err) -> bool:
    """403 deactivated / blocked / chat not found tekshiruvi."""
    msg = str(err).lower()
    return any(x in msg for x in (
        "user is deactivated",
        "bot was blocked by the user",
        "chat not found",
        "forbidden: bot was blocked",
        "forbidden: user is deactivated",
        "peer_id_invalid",
        "user_is_deactivated",
    ))


def safe_send(chat_id, text, reply_markup=None, parse_mode="HTML", **kwargs):
    """
    Xavfsiz xabar yuborish.
    Deactivated/blocked userlarga yuborishda exception chiqarmaydi,
    userni is_active=0 qiladi.
    """
    try:
        return bot.send_message(
            chat_id, text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            **kwargs
        )
    except ApiTelegramException as e:
        if is_user_unreachable_error(e):
            mark_user_inactive(chat_id, str(e))
            return None
        logger.error(f"safe_send ApiTelegramException to {chat_id}: {e}")
        return None
    except Exception as e:
        if is_user_unreachable_error(e):
            mark_user_inactive(chat_id, str(e))
            return None
        logger.error(f"safe_send error to {chat_id}: {e}")
        return None


def safe_edit(call, text, reply_markup=None):
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            safe_send(call.message.chat.id, text, reply_markup=reply_markup)


def answer_callback(call, text=None, show_alert=False):
    try:
        bot.answer_callback_query(call.id, text=text, show_alert=show_alert)
    except Exception:
        pass

# ==================== KEYBOARDS ====================
def main_menu_kb(user_id: int = None):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton("💰 Pul ishlash"),
        types.KeyboardButton("🎁 Bonus"),
        types.KeyboardButton("👤 Profil"),
        types.KeyboardButton("💸 Pul yechish"),
        types.KeyboardButton("💵 To‘lovlar"),
        types.KeyboardButton("🆘 Yordam"),
    )
    # Adminlar uchun alohida tugma
    if user_id and is_admin(user_id):
        kb.add(types.KeyboardButton("👑 Admin panel"))
    return kb


def profile_kb(has_card: bool = False):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(
        "💳 Karta qo‘shish / o‘zgartirish" if has_card else "💳 Karta qo‘shish",
        callback_data="add_card"
    ))
    kb.add(types.InlineKeyboardButton("📱 Telefon qo‘shish", callback_data="add_phone"))
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="back_main"))
    return kb

def back_kb(callback_data="back_main"):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data=callback_data))
    return kb

def is_valid_url(url: str) -> bool:
    """Telegram inline button uchun yaroqli URL."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    if url.rstrip("/") in (
        "https://t.me", "http://t.me",
        "https://telegram.me", "http://telegram.me",
    ):
        return False
    if len(url) < 15:
        return False
    return True


def mandatory_channels_kb(channels: List[sqlite3.Row]):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for ch in channels:
        title = ch["title"] or ch["username"] or "Kanal"
        url = None
        if ch["type"] == "public" and ch["username"]:
            uname = str(ch["username"]).lstrip("@").strip()
            if uname:
                url = f"https://t.me/{uname}"
                title = ch["title"] or uname
        else:
            link = (ch["invite_link"] or "").strip() if ch["invite_link"] else ""
            if is_valid_url(link):
                url = link
            title = ch["title"] or "Maxfiy kanal"

        if url and is_valid_url(url):
            kb.add(types.InlineKeyboardButton(f"🔗 {title}", url=url))
        else:
            # Noto'g'ri URL xato bermasligi uchun callback
            kb.add(types.InlineKeyboardButton(
                f"⚠️ {title} (link yo‘q)",
                callback_data=f"ch_info_{ch['id']}"
            ))
    kb.add(types.InlineKeyboardButton("✅ Obunani tekshirish", callback_data="check_subs"))
    return kb

def pul_ishlash_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📤 Do‘stga yuborish", callback_data="share_ref"),
        types.InlineKeyboardButton("👥 Referrallarim", callback_data="my_refs"),
        types.InlineKeyboardButton("↩️ Orqaga", callback_data="back_main"),
    )
    return kb

def withdraw_methods_kb(methods: List[str]):
    kb = types.InlineKeyboardMarkup(row_width=1)
    method_map = {
        "karta": "💳 Karta",
        "telefon": "📱 Telefon raqam",
        "boshqa": "👛 Boshqa usul",
    }
    for m in methods:
        m = m.strip().lower()
        if m in method_map:
            kb.add(types.InlineKeyboardButton(method_map[m], callback_data=f"wd_method_{m}"))
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="back_main"))
    return kb

def admin_panel_kb(is_super: bool = False):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📊 Statistika", callback_data="adm_stats"),
        types.InlineKeyboardButton("💰 Referral sozlamalari", callback_data="adm_ref_settings"),
    )
    kb.add(
        types.InlineKeyboardButton("💸 Pul yechishlar", callback_data="adm_withdrawals"),
        types.InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="adm_users"),
    )
    kb.add(
        types.InlineKeyboardButton("💵 To‘lovlar", callback_data="adm_payments"),
        types.InlineKeyboardButton("📢 Majburiy kanallar", callback_data="adm_channels"),
    )
    kb.add(
        types.InlineKeyboardButton("📨 Xabar yuborish", callback_data="adm_broadcast"),
        types.InlineKeyboardButton("💰 Yechishga tayyorlar", callback_data="adm_ready_users"),
    )
    if is_super:
        kb.add(
            types.InlineKeyboardButton("👨‍💼 Adminlar", callback_data="adm_admins"),
            types.InlineKeyboardButton("🎁 Bonuslar", callback_data="adm_bonuses"),
        )
        kb.add(types.InlineKeyboardButton("⚙️ Bot sozlamalari", callback_data="adm_settings"))
    kb.add(types.InlineKeyboardButton("❌ Yopish", callback_data="adm_close"))
    return kb

# ==================== MANDATORY SUBSCRIPTION ====================
def get_active_channels() -> List[sqlite3.Row]:
    return db_execute(
        "SELECT * FROM mandatory_channels WHERE is_active=1 ORDER BY id",
        fetchall=True
    ) or []


def enable_mandatory_sub():
    """Kanal qo'shilganda majburiy obunani yoqish."""
    set_setting("mandatory_sub_enabled", "1")
    logger.info("Majburiy obuna YOQILDI")


def check_user_subscriptions(user_id: int) -> Tuple[bool, List[sqlite3.Row]]:
    """
    Obuna bo'lmagan kanallarni qaytaradi.
    - mandatory_sub_enabled=0 → tekshirmaydi
    - member left/kicked → majburiy
    - API xato (bot admin emas) → baribir majburiy ko'rsatiladi (link orqali obuna)
    """
    if get_setting("mandatory_sub_enabled", "0") != "1":
        return True, []

    channels = get_active_channels()
    if not channels:
        return True, []

    not_joined = []
    for ch in channels:
        try:
            chat_id = ch["chat_id"]
            # Public: chat_id topish
            if not chat_id and ch["type"] == "public" and ch["username"]:
                username = str(ch["username"]).lstrip("@").strip()
                try:
                    chat = bot.get_chat(f"@{username}")
                    chat_id = chat.id
                    db_execute(
                        "UPDATE mandatory_channels SET chat_id=? WHERE id=?",
                        (chat_id, ch["id"])
                    )
                except Exception as e:
                    logger.warning(f"Kanal @{username} get_chat xato: {e}")
                    # Link bor — userga ko'rsatamiz
                    not_joined.append(ch)
                    continue

            if not chat_id:
                # chat_id yo'q, lekin invite/username bor bo'lsa ham ko'rsatamiz
                if ch["username"] or ch["invite_link"]:
                    not_joined.append(ch)
                continue

            try:
                member = bot.get_chat_member(chat_id, user_id)
                if member.status in ("left", "kicked"):
                    not_joined.append(ch)
                # member, administrator, creator, restricted → OK
            except Exception as e:
                # Bot kanalda admin emas — tekshirib bo'lmaydi, lekin majburiy ko'rsatamiz
                logger.warning(f"get_chat_member xato ch={ch['id']}: {e} — majburiy qilib ko'rsatiladi")
                not_joined.append(ch)
        except Exception as e:
            logger.warning(f"Sub check error channel {ch['id']}: {e}")
            not_joined.append(ch)

    return len(not_joined) == 0, not_joined


def require_subscription(func):
    @wraps(func)
    def wrapper(message_or_call, *args, **kwargs):
        if isinstance(message_or_call, types.CallbackQuery):
            user_id = message_or_call.from_user.id
            is_call = True
            chat_id = message_or_call.message.chat.id
        else:
            user_id = message_or_call.from_user.id
            is_call = False
            chat_id = message_or_call.chat.id

        if is_admin(user_id):
            return func(message_or_call, *args, **kwargs)

        ok, missing = check_user_subscriptions(user_id)
        if not ok and missing:
            text = "📢 Botdan foydalanish uchun quyidagi kanallarga obuna bo‘ling:\n\n"
            for ch in missing:
                title = ch["title"] or ch["username"] or "Kanal"
                text += f"• {title}\n"
            text += "\nObuna bo‘lgach ✅ Obunani tekshirish tugmasini bosing."
            kb = mandatory_channels_kb(missing)
            if is_call:
                try:
                    safe_edit(message_or_call, text, kb)
                except Exception:
                    safe_send(chat_id, text, reply_markup=kb)
                answer_callback(message_or_call)
            else:
                safe_send(chat_id, text, reply_markup=kb)
            return
        return func(message_or_call, *args, **kwargs)
    return wrapper

# ==================== REFERRAL LOGIC ====================
def process_referral(inviter_id: int, referred_id: int):
    """Referralni darhol tasdiqlash va inviter balansiga qo'shish."""
    if inviter_id == referred_id:
        return
    # Allaqachon referral sifatida hisoblanganmi?
    existing = db_execute(
        "SELECT id FROM referrals WHERE referred_id=?", (referred_id,), fetchone=True
    )
    if existing:
        return
    user = get_user(referred_id)
    if not user:
        return
    inviter = get_user(inviter_id)
    if not inviter:
        return
    try:
        if inviter["is_blocked"]:
            return
    except Exception:
        pass

    reward = float(get_setting("referral_sum", "800"))
    now = utc_now_iso()

    with db_lock:
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM referrals WHERE referred_id=?", (referred_id,))
            if cur.fetchone():
                conn.rollback()
                return
            cur.execute(
                """INSERT INTO referrals (inviter_id, referred_id, status, reward, confirmed_at)
                   VALUES (?, ?, 'confirmed', ?, ?)""",
                (inviter_id, referred_id, reward, now)
            )
            cur.execute(
                "UPDATE users SET balance = balance + ?, referral_balance = referral_balance + ? WHERE telegram_id=?",
                (reward, reward, inviter_id)
            )
            conn.commit()
            logger.info(f"Referral CONFIRMED: {inviter_id} -> {referred_id} (+{reward})")
        except Exception as e:
            conn.rollback()
            logger.error(f"process_referral error: {e}\n{traceback.format_exc()}")
            return
        finally:
            conn.close()

    safe_send(
        inviter_id,
        f"✅ <b>Yangi referral!</b>\n\n"
        f"👤 Yangi do‘st botga qo‘shildi.\n"
        f"💰 +{fmt_money(reward)} so‘m balansingizga qo‘shildi."
    )


def confirm_pending_referrals():
    """Eski pending referralarni (agar qolgan bo'lsa) darhol tasdiqlash."""
    while True:
        try:
            rows = db_execute(
                "SELECT * FROM referrals WHERE status='pending'",
                fetchall=True
            ) or []
            for r in rows:
                with db_lock:
                    conn = get_conn()
                    try:
                        cur = conn.cursor()
                        cur.execute(
                            "UPDATE referrals SET status='confirmed', confirmed_at=? WHERE id=? AND status='pending'",
                            (utc_now_iso(), r["id"])
                        )
                        if cur.rowcount:
                            cur.execute(
                                "UPDATE users SET balance = balance + ?, referral_balance = referral_balance + ? WHERE telegram_id=?",
                                (r["reward"], r["reward"], r["inviter_id"])
                            )
                            conn.commit()
                            safe_send(
                                r["inviter_id"],
                                f"✅ Referral tasdiqlandi!\n💰 +{fmt_money(r['reward'])} so‘m balansingizga qo‘shildi."
                            )
                    except Exception as e:
                        conn.rollback()
                        logger.error(f"Confirm ref error: {e}")
                    finally:
                        conn.close()
        except Exception as e:
            logger.error(f"Referral confirm loop error: {e}")
        time.sleep(30)

# ==================== START / MAIN MENU ====================
@bot.message_handler(commands=["start"])
def cmd_start(message: types.Message):
    try:
        user = message.from_user
        telegram_id = user.id
        username = user.username or ""
        first_name = user.first_name or ""

        # Parse referral
        referred_by = None
        parts = (message.text or "").split()
        if len(parts) > 1:
            try:
                ref_id = int(parts[1])
                if ref_id != telegram_id:
                    referred_by = ref_id
            except Exception:
                pass

        existing = get_user(telegram_id)
        if not existing:
            create_user(telegram_id, username, first_name, referred_by)
            if referred_by:
                process_referral(referred_by, telegram_id)
        else:
            update_user_activity(telegram_id, username, first_name)
            if existing["is_blocked"]:
                safe_send(message.chat.id, "🚫 Siz botdan bloklangansiz.")
                return

        # Check subscription
        ok, missing = check_user_subscriptions(telegram_id)
        if not ok and not is_admin(telegram_id):
            text = "📢 Botdan foydalanish uchun quyidagi kanallarga obuna bo‘ling:\n\n"
            for ch in missing:
                title = ch["title"] or ch["username"] or "Kanal"
                text += f"• {title}\n"
            text += "\nObuna bo‘lgach ✅ Obunani tekshirish tugmasini bosing."
            safe_send(message.chat.id, text, reply_markup=mandatory_channels_kb(missing))
            return

        safe_send(
            message.chat.id,
            "🏠 <b>Asosiy menyu</b>\n\nQuyidagi bo‘limlardan birini tanlang:",
            reply_markup=main_menu_kb(telegram_id)
        )
    except ApiTelegramException as e:
        if is_user_unreachable_error(e):
            mark_user_inactive(message.chat.id, str(e))
            logger.warning(f"start: user unreachable {message.chat.id}: {e}")
        else:
            logger.error(f"start ApiTelegramException: {e}\n{traceback.format_exc()}")
    except Exception as e:
        logger.error(f"start error: {e}\n{traceback.format_exc()}")
        try:
            safe_send(message.chat.id, "Xatolik yuz berdi. Qayta urinib ko‘ring.")
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data == "check_subs")
def cb_check_subs(call: types.CallbackQuery):
    try:
        ok, missing = check_user_subscriptions(call.from_user.id)
        if ok or is_admin(call.from_user.id):
            answer_callback(call, "✅ Obuna tasdiqlandi!")
            bot.send_message(
                call.message.chat.id,
                "🏠 <b>Asosiy menyu</b>\n\nQuyidagi bo‘limlardan birini tanlang:",
                reply_markup=main_menu_kb(call.from_user.id)
            )
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
        else:
            text = "❌ Siz hali barcha kanallarga obuna bo‘lmagansiz.\n\n"
            for ch in missing:
                title = ch["title"] or ch["username"] or "Kanal"
                text += f"• {title}\n"
            text += "\nObuna bo‘ling va qayta tekshiring."
            safe_edit(call, text, mandatory_channels_kb(missing))
            answer_callback(call, "Hali obuna bo‘lmagansiz", show_alert=True)
    except Exception as e:
        logger.error(f"check_subs: {e}")
        answer_callback(call, "Xatolik")

@bot.callback_query_handler(func=lambda c: c.data == "back_main")
def cb_back_main(call: types.CallbackQuery):
    try:
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        bot.delete_state(call.from_user.id, call.message.chat.id)
        answer_callback(call)
        bot.send_message(
            call.message.chat.id,
            "🏠 <b>Asosiy menyu</b>",
            reply_markup=main_menu_kb(call.from_user.id)
        )
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
    except Exception as e:
        logger.error(f"back_main: {e}")

# ==================== PUL ISHLASH ====================
@bot.message_handler(func=lambda m: m.text == "💰 Pul ishlash", state=None)
@require_subscription
def menu_pul_ishlash(message: types.Message):
    try:
        user = get_user(message.from_user.id)
        if not user:
            create_user(message.from_user.id, message.from_user.username or "", message.from_user.first_name or "")
            user = get_user(message.from_user.id)

        ref_sum = get_setting("referral_sum", "800")
        bot_uname = get_setting("bot_username", BOT_USERNAME)
        ref_link = f"https://t.me/{bot_uname}?start={message.from_user.id}"

        text = (
            f"💰 <b>Pul ishlash</b>\n\n"
            f"👥 Har bir taklif: <b>{fmt_money(ref_sum)} so‘m</b>\n\n"
            f"🔗 Sizning referral havolangiz:\n"
            f"<code>{ref_link}</code>\n\n"
            f"Havolani do‘stlaringizga yuboring va pul ishlang.\n\n"
            f"✅ Mukofot do‘stingiz botga kirishi bilan <b>darhol</b> beriladi."
        )
        bot.send_message(message.chat.id, text, reply_markup=pul_ishlash_kb())
    except Exception as e:
        logger.error(f"pul_ishlash: {e}")
        bot.send_message(message.chat.id, "Xatolik yuz berdi.")

@bot.callback_query_handler(func=lambda c: c.data == "share_ref")
def cb_share_ref(call: types.CallbackQuery):
    try:
        from urllib.parse import quote
        bot_uname = (get_setting("bot_username", BOT_USERNAME) or BOT_USERNAME).lstrip("@").strip()
        if not bot_uname or bot_uname in ("YourBotUsername", "yourbotusername", ""):
            # Token orqali username olishga urinish
            try:
                me = bot.get_me()
                bot_uname = me.username or bot_uname
                if me.username:
                    set_setting("bot_username", me.username)
            except Exception:
                pass
        ref_link = f"https://t.me/{bot_uname}?start={call.from_user.id}"
        share_text = quote("Do'stim, shu bot orqali pul ishla!")
        share_url = f"https://t.me/share/url?url={quote(ref_link, safe='')}&text={share_text}"
        kb = types.InlineKeyboardMarkup()
        if is_valid_url(share_url) and bot_uname and bot_uname != "YourBotUsername":
            kb.add(types.InlineKeyboardButton("📤 Do‘stga yuborish", url=share_url))
        else:
            kb.add(types.InlineKeyboardButton("📋 Havolani ko‘rish", callback_data="show_ref_link"))
        kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="back_main"))
        text = (
            "Do‘stlaringizga yuborish uchun tugmani bosing:\n\n"
            f"🔗 <code>{ref_link}</code>"
        )
        safe_edit(call, text, kb)
        answer_callback(call)
    except Exception as e:
        logger.error(f"share_ref: {e}\n{traceback.format_exc()}")
        answer_callback(call, "Xatolik")


@bot.callback_query_handler(func=lambda c: c.data == "show_ref_link")
def cb_show_ref_link(call: types.CallbackQuery):
    try:
        bot_uname = (get_setting("bot_username", BOT_USERNAME) or "").lstrip("@")
        ref_link = f"https://t.me/{bot_uname}?start={call.from_user.id}"
        answer_callback(call, ref_link, show_alert=True)
    except Exception as e:
        answer_callback(call, "Xatolik")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("ch_info_"))
def cb_ch_info(call: types.CallbackQuery):
    answer_callback(
        call,
        "Bu kanal uchun to‘g‘ri link yo‘q.\nAdmin panelidan kanalni qayta qo‘shing (invite link bilan).",
        show_alert=True
    )

@bot.callback_query_handler(func=lambda c: c.data == "my_refs")
def cb_my_refs(call: types.CallbackQuery):
    try:
        uid = call.from_user.id
        confirmed = db_execute(
            "SELECT COUNT(*) as c FROM referrals WHERE inviter_id=? AND status='confirmed'",
            (uid,), fetchone=True
        )["c"]
        pending = db_execute(
            "SELECT COUNT(*) as c FROM referrals WHERE inviter_id=? AND status='pending'",
            (uid,), fetchone=True
        )["c"]
        rejected = db_execute(
            "SELECT COUNT(*) as c FROM referrals WHERE inviter_id=? AND status='rejected'",
            (uid,), fetchone=True
        )["c"]
        ref_sum = get_setting("referral_sum", "800")
        earned = confirmed * float(ref_sum)

        text = (
            f"👥 <b>Referrallarim</b>\n\n"
            f"✅ Tasdiqlangan: <b>{confirmed}</b> ta\n"
            f"🟡 Tekshiruvda: <b>{pending}</b> ta\n"
            f"❌ Rad etilgan: <b>{rejected}</b> ta\n\n"
            f"💵 Har bir referral: <b>{fmt_money(ref_sum)} so‘m</b>\n"
            f"💰 Jami ishlangan: <b>{fmt_money(earned)} so‘m</b>\n\n"
            f"✅ Mukofot <b>darhol</b> beriladi."
        )
        safe_edit(call, text, back_kb())
        answer_callback(call)
    except Exception as e:
        logger.error(f"my_refs: {e}")
        answer_callback(call, "Xatolik")

# ==================== PROFIL ====================
@bot.message_handler(func=lambda m: m.text == "👤 Profil", state=None)
@require_subscription
def menu_profil(message: types.Message):
    try:
        user = get_user(message.from_user.id)
        if not user:
            bot.send_message(message.chat.id, "Profil topilmadi. /start bosing.")
            return

        conf_refs = db_execute(
            "SELECT COUNT(*) as c FROM referrals WHERE inviter_id=? AND status='confirmed'",
            (user["telegram_id"],), fetchone=True
        )["c"]

        name = user["first_name"] or user["username"] or "Foydalanuvchi"
        phone = user["phone"] or "Kiritilmagan"
        card_raw = None
        try:
            card_raw = user["card_number"]
        except Exception:
            card_raw = None
        if card_raw:
            card_show = mask_account(card_raw, "karta")
        else:
            card_show = "Kiritilmagan"

        text = (
            f"🏛 <b>Profilingiz:</b>\n\n"
            f"👤 User: {name}\n"
            f"🆔 ID raqam: <code>{user['telegram_id']}</code>\n"
            f"📱 Telefon: {phone}\n"
            f"💳 Karta: <code>{card_show}</code>\n\n"
            f"💰 Balans: <b>{fmt_money(user['balance'])} so‘m</b>\n"
            f"🔗 Do‘stlaringiz: <b>{conf_refs}</b> ta\n"
            f"💵 Referral daromad: <b>{fmt_money(user['referral_balance'])} so‘m</b>\n"
            f"💸 Yechib olgan: <b>{fmt_money(user['withdrawn_amount'])} so‘m</b>\n\n"
            f"💡 Pul yechish uchun kartani qo‘shing."
        )
        bot.send_message(
            message.chat.id,
            text,
            reply_markup=profile_kb(has_card=bool(card_raw))
        )
    except Exception as e:
        logger.error(f"profil: {e}\n{traceback.format_exc()}")
        bot.send_message(message.chat.id, "Xatolik yuz berdi.")


@bot.callback_query_handler(func=lambda c: c.data == "add_card")
@require_subscription
def cb_add_card(call: types.CallbackQuery):
    try:
        bot.set_state(call.from_user.id, UserStates.wait_card, call.message.chat.id)
        safe_edit(
            call,
            "💳 <b>Karta raqamingizni kiriting</b>\n\n"
            "16 raqamli karta (masalan: 8600123456789012)\n\n"
            "Bekor: /start"
        )
        answer_callback(call)
    except Exception as e:
        logger.error(f"add_card: {e}")
        answer_callback(call, "Xatolik")


@bot.message_handler(state=UserStates.wait_card, content_types=["text"])
def process_add_card(message: types.Message):
    try:
        raw = (message.text or "").strip().replace(" ", "").replace("-", "")
        if not raw.isdigit() or len(raw) < 12 or len(raw) > 20:
            bot.send_message(
                message.chat.id,
                "❌ Noto‘g‘ri karta. 12–20 raqam kiriting.\nQayta yozing yoki /start"
            )
            return
        db_execute(
            "UPDATE users SET card_number=? WHERE telegram_id=?",
            (raw, message.from_user.id)
        )
        bot.delete_state(message.from_user.id, message.chat.id)
        masked = mask_account(raw, "karta")
        bot.send_message(
            message.chat.id,
            f"✅ <b>Karta saqlandi!</b>\n\n💳 <code>{masked}</code>\n\n"
            f"Endi «💸 Pul yechish» orqali mablag‘ yechishingiz mumkin.",
            reply_markup=main_menu_kb(message.from_user.id)
        )
    except Exception as e:
        logger.error(f"process_add_card: {e}")
        bot.send_message(message.chat.id, "Xatolik yuz berdi.")
        try:
            bot.delete_state(message.from_user.id, message.chat.id)
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data == "add_phone")
@require_subscription
def cb_add_phone(call: types.CallbackQuery):
    try:
        bot.set_state(call.from_user.id, UserStates.wait_phone, call.message.chat.id)
        safe_edit(
            call,
            "📱 <b>Telefon raqamingizni kiriting</b>\n\n"
            "Masalan: +998901234567\n\nBekor: /start"
        )
        answer_callback(call)
    except Exception as e:
        logger.error(f"add_phone: {e}")
        answer_callback(call, "Xatolik")


@bot.message_handler(state=UserStates.wait_phone, content_types=["text"])
def process_add_phone(message: types.Message):
    try:
        phone = (message.text or "").strip().replace(" ", "")
        digits = re.sub(r"\D", "", phone)
        if len(digits) < 9:
            bot.send_message(message.chat.id, "❌ Noto‘g‘ri raqam. Qayta yozing yoki /start")
            return
        db_execute(
            "UPDATE users SET phone=? WHERE telegram_id=?",
            (phone, message.from_user.id)
        )
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(
            message.chat.id,
            f"✅ <b>Telefon saqlandi!</b>\n\n📱 {phone}",
            reply_markup=main_menu_kb(message.from_user.id)
        )
    except Exception as e:
        logger.error(f"process_add_phone: {e}")
        bot.send_message(message.chat.id, "Xatolik.")


# ==================== ADMIN PANEL TUGMASI ====================
@bot.message_handler(func=lambda m: m.text == "👑 Admin panel", state=None)
def menu_admin_btn(message: types.Message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Ruxsat yo‘q.", reply_markup=main_menu_kb(message.from_user.id))
        return
    try:
        try:
            bot.delete_state(message.from_user.id, message.chat.id)
        except Exception:
            pass
        is_super = is_super_admin(message.from_user.id)
        bot.send_message(
            message.chat.id,
            "👑 <b>ADMIN PANEL</b>\n\nBo‘limni tanlang:",
            reply_markup=admin_panel_kb(is_super)
        )
    except Exception as e:
        logger.error(f"admin_btn: {e}")

# ==================== PUL YECHISH ====================
@bot.message_handler(func=lambda m: m.text == "💸 Pul yechish", state=None)
@require_subscription
def menu_pul_yechish(message: types.Message):
    try:
        user = get_user(message.from_user.id)
        if not user:
            bot.send_message(message.chat.id, "Avval /start bosing.")
            return
        if user["is_blocked"]:
            bot.send_message(message.chat.id, "🚫 Siz bloklangansiz.")
            return

        balance = float(user["balance"])
        min_wd = float(get_setting("min_withdraw", "8000"))

        if balance < min_wd:
            text = (
                f"❌ <b>Pul yechish mumkin emas.</b>\n\n"
                f"💰 Balansingiz: <b>{fmt_money(balance)} so‘m</b>\n"
                f"🔻 Minimal yechish: <b>{fmt_money(min_wd)} so‘m</b>"
            )
            bot.send_message(message.chat.id, text, reply_markup=back_kb())
            return

        methods_str = get_setting("withdraw_methods", "karta,telefon,boshqa")
        methods = [m.strip() for m in methods_str.split(",") if m.strip()]
        text = (
            f"💸 <b>Pul yechish</b>\n\n"
            f"💰 Balansingiz: <b>{fmt_money(balance)} so‘m</b>\n"
            f"❌ Minimal yechish: <b>{fmt_money(min_wd)} so‘m</b>\n\n"
            f"💳 Pul yechish usulini tanlang:"
        )
        bot.send_message(message.chat.id, text, reply_markup=withdraw_methods_kb(methods))
    except Exception as e:
        logger.error(f"pul_yechish: {e}")
        bot.send_message(message.chat.id, "Xatolik yuz berdi.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("wd_method_"))
def cb_wd_method(call: types.CallbackQuery):
    try:
        method = call.data.replace("wd_method_", "")
        bot.set_state(call.from_user.id, UserStates.wait_withdraw_amount, call.message.chat.id)
        with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
            data["wd_method"] = method

        method_names = {"karta": "💳 Karta", "telefon": "📱 Telefon", "boshqa": "👛 Boshqa"}
        text = (
            f"💰 Yechmoqchi bo‘lgan summani kiriting (so‘m):\n\n"
            f"Usul: {method_names.get(method, method)}\n"
            f"Minimal: {fmt_money(get_setting('min_withdraw', '8000'))} so‘m"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ Bekor qilish", callback_data="back_main"))
        safe_edit(call, text, kb)
        answer_callback(call)
    except Exception as e:
        logger.error(f"wd_method: {e}")
        answer_callback(call, "Xatolik")

@bot.message_handler(state=UserStates.wait_withdraw_amount, content_types=["text"])
def process_wd_amount(message: types.Message):
    try:
        text = (message.text or "").strip().replace(" ", "").replace(",", "")
        if not text.isdigit():
            bot.send_message(message.chat.id, "❌ Faqat raqam kiriting. Qayta urinib ko‘ring:")
            return
        amount = float(text)
        user = get_user(message.from_user.id)
        min_wd = float(get_setting("min_withdraw", "8000"))
        if amount < min_wd:
            bot.send_message(message.chat.id, f"❌ Minimal summa: {fmt_money(min_wd)} so‘m")
            return
        if amount > float(user["balance"]):
            bot.send_message(message.chat.id, f"❌ Balansingiz yetarli emas. Mavjud: {fmt_money(user['balance'])} so‘m")
            return

        with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            data["wd_amount"] = amount
            method = data.get("wd_method", "karta")

        # Saqlangan karta / telefon bo'lsa — tanlash imkoniyati
        saved_card = None
        saved_phone = user["phone"] if user else None
        try:
            saved_card = user["card_number"] if user else None
        except Exception:
            saved_card = None

        if method == "karta" and saved_card:
            masked = mask_account(saved_card, "karta")
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton(
                f"✅ Saqlangan karta: {masked}",
                callback_data="wd_use_saved"
            ))
            kb.add(types.InlineKeyboardButton("✏️ Boshqa karta kiritish", callback_data="wd_enter_new"))
            kb.add(types.InlineKeyboardButton("❌ Bekor", callback_data="back_main"))
            with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
                data["wd_saved_account"] = saved_card
            bot.set_state(message.from_user.id, UserStates.wait_withdraw_confirm, message.chat.id)
            bot.send_message(
                message.chat.id,
                f"💳 Saqlangan kartangiz bor.\n\n"
                f"💰 Summa: <b>{fmt_money(amount)} so‘m</b>\n"
                f"Qaysi kartaga yechasiz?",
                reply_markup=kb
            )
            return

        if method == "telefon" and saved_phone:
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton(
                f"✅ Saqlangan: {saved_phone}",
                callback_data="wd_use_saved"
            ))
            kb.add(types.InlineKeyboardButton("✏️ Boshqa raqam", callback_data="wd_enter_new"))
            kb.add(types.InlineKeyboardButton("❌ Bekor", callback_data="back_main"))
            with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
                data["wd_saved_account"] = saved_phone
            bot.set_state(message.from_user.id, UserStates.wait_withdraw_confirm, message.chat.id)
            bot.send_message(
                message.chat.id,
                f"📱 Saqlangan telefon bor.\n\n💰 Summa: <b>{fmt_money(amount)} so‘m</b>",
                reply_markup=kb
            )
            return

        bot.set_state(message.from_user.id, UserStates.wait_withdraw_account, message.chat.id)
        prompts = {
            "karta": "💳 Karta raqamingizni kiriting (16 raqam):",
            "telefon": "📱 Telefon raqamingizni kiriting (+998...):",
            "boshqa": "👛 Hisob ma’lumotlarini kiriting:",
        }
        bot.send_message(message.chat.id, prompts.get(method, "Ma’lumotni kiriting:"))
    except Exception as e:
        logger.error(f"wd_amount: {e}\n{traceback.format_exc()}")
        bot.send_message(message.chat.id, "Xatolik. Qayta urinib ko‘ring.")
        bot.delete_state(message.from_user.id, message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data == "wd_use_saved")
def cb_wd_use_saved(call: types.CallbackQuery):
    try:
        with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
            account = data.get("wd_saved_account")
            amount = data.get("wd_amount")
            method = data.get("wd_method", "karta")
        if not account or not amount:
            answer_callback(call, "Ma’lumot yo‘qoldi. Qayta boshlang.", show_alert=True)
            return
        masked = mask_account(str(account), method)
        method_names = {"karta": "💳 Karta", "telefon": "📱 Telefon", "boshqa": "👛 Boshqa"}
        text = (
            f"📋 <b>Tasdiqlash</b>\n\n"
            f"💰 Summa: <b>{fmt_money(amount)} so‘m</b>\n"
            f"🔎 Usul: {method_names.get(method, method)}\n"
            f"💳 Hisob: <code>{masked}</code>\n\n"
            f"Ma’lumotlar to‘g‘rimi?"
        )
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Tasdiqlash", callback_data="wd_confirm"),
            types.InlineKeyboardButton("❌ Bekor", callback_data="back_main"),
        )
        with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
            data["wd_account"] = account
            data["wd_masked"] = masked
        bot.set_state(call.from_user.id, UserStates.wait_withdraw_confirm, call.message.chat.id)
        safe_edit(call, text, kb)
        answer_callback(call)
    except Exception as e:
        logger.error(f"wd_use_saved: {e}")
        answer_callback(call, "Xatolik")


@bot.callback_query_handler(func=lambda c: c.data == "wd_enter_new")
def cb_wd_enter_new(call: types.CallbackQuery):
    try:
        with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
            method = data.get("wd_method", "karta")
        bot.set_state(call.from_user.id, UserStates.wait_withdraw_account, call.message.chat.id)
        prompts = {
            "karta": "💳 Yangi karta raqamini kiriting:",
            "telefon": "📱 Yangi telefon raqamini kiriting:",
            "boshqa": "👛 Hisob ma’lumotlarini kiriting:",
        }
        safe_edit(call, prompts.get(method, "Ma’lumotni kiriting:"))
        answer_callback(call)
    except Exception as e:
        logger.error(f"wd_enter_new: {e}")
        answer_callback(call, "Xatolik")

@bot.message_handler(state=UserStates.wait_withdraw_account, content_types=["text"])
def process_wd_account(message: types.Message):
    try:
        account = (message.text or "").strip()
        if len(account) < 5:
            bot.send_message(message.chat.id, "❌ Ma’lumot juda qisqa. Qayta kiriting:")
            return

        with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            amount = data.get("wd_amount")
            method = data.get("wd_method", "karta")

        # Agar karta bo'lsa — profilga ham saqlash
        if method == "karta":
            digits = account.replace(" ", "").replace("-", "")
            if digits.isdigit() and len(digits) >= 12:
                db_execute(
                    "UPDATE users SET card_number=? WHERE telegram_id=?",
                    (digits, message.from_user.id)
                )
                account = digits

        masked = mask_account(account, method)
        method_names = {"karta": "💳 Karta", "telefon": "📱 Telefon", "boshqa": "👛 Boshqa"}

        text = (
            f"📋 <b>Tasdiqlash</b>\n\n"
            f"💰 Summa: <b>{fmt_money(amount)} so‘m</b>\n"
            f"🔎 Usul: {method_names.get(method, method)}\n"
            f"💳 Hisob: <code>{masked}</code>\n\n"
            f"Ma’lumotlar to‘g‘rimi?"
        )
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Tasdiqlash", callback_data="wd_confirm"),
            types.InlineKeyboardButton("❌ Bekor", callback_data="back_main"),
        )
        bot.set_state(message.from_user.id, UserStates.wait_withdraw_confirm, message.chat.id)
        with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            data["wd_account"] = account
            data["wd_masked"] = masked
        bot.send_message(message.chat.id, text, reply_markup=kb)
    except Exception as e:
        logger.error(f"wd_account: {e}")
        bot.send_message(message.chat.id, "Xatolik.")
        bot.delete_state(message.from_user.id, message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "wd_confirm")
def cb_wd_confirm(call: types.CallbackQuery):
    try:
        uid = call.from_user.id
        with bot.retrieve_data(uid, call.message.chat.id) as data:
            amount = data.get("wd_amount")
            method = data.get("wd_method")
            account = data.get("wd_account")
            masked = data.get("wd_masked")

        if not all([amount, method, account]):
            answer_callback(call, "Ma’lumotlar yo‘qoldi. Qayta boshlang.", show_alert=True)
            bot.delete_state(uid, call.message.chat.id)
            return

        user = get_user(uid)
        if not user or float(user["balance"]) < amount:
            answer_callback(call, "Balans yetarli emas!", show_alert=True)
            bot.delete_state(uid, call.message.chat.id)
            return

        # Transaction: hold balance
        with db_lock:
            conn = get_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE users SET balance = balance - ? WHERE telegram_id=? AND balance >= ?",
                    (amount, uid, amount)
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    answer_callback(call, "Balans yetarli emas yoki xato!", show_alert=True)
                    bot.delete_state(uid, call.message.chat.id)
                    return
                cur.execute(
                    """INSERT INTO withdrawals (user_id, amount, method, account, masked_account, status)
                       VALUES (?, ?, ?, ?, ?, 'pending')""",
                    (uid, amount, method, account, masked)
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error(f"wd create error: {e}")
                answer_callback(call, "Xatolik yuz berdi", show_alert=True)
                return
            finally:
                conn.close()

        bot.delete_state(uid, call.message.chat.id)
        safe_edit(
            call,
            f"✅ <b>So‘rov yuborildi!</b>\n\n"
            f"💰 Summa: {fmt_money(amount)} so‘m\n"
            f"⏳ Admin tekshiruvini kuting."
        )
        answer_callback(call, "So‘rov yuborildi!")

        # Notify admins
        for admin in db_execute("SELECT telegram_id FROM admins", fetchall=True) or []:
            safe_send(
                admin["telegram_id"],
                f"🆕 <b>Yangi pul yechish so‘rovi</b>\n\n"
                f"👤 User: {call.from_user.first_name} (@{call.from_user.username or '-'})\n"
                f"🆔 ID: <code>{uid}</code>\n"
                f"💰 {fmt_money(amount)} so‘m\n"
                f"💳 {masked}\n"
                f"Usul: {method}"
            )
    except Exception as e:
        logger.error(f"wd_confirm: {e}\n{traceback.format_exc()}")
        answer_callback(call, "Xatolik")

# ==================== TO'LOVLAR ====================
@bot.message_handler(func=lambda m: m.text == "💵 To‘lovlar", state=None)
@require_subscription
def menu_tolovlar(message: types.Message):
    try:
        channel = (get_setting("payment_proof_channel", "") or "").strip()
        text = "📄 <b>To‘lov isbotlari kanalimiz:</b>\n\n"
        kb = types.InlineKeyboardMarkup()
        url = None
        if channel:
            if channel.startswith("http://") or channel.startswith("https://"):
                url = channel
            elif channel.startswith("@"):
                url = f"https://t.me/{channel.lstrip('@')}"
            elif channel.lstrip("-").isdigit():
                # private channel ID — public URL bo'lmaydi
                text += f"Kanal ID: <code>{channel}</code>\n(Admin sozlagan)"
            else:
                url = f"https://t.me/{channel.lstrip('@')}"
            if url and is_valid_url(url):
                kb.add(types.InlineKeyboardButton("👁 Ko‘rish", url=url))
                text += f"🔗 {url}"
            elif not channel.lstrip("-").isdigit():
                text += "Kanal sozlangan, lekin link yaroqsiz."
        else:
            text += "Hozircha kanal sozlanmagan."
        kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="back_main"))
        bot.send_message(message.chat.id, text, reply_markup=kb)
    except Exception as e:
        logger.error(f"tolovlar: {e}\n{traceback.format_exc()}")
        bot.send_message(message.chat.id, "Xatolik.")

# ==================== BONUS ====================
@bot.message_handler(func=lambda m: m.text == "🎁 Bonus", state=None)
@require_subscription
def menu_bonus(message: types.Message):
    try:
        now = utc_now_iso()
        bonuses = db_execute(
            """SELECT * FROM bonuses WHERE is_active=1
               AND (start_time IS NULL OR start_time <= ?)
               AND (end_time IS NULL OR end_time >= ?)
               AND (max_users=0 OR claimed_count < max_users)
               ORDER BY id DESC""",
            (now, now), fetchall=True
        ) or []

        if not bonuses:
            bot.send_message(
                message.chat.id,
                "🎁 <b>BONUS</b>\n\nHozircha faol bonuslar yo‘q.",
                reply_markup=back_kb()
            )
            return

        text = "🎁 <b>Faol bonuslar:</b>\n\n"
        kb = types.InlineKeyboardMarkup(row_width=1)
        for b in bonuses:
            claimed = db_execute(
                "SELECT id FROM bonus_claims WHERE bonus_id=? AND user_id=?",
                (b["id"], message.from_user.id), fetchone=True
            )
            status = " ✅" if claimed else ""
            text += f"• {b['name']}: <b>{fmt_money(b['amount'])} so‘m</b>{status}\n"
            if not claimed:
                kb.add(types.InlineKeyboardButton(
                    f"🎁 {b['name']} olish",
                    callback_data=f"claim_bonus_{b['id']}"
                ))
        kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="back_main"))
        bot.send_message(message.chat.id, text, reply_markup=kb)
    except Exception as e:
        logger.error(f"bonus: {e}")
        bot.send_message(message.chat.id, "Xatolik.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("claim_bonus_"))
def cb_claim_bonus(call: types.CallbackQuery):
    try:
        bonus_id = int(call.data.replace("claim_bonus_", ""))
        uid = call.from_user.id
        now = utc_now_iso()

        with db_lock:
            conn = get_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    """SELECT * FROM bonuses WHERE id=? AND is_active=1
                       AND (start_time IS NULL OR start_time <= ?)
                       AND (end_time IS NULL OR end_time >= ?)
                       AND (max_users=0 OR claimed_count < max_users)""",
                    (bonus_id, now, now)
                )
                b = cur.fetchone()
                if not b:
                    answer_callback(call, "Bonus mavjud emas yoki tugagan.", show_alert=True)
                    return
                cur.execute(
                    "SELECT id FROM bonus_claims WHERE bonus_id=? AND user_id=?",
                    (bonus_id, uid)
                )
                if cur.fetchone():
                    answer_callback(call, "Siz allaqachon olgansiz!", show_alert=True)
                    return
                cur.execute(
                    "INSERT INTO bonus_claims (bonus_id, user_id) VALUES (?, ?)",
                    (bonus_id, uid)
                )
                cur.execute(
                    "UPDATE bonuses SET claimed_count = claimed_count + 1 WHERE id=?",
                    (bonus_id,)
                )
                cur.execute(
                    "UPDATE users SET balance = balance + ? WHERE telegram_id=?",
                    (b["amount"], uid)
                )
                conn.commit()
                answer_callback(call, f"✅ {fmt_money(b['amount'])} so‘m olindi!", show_alert=True)
                safe_edit(call, f"✅ Bonus olindi: <b>{fmt_money(b['amount'])} so‘m</b>")
            except Exception as e:
                conn.rollback()
                logger.error(f"claim_bonus: {e}")
                answer_callback(call, "Xatolik", show_alert=True)
            finally:
                conn.close()
    except Exception as e:
        logger.error(f"claim_bonus outer: {e}")
        answer_callback(call, "Xatolik")

# ==================== YORDAM ====================
@bot.message_handler(func=lambda m: m.text == "🆘 Yordam", state=None)
@require_subscription
def menu_yordam(message: types.Message):
    try:
        admin_uname = get_setting("admin_username", "admin")
        text = (
            "🆘 <b>Yordam</b>\n\n"
            "💰 <b>Pul ishlash</b> — referral havolangiz orqali daromad oling.\n"
            "🎁 <b>Bonus</b> — mavjud bonuslarni ko‘ring.\n"
            "💸 <b>Pul yechish</b> — balansdan mablag‘ yeching.\n"
            "💵 <b>To‘lovlar</b> — to‘lov isbotlarini ko‘ring.\n\n"
            f"👨‍💼 Admin: @{admin_uname.lstrip('@')}"
        )
        bot.send_message(message.chat.id, text, reply_markup=back_kb())
    except Exception as e:
        logger.error(f"yordam: {e}")
        bot.send_message(message.chat.id, "Xatolik.")

# ==================== ADMIN PANEL ====================
def admin_only(func):
    @wraps(func)
    def wrapper(message_or_call, *args, **kwargs):
        if isinstance(message_or_call, types.CallbackQuery):
            uid = message_or_call.from_user.id
            is_call = True
        else:
            uid = message_or_call.from_user.id
            is_call = False
        if not is_admin(uid):
            if is_call:
                answer_callback(message_or_call, "Ruxsat yo‘q!", show_alert=True)
            else:
                bot.send_message(message_or_call.chat.id, "❌ Ruxsat yo‘q.")
            return
        return func(message_or_call, *args, **kwargs)
    return wrapper

@bot.message_handler(commands=["admin"])
@admin_only
def cmd_admin(message: types.Message):
    try:
        try:
            bot.delete_state(message.from_user.id, message.chat.id)
        except Exception:
            pass
        is_super = is_super_admin(message.from_user.id)
        bot.send_message(
            message.chat.id,
            "👑 <b>ADMIN PANEL</b>\n\nBo‘limni tanlang:",
            reply_markup=admin_panel_kb(is_super)
        )
    except Exception as e:
        logger.error(f"admin: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "adm_close")
@admin_only
def cb_adm_close(call: types.CallbackQuery):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
        answer_callback(call)
    except:
        answer_callback(call)

@bot.callback_query_handler(func=lambda c: c.data == "adm_stats")
@admin_only
def cb_adm_stats(call: types.CallbackQuery):
    try:
        now = utc_now()
        today = now.strftime("%Y-%m-%d")
        yesterday = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        week_ago = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        month_ago = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%d")

        total_users = db_execute("SELECT COUNT(*) as c FROM users", fetchone=True)["c"]
        today_users = db_execute(
            "SELECT COUNT(*) as c FROM users WHERE date(created_at)=?", (today,), fetchone=True
        )["c"]
        yest_users = db_execute(
            "SELECT COUNT(*) as c FROM users WHERE date(created_at)=?", (yesterday,), fetchone=True
        )["c"]
        week_users = db_execute(
            "SELECT COUNT(*) as c FROM users WHERE date(created_at)>=?", (week_ago,), fetchone=True
        )["c"]
        month_users = db_execute(
            "SELECT COUNT(*) as c FROM users WHERE date(created_at)>=?", (month_ago,), fetchone=True
        )["c"]

        total_ref = db_execute("SELECT COUNT(*) as c FROM referrals", fetchone=True)["c"]
        conf_ref = db_execute("SELECT COUNT(*) as c FROM referrals WHERE status='confirmed'", fetchone=True)["c"]
        pend_ref = db_execute("SELECT COUNT(*) as c FROM referrals WHERE status='pending'", fetchone=True)["c"]
        rej_ref = db_execute("SELECT COUNT(*) as c FROM referrals WHERE status='rejected'", fetchone=True)["c"]

        ref_sum_total = db_execute(
            "SELECT COALESCE(SUM(reward),0) as s FROM referrals WHERE status='confirmed'",
            fetchone=True
        )["s"]
        withdrawn_total = db_execute(
            "SELECT COALESCE(SUM(amount),0) as s FROM withdrawals WHERE status='paid'",
            fetchone=True
        )["s"]
        pending_wd = db_execute(
            "SELECT COALESCE(SUM(amount),0) as s FROM withdrawals WHERE status='pending'",
            fetchone=True
        )["s"]

        min_wd = get_setting("min_withdraw", "8000")
        ref_sum = get_setting("referral_sum", "800")
        ready_count = db_execute(
            "SELECT COUNT(*) as c FROM users WHERE balance >= ? AND is_blocked=0",
            (float(min_wd),), fetchone=True
        )["c"]

        text = (
            f"📊 <b>STATISTIKA</b>\n\n"
            f"👥 Jami foydalanuvchilar: <b>{total_users}</b>\n"
            f"🟢 Bugungi yangi userlar: <b>{today_users}</b>\n"
            f"📅 Kechagi userlar: <b>{yest_users}</b>\n"
            f"📈 Haftalik: <b>{week_users}</b>\n"
            f"📈 Oylik: <b>{month_users}</b>\n\n"
            f"🔗 Jami referral: <b>{total_ref}</b>\n"
            f"✅ Tasdiqlangan: <b>{conf_ref}</b>\n"
            f"🟡 Tekshiruvda: <b>{pend_ref}</b>\n"
            f"❌ Rad etilgan: <b>{rej_ref}</b>\n\n"
            f"💰 Referral uchun berilgan jami: <b>{fmt_money(ref_sum_total)} so‘m</b>\n"
            f"💳 Jami yechilgan: <b>{fmt_money(withdrawn_total)} so‘m</b>\n"
            f"⏳ Kutilayotgan withdrawal: <b>{fmt_money(pending_wd)} so‘m</b>\n\n"
            f"💵 Minimal yechish: <b>{fmt_money(min_wd)} so‘m</b>\n"
            f"💰 Har bir referral: <b>{fmt_money(ref_sum)} so‘m</b>\n\n"
            f"👥 Minimal summadan oshganlar: <b>{ready_count}</b> ta"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="adm_back"))
        safe_edit(call, text, kb)
        answer_callback(call)
    except Exception as e:
        logger.error(f"adm_stats: {e}\n{traceback.format_exc()}")
        answer_callback(call, "Xatolik")

@bot.callback_query_handler(func=lambda c: c.data == "adm_back")
@admin_only
def cb_adm_back(call: types.CallbackQuery):
    try:
        is_super = is_super_admin(call.from_user.id)
        safe_edit(call, "👑 <b>ADMIN PANEL</b>\n\nBo‘limni tanlang:", admin_panel_kb(is_super))
        answer_callback(call)
        bot.delete_state(call.from_user.id, call.message.chat.id)
    except Exception as e:
        logger.error(f"adm_back: {e}")

def admin_done(message, text: str):
    """Admin amalidan keyin tasdiq + panel."""
    try:
        bot.delete_state(message.from_user.id, message.chat.id)
    except Exception:
        pass
    bot.send_message(message.chat.id, text)
    is_super = is_super_admin(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "👑 <b>ADMIN PANEL</b>\n\nBo‘limni tanlang:",
        reply_markup=admin_panel_kb(is_super)
    )


# ----- Referral sozlamalari -----
@bot.callback_query_handler(func=lambda c: c.data == "adm_ref_settings")
@admin_only
def cb_adm_ref_settings(call: types.CallbackQuery):
    try:
        ref_sum = get_setting("referral_sum", "800")
        min_wd = get_setting("min_withdraw", "8000")
        text = (
            f"💰 <b>Referral sozlamalari</b>\n\n"
            f"💵 Har bir referral: <b>{fmt_money(ref_sum)} so‘m</b>\n"
            f"💸 Minimal yechish: <b>{fmt_money(min_wd)} so‘m</b>\n\n"
            f"✅ Referral mukofoti <b>darhol</b> beriladi (kutish yo‘q)."
        )
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("💵 Referral summasini o‘zgartirish", callback_data="adm_set_ref_sum"),
            types.InlineKeyboardButton("💸 Minimal yechishni o‘zgartirish", callback_data="adm_set_min_wd"),
            types.InlineKeyboardButton("↩️ Orqaga", callback_data="adm_back"),
        )
        safe_edit(call, text, kb)
        answer_callback(call)
    except Exception as e:
        logger.error(f"adm_ref_settings: {e}")
        answer_callback(call, "Xatolik")

@bot.callback_query_handler(func=lambda c: c.data == "adm_set_ref_sum")
@admin_only
def cb_set_ref_sum(call: types.CallbackQuery):
    try:
        bot.set_state(call.from_user.id, AdminStates.wait_referral_sum, call.message.chat.id)
        safe_edit(
            call,
            "💵 <b>Yangi referral summasini kiriting</b>\n\n"
            "Faqat raqam yozing (masalan: 800)\n"
            "Bekor qilish: /admin"
        )
        answer_callback(call)
    except Exception as e:
        logger.error(f"cb_set_ref_sum: {e}")
        answer_callback(call, "Xatolik")

@bot.message_handler(state=AdminStates.wait_referral_sum, content_types=["text"])
@admin_only
def process_ref_sum(message: types.Message):
    try:
        raw = (message.text or "").strip()
        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            bot.send_message(message.chat.id, "❌ Faqat raqam yuboring (masalan: 800)\nBekor: /admin")
            return
        val = digits
        set_setting("referral_sum", val)
        admin_done(message, f"✅ <b>Saqlandi!</b>\n\n💵 Referral summasi: <b>{fmt_money(val)} so‘m</b>")
    except Exception as e:
        logger.error(f"set_ref_sum: {e}\n{traceback.format_exc()}")
        bot.send_message(message.chat.id, f"❌ Xatolik: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "adm_set_min_wd")
@admin_only
def cb_set_min_wd(call: types.CallbackQuery):
    try:
        bot.set_state(call.from_user.id, AdminStates.wait_min_withdraw, call.message.chat.id)
        safe_edit(
            call,
            "💸 <b>Minimal yechish summasini kiriting</b>\n\n"
            "Faqat raqam (masalan: 8000)\n"
            "Bekor: /admin"
        )
        answer_callback(call)
    except Exception as e:
        logger.error(f"cb_set_min_wd: {e}")
        answer_callback(call, "Xatolik")

@bot.message_handler(state=AdminStates.wait_min_withdraw, content_types=["text"])
@admin_only
def process_min_wd(message: types.Message):
    try:
        raw = (message.text or "").strip()
        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            bot.send_message(message.chat.id, "❌ Faqat raqam yuboring (masalan: 8000)\nBekor: /admin")
            return
        val = digits
        set_setting("min_withdraw", val)
        admin_done(message, f"✅ <b>Saqlandi!</b>\n\n💸 Minimal yechish: <b>{fmt_money(val)} so‘m</b>")
    except Exception as e:
        logger.error(f"set_min_wd: {e}\n{traceback.format_exc()}")
        bot.send_message(message.chat.id, f"❌ Xatolik: {e}")

# ----- Withdrawals admin -----
@bot.callback_query_handler(func=lambda c: c.data == "adm_withdrawals")
@admin_only
def cb_adm_withdrawals(call: types.CallbackQuery):
    try:
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("🟡 Kutilayotgan", callback_data="adm_wd_list_pending"),
            types.InlineKeyboardButton("✅ To‘langan", callback_data="adm_wd_list_paid"),
            types.InlineKeyboardButton("❌ Rad etilgan", callback_data="adm_wd_list_rejected"),
            types.InlineKeyboardButton("↩️ Orqaga", callback_data="adm_back"),
        )
        safe_edit(call, "💸 <b>Pul yechishlar</b>\n\nStatusni tanlang:", kb)
        answer_callback(call)
    except Exception as e:
        logger.error(f"adm_wd: {e}")

def show_wd_list(call, status: str, page: int = 0):
    limit = 5
    offset = page * limit
    rows = db_execute(
        "SELECT * FROM withdrawals WHERE status=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (status, limit, offset), fetchall=True
    ) or []
    total = db_execute(
        "SELECT COUNT(*) as c FROM withdrawals WHERE status=?", (status,), fetchone=True
    )["c"]

    status_names = {"pending": "🟡 Kutilayotgan", "paid": "✅ To‘langan", "rejected": "❌ Rad etilgan"}
    text = f"💸 <b>{status_names.get(status, status)}</b> (jami: {total})\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)

    if not rows:
        text += "Bo‘sh."
    else:
        for r in rows:
            user = get_user(r["user_id"])
            uname = (user["username"] if user else None) or (user["first_name"] if user else "Noma’lum")
            text += (
                f"#{r['id']} | 👤 {uname}\n"
                f"🆔 <code>{r['user_id']}</code>\n"
                f"💰 {fmt_money(r['amount'])} so‘m | {r['method']}\n"
                f"💳 {r['masked_account']}\n"
                f"📅 {r['created_at'][:16]}\n\n"
            )
            if status == "pending":
                kb.add(
                    types.InlineKeyboardButton(
                        f"✅ To‘lash #{r['id']}", callback_data=f"adm_wd_pay_{r['id']}"
                    ),
                    types.InlineKeyboardButton(
                        f"❌ Rad #{r['id']}", callback_data=f"adm_wd_rej_{r['id']}"
                    ),
                )

    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️", callback_data=f"adm_wd_page_{status}_{page-1}"))
    if offset + limit < total:
        nav.append(types.InlineKeyboardButton("➡️", callback_data=f"adm_wd_page_{status}_{page+1}"))
    if nav:
        kb.row(*nav)
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="adm_withdrawals"))
    safe_edit(call, text, kb)
    answer_callback(call)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_wd_list_"))
@admin_only
def cb_wd_list(call: types.CallbackQuery):
    status = call.data.replace("adm_wd_list_", "")
    show_wd_list(call, status, 0)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_wd_page_"))
@admin_only
def cb_wd_page(call: types.CallbackQuery):
    parts = call.data.split("_")
    status = parts[3]
    page = int(parts[4])
    show_wd_list(call, status, page)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_wd_pay_"))
@admin_only
def cb_wd_pay(call: types.CallbackQuery):
    try:
        wd_id = int(call.data.replace("adm_wd_pay_", ""))
        with db_lock:
            conn = get_conn()
            try:
                cur = conn.cursor()
                cur.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wd_id,))
                wd = cur.fetchone()
                if not wd:
                    answer_callback(call, "So‘rov topilmadi yoki allaqachon ishlangan.", show_alert=True)
                    return

                now = utc_now_iso()
                cur.execute(
                    "UPDATE withdrawals SET status='paid', paid_at=?, admin_id=? WHERE id=? AND status='pending'",
                    (now, call.from_user.id, wd_id)
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    answer_callback(call, "Allaqachon ishlangan.", show_alert=True)
                    return

                cur.execute(
                    "UPDATE users SET withdrawn_amount = withdrawn_amount + ? WHERE telegram_id=?",
                    (wd["amount"], wd["user_id"])
                )
                conn.commit()

                # Notify user
                safe_send(
                    wd["user_id"],
                    f"✅ <b>Pul yechish tasdiqlandi!</b>\n\n"
                    f"💰 Summa: {fmt_money(wd['amount'])} so‘m\n"
                    f"💳 {wd['masked_account']}\n\n"
                    f"To‘lov amalga oshirildi."
                )

                # Payment proof channel
                proof_ch = get_setting("payment_proof_channel", "")
                if proof_ch:
                    try:
                        method_names = {"karta": "💳 Karta", "telefon": "📱 Telefon", "boshqa": "👛 Boshqa"}
                        proof_text = (
                            f"📋 <b>To‘lov isboti</b>\n\n"
                            f"🤵 Foydalanuvchi: <code>{wd['user_id']}</code>\n"
                            f"💰 Miqdor: <b>{fmt_money(wd['amount'])} so‘m</b>\n"
                            f"💳 Hisob raqam: <code>{wd['masked_account']}</code>\n"
                            f"🔎 Yechish turi: {method_names.get(wd['method'], wd['method'])}\n"
                            f"📤 Holati: ✅ To‘langan\n\n"
                            f"Bot orqali pul yechib olindi."
                        )
                        bot.send_message(proof_ch, proof_text)
                    except Exception as e:
                        logger.error(f"proof channel error: {e}")

                answer_callback(call, "✅ To‘landi!", show_alert=True)
                show_wd_list(call, "pending", 0)
            except Exception as e:
                conn.rollback()
                logger.error(f"wd_pay: {e}")
                answer_callback(call, "Xatolik", show_alert=True)
            finally:
                conn.close()
    except Exception as e:
        logger.error(f"wd_pay outer: {e}")
        answer_callback(call, "Xatolik")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_wd_rej_"))
@admin_only
def cb_wd_rej(call: types.CallbackQuery):
    try:
        wd_id = int(call.data.replace("adm_wd_rej_", ""))
        bot.set_state(call.from_user.id, AdminStates.wait_reject_reason, call.message.chat.id)
        with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
            data["rej_wd_id"] = wd_id
        safe_edit(call, f"❌ #{wd_id} ni rad etish sababi (yoki - deb yozing):")
        answer_callback(call)
    except Exception as e:
        logger.error(f"wd_rej: {e}")

@bot.message_handler(state=AdminStates.wait_reject_reason)
@admin_only
def process_wd_reject(message: types.Message):
    try:
        with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            wd_id = data.get("rej_wd_id")
        reason = message.text.strip() if message.text.strip() != "-" else ""

        with db_lock:
            conn = get_conn()
            try:
                cur = conn.cursor()
                cur.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wd_id,))
                wd = cur.fetchone()
                if not wd:
                    bot.send_message(message.chat.id, "So‘rov topilmadi.")
                    bot.delete_state(message.from_user.id, message.chat.id)
                    return

                cur.execute(
                    "UPDATE withdrawals SET status='rejected', admin_id=?, reject_reason=? WHERE id=? AND status='pending'",
                    (message.from_user.id, reason, wd_id)
                )
                if cur.rowcount:
                    cur.execute(
                        "UPDATE users SET balance = balance + ? WHERE telegram_id=?",
                        (wd["amount"], wd["user_id"])
                    )
                    conn.commit()
                    safe_send(
                        wd["user_id"],
                        f"❌ <b>Pul yechish rad etildi.</b>\n\n"
                        f"💰 Summa: {fmt_money(wd['amount'])} so‘m qaytarildi.\n"
                        f"{('Sabab: ' + reason) if reason else ''}"
                    )
                    bot.send_message(message.chat.id, f"✅ #{wd_id} rad etildi, summa qaytarildi.")
                else:
                    conn.rollback()
                    bot.send_message(message.chat.id, "Allaqachon ishlangan.")
            except Exception as e:
                conn.rollback()
                logger.error(f"reject: {e}")
                bot.send_message(message.chat.id, "Xatolik.")
            finally:
                conn.close()
        bot.delete_state(message.from_user.id, message.chat.id)
    except Exception as e:
        logger.error(f"process_reject: {e}")

# ----- Ready users -----
@bot.callback_query_handler(func=lambda c: c.data == "adm_ready_users")
@admin_only
def cb_adm_ready(call: types.CallbackQuery):
    show_ready_users(call, 0)

def show_ready_users(call, page: int = 0):
    min_wd = float(get_setting("min_withdraw", "8000"))
    limit = 5
    offset = page * limit
    rows = db_execute(
        """SELECT u.*, (SELECT COUNT(*) FROM referrals r WHERE r.inviter_id=u.telegram_id AND r.status='confirmed') as ref_count
           FROM users u WHERE u.balance >= ? AND u.is_blocked=0 ORDER BY u.balance DESC LIMIT ? OFFSET ?""",
        (min_wd, limit, offset), fetchall=True
    ) or []
    total = db_execute(
        "SELECT COUNT(*) as c FROM users WHERE balance >= ? AND is_blocked=0",
        (min_wd,), fetchone=True
    )["c"]

    text = f"💰 <b>Yechishga tayyor foydalanuvchilar</b> (jami: {total})\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)
    if not rows:
        text += "Hozircha yo‘q."
    else:
        for i, u in enumerate(rows, start=offset + 1):
            uname = u["username"] or u["first_name"] or "Noma’lum"
            text += (
                f"{i}. 👤 @{uname if u['username'] else uname}\n"
                f"🆔 <code>{u['telegram_id']}</code>\n"
                f"💰 Balans: <b>{fmt_money(u['balance'])} so‘m</b>\n"
                f"👥 Referral: {u['ref_count']} ta\n\n"
            )
            kb.add(types.InlineKeyboardButton(
                f"👤 {uname[:20]}", callback_data=f"adm_user_{u['telegram_id']}"
            ))

    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️", callback_data=f"adm_ready_page_{page-1}"))
    if offset + limit < total:
        nav.append(types.InlineKeyboardButton("➡️", callback_data=f"adm_ready_page_{page+1}"))
    if nav:
        kb.row(*nav)
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="adm_back"))
    safe_edit(call, text, kb)
    answer_callback(call)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_ready_page_"))
@admin_only
def cb_ready_page(call: types.CallbackQuery):
    page = int(call.data.replace("adm_ready_page_", ""))
    show_ready_users(call, page)

# ----- Users management -----
@bot.callback_query_handler(func=lambda c: c.data == "adm_users")
@admin_only
def cb_adm_users(call: types.CallbackQuery):
    try:
        bot.set_state(call.from_user.id, AdminStates.wait_user_search, call.message.chat.id)
        safe_edit(
            call,
            "👥 <b>Foydalanuvchilar</b>\n\n"
            "Qidirish uchun Telegram ID, username yoki ism yuboring:"
        )
        answer_callback(call)
    except Exception as e:
        logger.error(f"adm_users: {e}")

@bot.message_handler(state=AdminStates.wait_user_search)
@admin_only
def process_user_search(message: types.Message):
    try:
        q = message.text.strip().lstrip("@")
        rows = []
        if q.isdigit():
            rows = db_execute("SELECT * FROM users WHERE telegram_id=?", (int(q),), fetchall=True) or []
        else:
            rows = db_execute(
                "SELECT * FROM users WHERE username LIKE ? OR first_name LIKE ? LIMIT 10",
                (f"%{q}%", f"%{q}%"), fetchall=True
            ) or []

        if not rows:
            bot.send_message(message.chat.id, "Foydalanuvchi topilmadi.")
            return

        for u in rows:
            conf = db_execute(
                "SELECT COUNT(*) as c FROM referrals WHERE inviter_id=? AND status='confirmed'",
                (u["telegram_id"],), fetchone=True
            )["c"]
            inviter = u["referred_by"] or "—"
            text = (
                f"👤 <b>{u['first_name'] or u['username'] or 'User'}</b>\n"
                f"🆔 <code>{u['telegram_id']}</code>\n"
                f"📱 {u['phone'] or 'Kiritilmagan'}\n"
                f"💰 Balans: <b>{fmt_money(u['balance'])} so‘m</b>\n"
                f"👥 Referral: {conf} ta\n"
                f"💸 Yechib olgan: {fmt_money(u['withdrawn_amount'])} so‘m\n"
                f"📅 Ro‘yxat: {u['created_at'][:16]}\n"
                f"🔗 Kim orqali: {inviter}\n"
                f"{'🚫 BLOKLANGAN' if u['is_blocked'] else '✅ Aktiv'}"
            )
            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.add(
                types.InlineKeyboardButton("💰 +", callback_data=f"adm_bal_add_{u['telegram_id']}"),
                types.InlineKeyboardButton("💰 −", callback_data=f"adm_bal_sub_{u['telegram_id']}"),
            )
            kb.add(
                types.InlineKeyboardButton("📩 Xabar", callback_data=f"adm_msg_{u['telegram_id']}"),
            )
            if u["is_blocked"]:
                kb.add(types.InlineKeyboardButton("✅ Blokdan chiqarish", callback_data=f"adm_unblock_{u['telegram_id']}"))
            else:
                kb.add(types.InlineKeyboardButton("🚫 Bloklash", callback_data=f"adm_block_{u['telegram_id']}"))
            kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="adm_back"))
            bot.send_message(message.chat.id, text, reply_markup=kb)
        bot.delete_state(message.from_user.id, message.chat.id)
    except Exception as e:
        logger.error(f"user_search: {e}")
        bot.send_message(message.chat.id, "Xatolik.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_user_"))
@admin_only
def cb_adm_user_detail(call: types.CallbackQuery):
    try:
        uid = int(call.data.replace("adm_user_", ""))
        u = get_user(uid)
        if not u:
            answer_callback(call, "Topilmadi", show_alert=True)
            return
        conf = db_execute(
            "SELECT COUNT(*) as c FROM referrals WHERE inviter_id=? AND status='confirmed'",
            (uid,), fetchone=True
        )["c"]
        text = (
            f"👤 <b>{u['first_name'] or u['username'] or 'User'}</b>\n"
            f"🆔 <code>{uid}</code>\n"
            f"💰 Balans: <b>{fmt_money(u['balance'])} so‘m</b>\n"
            f"👥 Referral: {conf} ta"
        )
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("💰 +", callback_data=f"adm_bal_add_{uid}"),
            types.InlineKeyboardButton("💰 −", callback_data=f"adm_bal_sub_{uid}"),
        )
        kb.add(types.InlineKeyboardButton("📩 Xabar", callback_data=f"adm_msg_{uid}"))
        kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="adm_ready_users"))
        safe_edit(call, text, kb)
        answer_callback(call)
    except Exception as e:
        logger.error(f"user_detail: {e}")

@bot.callback_query_handler(func=lambda c: c.data and (c.data.startswith("adm_bal_add_") or c.data.startswith("adm_bal_sub_")))
@admin_only
def cb_bal_change(call: types.CallbackQuery):
    try:
        is_add = call.data.startswith("adm_bal_add_")
        uid = int(call.data.split("_")[-1])
        bot.set_state(call.from_user.id, AdminStates.wait_balance_change, call.message.chat.id)
        with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
            data["bal_uid"] = uid
            data["bal_add"] = is_add
        action = "oshirish" if is_add else "kamaytirish"
        safe_edit(call, f"💰 Balansni {action} uchun summani kiriting:")
        answer_callback(call)
    except Exception as e:
        logger.error(f"bal_change: {e}")

@bot.message_handler(state=AdminStates.wait_balance_change)
@admin_only
def process_bal_change(message: types.Message):
    try:
        val = message.text.strip().replace(" ", "")
        if not val.isdigit():
            bot.send_message(message.chat.id, "❌ Faqat raqam.")
            return
        amount = float(val)
        with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            uid = data.get("bal_uid")
            is_add = data.get("bal_add", True)

        if is_add:
            db_execute("UPDATE users SET balance = balance + ? WHERE telegram_id=?", (amount, uid))
            bot.send_message(message.chat.id, f"✅ +{fmt_money(amount)} so‘m qo‘shildi.")
            safe_send(uid, f"💰 Balansingizga {fmt_money(amount)} so‘m qo‘shildi (admin).")
        else:
            db_execute("UPDATE users SET balance = MAX(0, balance - ?) WHERE telegram_id=?", (amount, uid))
            bot.send_message(message.chat.id, f"✅ −{fmt_money(amount)} so‘m ayirildi.")
        bot.delete_state(message.from_user.id, message.chat.id)
    except Exception as e:
        logger.error(f"process_bal: {e}")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_block_"))
@admin_only
def cb_block(call: types.CallbackQuery):
    uid = int(call.data.replace("adm_block_", ""))
    db_execute("UPDATE users SET is_blocked=1 WHERE telegram_id=?", (uid,))
    answer_callback(call, "Bloklandi", show_alert=True)
    safe_send(uid, "🚫 Siz botdan bloklandingiz.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_unblock_"))
@admin_only
def cb_unblock(call: types.CallbackQuery):
    uid = int(call.data.replace("adm_unblock_", ""))
    db_execute("UPDATE users SET is_blocked=0 WHERE telegram_id=?", (uid,))
    answer_callback(call, "Blokdan chiqarildi", show_alert=True)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_msg_"))
@admin_only
def cb_msg_user(call: types.CallbackQuery):
    uid = int(call.data.replace("adm_msg_", ""))
    bot.set_state(call.from_user.id, AdminStates.wait_user_message, call.message.chat.id)
    with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
        data["msg_uid"] = uid
    safe_edit(call, f"📩 <code>{uid}</code> ga yuboriladigan xabarni yozing:")
    answer_callback(call)

@bot.message_handler(state=AdminStates.wait_user_message)
@admin_only
def process_user_msg(message: types.Message):
    try:
        with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            uid = data.get("msg_uid")
        try:
            bot.copy_message(uid, message.chat.id, message.message_id)
            bot.send_message(message.chat.id, "✅ Xabar yuborildi.")
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Yuborilmadi: {e}")
        bot.delete_state(message.from_user.id, message.chat.id)
    except Exception as e:
        logger.error(f"user_msg: {e}")

# ----- Mandatory channels -----
@bot.callback_query_handler(func=lambda c: c.data == "adm_channels")
@admin_only
def cb_adm_channels(call: types.CallbackQuery):
    try:
        channels = db_execute("SELECT * FROM mandatory_channels ORDER BY id", fetchall=True) or []
        sub_on = get_setting("mandatory_sub_enabled", "0") == "1"
        text = (
            f"📢 <b>Majburiy kanallar</b>\n\n"
            f"Majburiy obuna: {'✅ YOQIQ' if sub_on else '❌ O‘CHIQ'}\n\n"
        )
        if not channels:
            text += "Hozircha kanal yo‘q.\nKanal qo‘shilsa obuna avtomatik yoqiladi."
        else:
            for ch in channels:
                status = "✅" if ch["is_active"] else "❌"
                typ = "🌐" if ch["type"] == "public" else "🔒"
                title = ch["title"] or ch["username"] or str(ch["chat_id"])
                text += f"{status} {typ} {title}\n"
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("➕ Ommaviy kanal", callback_data="adm_ch_add_public"),
            types.InlineKeyboardButton("🔒 Private kanal", callback_data="adm_ch_add_private"),
            types.InlineKeyboardButton("📋 Ro‘yxat / boshqarish", callback_data="adm_ch_list"),
            types.InlineKeyboardButton(
                f"{'🔴 Obunani O‘CHIRISH' if sub_on else '🟢 Obunani YOQISH'}",
                callback_data="adm_toggle_mandatory"
            ),
            types.InlineKeyboardButton("↩️ Orqaga", callback_data="adm_back"),
        )
        safe_edit(call, text, kb)
        answer_callback(call)
    except Exception as e:
        logger.error(f"adm_channels: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "adm_ch_add_public")
@admin_only
def cb_ch_add_public(call: types.CallbackQuery):
    bot.set_state(call.from_user.id, AdminStates.wait_channel_username, call.message.chat.id)
    safe_edit(call, "📢 Ommaviy kanal username kiriting:\nMasalan: @my_channel")
    answer_callback(call)

@bot.message_handler(
    state=AdminStates.wait_channel_username,
    content_types=["text", "photo", "video", "document"]
)
@admin_only
def process_ch_username(message: types.Message):
    try:
        ref = extract_channel_ref(message)
        if not ref or ref.lstrip("-").isdigit():
            # Public kanal uchun @username kerak
            raw = (message.text or "").strip().lstrip("@")
            m = re.search(r"@?([A-Za-z0-9_]{4,})", raw)
            if not m:
                bot.send_message(
                    message.chat.id,
                    "❌ Username yozing: <code>@my_channel</code>\n"
                    "yoki kanaldan xabarni forward qiling.\nBekor: /admin"
                )
                return
            uname = m.group(1)
        else:
            uname = ref.lstrip("@")

        title = uname
        chat_id = None
        try:
            chat = bot.get_chat(f"@{uname}")
            title = chat.title or uname
            chat_id = chat.id
        except Exception as e:
            logger.warning(f"get_chat @{uname}: {e}")
            bot.send_message(
                message.chat.id,
                f"⚠️ Kanal topilmadi. Botni <b>@{uname}</b> kanaliga admin qiling.\n"
                f"Username saqlanmoqda..."
            )

        db_execute(
            "INSERT INTO mandatory_channels (title, type, username, chat_id, is_active) VALUES (?, 'public', ?, ?, 1)",
            (title, uname, chat_id)
        )
        enable_mandatory_sub()
        warn = ""
        if not chat_id:
            warn = "\n\n⚠️ Botni kanalga <b>admin</b> qiling — aks holda obuna to‘liq tekshirilmaydi."
        admin_done(
            message,
            f"✅ <b>Kanal qo‘shildi!</b>\n\n"
            f"📢 {title}\n"
            f"🔗 @{uname}\n"
            f"{'🆔 ' + str(chat_id) if chat_id else '⚠️ chat_id yo‘q'}\n"
            f"✅ Majburiy obuna: <b>YOQILDI</b>"
            f"{warn}"
        )
    except Exception as e:
        logger.error(f"ch_username: {e}\n{traceback.format_exc()}")
        bot.send_message(message.chat.id, f"❌ Xatolik: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "adm_ch_add_private")
@admin_only
def cb_ch_add_private(call: types.CallbackQuery):
    bot.set_state(call.from_user.id, AdminStates.wait_channel_forward, call.message.chat.id)
    safe_edit(
        call,
        "🔒 Private kanal qo‘shish\n\n"
        "Kanaldan istalgan xabarni botga <b>forward</b> qiling."
    )
    answer_callback(call)

@bot.message_handler(state=AdminStates.wait_channel_forward, content_types=["text", "photo", "video", "document", "audio", "voice", "sticker"])
@admin_only
def process_ch_forward(message: types.Message):
    try:
        if not message.forward_from_chat:
            bot.send_message(message.chat.id, "❌ Forward qilingan xabar kerak. Qayta urinib ko‘ring.")
            return
        chat = message.forward_from_chat
        if chat.type not in ("channel", "supergroup"):
            bot.send_message(message.chat.id, "❌ Bu kanal emas.")
            return
        with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            data["priv_chat_id"] = chat.id
            data["priv_title"] = chat.title or str(chat.id)
        bot.set_state(message.from_user.id, AdminStates.wait_channel_invite, message.chat.id)
        bot.send_message(message.chat.id, "🔗 Endi invite link yuboring:\nMasalan: https://t.me/+xxxxx")
    except Exception as e:
        logger.error(f"ch_forward: {e}")
        bot.send_message(message.chat.id, "Xatolik.")

@bot.message_handler(state=AdminStates.wait_channel_invite, content_types=["text"])
@admin_only
def process_ch_invite(message: types.Message):
    try:
        if not message.text:
            bot.send_message(message.chat.id, "❌ Invite link yuboring.")
            return
        link = message.text.strip()
        if "t.me/" not in link and "telegram.me/" not in link:
            bot.send_message(message.chat.id, "❌ To‘g‘ri invite link yuboring.\nMasalan: https://t.me/+xxxxx")
            return
        with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            chat_id = data.get("priv_chat_id")
            title = data.get("priv_title", "Private")
        if not chat_id:
            bot.send_message(message.chat.id, "❌ Avval kanaldan xabar forward qiling. /admin")
            bot.delete_state(message.from_user.id, message.chat.id)
            return
        db_execute(
            "INSERT INTO mandatory_channels (title, type, chat_id, invite_link, is_active) VALUES (?, 'private', ?, ?, 1)",
            (title, chat_id, link)
        )
        enable_mandatory_sub()
        admin_done(
            message,
            f"✅ <b>Private kanal qo‘shildi!</b>\n\n"
            f"📢 {title}\n"
            f"🆔 <code>{chat_id}</code>\n"
            f"🔗 {link}\n"
            f"✅ Majburiy obuna: <b>YOQILDI</b>\n\n"
            f"⚠️ Botni shu kanalga <b>admin</b> qiling!"
        )
    except Exception as e:
        logger.error(f"ch_invite: {e}\n{traceback.format_exc()}")
        bot.send_message(message.chat.id, f"❌ Xatolik: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "adm_ch_list")
@admin_only
def cb_ch_list(call: types.CallbackQuery):
    try:
        channels = db_execute("SELECT * FROM mandatory_channels ORDER BY id", fetchall=True) or []
        if not channels:
            safe_edit(call, "Kanal yo‘q.", back_kb("adm_channels"))
            answer_callback(call)
            return
        text = "📋 <b>Kanallar ro‘yxati</b>\n\n"
        kb = types.InlineKeyboardMarkup(row_width=2)
        for ch in channels:
            status = "✅" if ch["is_active"] else "❌"
            typ = "🌐" if ch["type"] == "public" else "🔒"
            title = (ch["title"] or ch["username"] or str(ch["chat_id"]))[:25]
            text += f"{status} {typ} {title} (ID:{ch['id']})\n"
            kb.add(
                types.InlineKeyboardButton(
                    f"{'❌' if ch['is_active'] else '✅'} {ch['id']}",
                    callback_data=f"adm_ch_toggle_{ch['id']}"
                ),
                types.InlineKeyboardButton(
                    f"🗑 {ch['id']}",
                    callback_data=f"adm_ch_del_{ch['id']}"
                ),
            )
        kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="adm_channels"))
        safe_edit(call, text, kb)
        answer_callback(call)
    except Exception as e:
        logger.error(f"ch_list: {e}")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_ch_toggle_"))
@admin_only
def cb_ch_toggle(call: types.CallbackQuery):
    cid = int(call.data.replace("adm_ch_toggle_", ""))
    row = db_execute("SELECT is_active FROM mandatory_channels WHERE id=?", (cid,), fetchone=True)
    if row:
        new_val = 0 if row["is_active"] else 1
        db_execute("UPDATE mandatory_channels SET is_active=? WHERE id=?", (new_val, cid))
        answer_callback(call, "O‘zgartirildi")
        cb_ch_list(call)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_ch_del_"))
@admin_only
def cb_ch_del(call: types.CallbackQuery):
    cid = int(call.data.replace("adm_ch_del_", ""))
    db_execute("DELETE FROM mandatory_channels WHERE id=?", (cid,))
    answer_callback(call, "O‘chirildi")
    cb_ch_list(call)

# ----- Broadcast -----
@bot.callback_query_handler(func=lambda c: c.data == "adm_broadcast")
@admin_only
def cb_adm_broadcast(call: types.CallbackQuery):
    bot.set_state(call.from_user.id, AdminStates.wait_broadcast, call.message.chat.id)
    safe_edit(
        call,
        "📨 <b>Xabar yuborish</b>\n\n"
        "Oddiy matn, rasm, video, document, audio, voice yoki sticker yuboring.\n"
        "Keyin tasdiqlash so‘raladi."
    )
    answer_callback(call)

@bot.message_handler(state=AdminStates.wait_broadcast, content_types=["text", "photo", "video", "document", "audio", "voice", "sticker"])
@admin_only
def process_broadcast(message: types.Message):
    try:
        with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            data["bc_msg_id"] = message.message_id
            data["bc_chat_id"] = message.chat.id
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Yuborish", callback_data="adm_bc_confirm"),
            types.InlineKeyboardButton("❌ Bekor", callback_data="adm_back"),
        )
        bot.send_message(
            message.chat.id,
            "📨 Ushbu xabarni barcha foydalanuvchilarga yuboraymi?",
            reply_markup=kb
        )
    except Exception as e:
        logger.error(f"broadcast: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "adm_bc_confirm")
@admin_only
def cb_bc_confirm(call: types.CallbackQuery):
    try:
        with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
            msg_id = data.get("bc_msg_id")
            chat_id = data.get("bc_chat_id")
        if not msg_id:
            answer_callback(call, "Xabar topilmadi", show_alert=True)
            return

        users = db_execute(
            "SELECT telegram_id FROM users WHERE is_active=1 AND is_blocked=0",
            fetchall=True
        ) or []
        answer_callback(call, "Yuborish boshlandi...")
        safe_edit(call, f"📨 Yuborilmoqda... 0/{len(users)}")

        sent = 0
        fail = 0
        for i, u in enumerate(users):
            try:
                bot.copy_message(u["telegram_id"], chat_id, msg_id)
                sent += 1
            except Exception as e:
                fail += 1
                err = str(e).lower()
                if "blocked" in err or "deactivated" in err or "not found" in err:
                    db_execute("UPDATE users SET is_active=0 WHERE telegram_id=?", (u["telegram_id"],))
            if (i + 1) % 20 == 0:
                time.sleep(1)
                try:
                    bot.edit_message_text(
                        f"📨 Yuborilmoqda... {i+1}/{len(users)}",
                        call.message.chat.id,
                        call.message.message_id
                    )
                except:
                    pass

        db_execute(
            "INSERT INTO broadcasts (admin_id, sent_count, fail_count) VALUES (?, ?, ?)",
            (call.from_user.id, sent, fail)
        )
        bot.delete_state(call.from_user.id, call.message.chat.id)
        safe_edit(
            call,
            f"📨 <b>Yuborish yakunlandi.</b>\n\n"
            f"✅ Yetkazildi: {sent}\n"
            f"❌ Xato: {fail}"
        )
    except Exception as e:
        logger.error(f"bc_confirm: {e}\n{traceback.format_exc()}")
        answer_callback(call, "Xatolik")

# ----- Admins management -----
@bot.callback_query_handler(func=lambda c: c.data == "adm_admins")
@admin_only
def cb_adm_admins(call: types.CallbackQuery):
    if not is_super_admin(call.from_user.id):
        answer_callback(call, "Faqat super admin!", show_alert=True)
        return
    try:
        admins = db_execute("SELECT * FROM admins ORDER BY id", fetchall=True) or []
        text = "👨‍💼 <b>Adminlar</b>\n\n"
        for a in admins:
            role = "👑 Super" if a["role"] == "super" else "👤 Admin"
            text += f"{role} — <code>{a['telegram_id']}</code>\n"
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("➕ Admin qo‘shish", callback_data="adm_add_admin"),
            types.InlineKeyboardButton("🗑 Admin o‘chirish", callback_data="adm_del_admin"),
            types.InlineKeyboardButton("↩️ Orqaga", callback_data="adm_back"),
        )
        safe_edit(call, text, kb)
        answer_callback(call)
    except Exception as e:
        logger.error(f"adm_admins: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "adm_add_admin")
@admin_only
def cb_add_admin(call: types.CallbackQuery):
    if not is_super_admin(call.from_user.id):
        answer_callback(call, "Faqat super admin!", show_alert=True)
        return
    bot.set_state(call.from_user.id, AdminStates.wait_add_admin, call.message.chat.id)
    safe_edit(call, "👨‍💼 Yangi admin Telegram ID sini yuboring:")
    answer_callback(call)

@bot.message_handler(state=AdminStates.wait_add_admin, content_types=["text"])
@admin_only
def process_add_admin(message: types.Message):
    if not is_super_admin(message.from_user.id):
        return
    try:
        tid = int((message.text or "").strip())
        db_execute(
            "INSERT OR IGNORE INTO admins (telegram_id, role) VALUES (?, 'admin')",
            (tid,)
        )
        admin_done(message, f"✅ <b>Admin qo‘shildi!</b>\n\n🆔 <code>{tid}</code>")
    except Exception:
        bot.send_message(message.chat.id, "❌ Noto‘g‘ri ID. Faqat raqam yozing.")

@bot.callback_query_handler(func=lambda c: c.data == "adm_del_admin")
@admin_only
def cb_del_admin(call: types.CallbackQuery):
    if not is_super_admin(call.from_user.id):
        answer_callback(call, "Faqat super admin!", show_alert=True)
        return
    admins = db_execute("SELECT * FROM admins WHERE role!='super'", fetchall=True) or []
    if not admins:
        answer_callback(call, "O‘chiriladigan admin yo‘q", show_alert=True)
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    for a in admins:
        kb.add(types.InlineKeyboardButton(
            f"🗑 {a['telegram_id']}", callback_data=f"adm_del_adm_{a['telegram_id']}"
        ))
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="adm_admins"))
    safe_edit(call, "O‘chirish uchun adminni tanlang:", kb)
    answer_callback(call)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_del_adm_"))
@admin_only
def cb_do_del_admin(call: types.CallbackQuery):
    if not is_super_admin(call.from_user.id):
        return
    tid = int(call.data.replace("adm_del_adm_", ""))
    db_execute("DELETE FROM admins WHERE telegram_id=? AND role!='super'", (tid,))
    answer_callback(call, "O‘chirildi")
    cb_adm_admins(call)

# ----- Settings -----
@bot.callback_query_handler(func=lambda c: c.data == "adm_settings")
@admin_only
def cb_adm_settings(call: types.CallbackQuery):
    if not is_super_admin(call.from_user.id):
        answer_callback(call, "Faqat super admin!", show_alert=True)
        return
    try:
        text = (
            f"⚙️ <b>Bot sozlamalari</b>\n\n"
            f"👨‍💼 Admin username: @{get_setting('admin_username')}\n"
            f"🤖 Bot username: @{get_setting('bot_username')}\n"
            f"📢 To‘lov isbotlari: {get_setting('payment_proof_channel') or '—'}\n"
            f"💳 Withdraw usullari: {get_setting('withdraw_methods')}\n"
            f"📢 Majburiy obuna: {'✅' if get_setting('mandatory_sub_enabled')=='1' else '❌'}\n"
            f"💵 Referral: {fmt_money(get_setting('referral_sum'))} so‘m (darhol)\n"
            f"💸 Min yechish: {fmt_money(get_setting('min_withdraw'))} so‘m"
        )
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("👨‍💼 Admin username", callback_data="adm_set_admin_uname"),
            types.InlineKeyboardButton("🤖 Bot username", callback_data="adm_set_bot_uname"),
            types.InlineKeyboardButton("📢 To‘lov isbotlari kanali", callback_data="adm_set_proof_ch"),
            types.InlineKeyboardButton("💳 Withdraw usullari", callback_data="adm_set_wd_methods"),
            types.InlineKeyboardButton(
                "📢 Majburiy obuna ON/OFF",
                callback_data="adm_toggle_mandatory"
            ),
            types.InlineKeyboardButton("↩️ Orqaga", callback_data="adm_back"),
        )
        safe_edit(call, text, kb)
        answer_callback(call)
    except Exception as e:
        logger.error(f"adm_settings: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "adm_set_admin_uname")
@admin_only
def cb_set_admin_uname(call: types.CallbackQuery):
    bot.set_state(call.from_user.id, AdminStates.wait_admin_username, call.message.chat.id)
    safe_edit(call, "👨‍💼 Yangi admin username kiriting (@sizsiz):")
    answer_callback(call)

@bot.message_handler(state=AdminStates.wait_admin_username, content_types=["text"])
@admin_only
def process_admin_uname(message: types.Message):
    try:
        val = (message.text or "").strip().lstrip("@")
        if not val:
            bot.send_message(message.chat.id, "❌ Username yozing.")
            return
        set_setting("admin_username", val)
        admin_done(message, f"✅ <b>Saqlandi!</b>\n\n👨‍💼 Admin username: @{val}")
    except Exception as e:
        logger.error(f"admin_uname: {e}")
        bot.send_message(message.chat.id, f"❌ Xatolik: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "adm_set_bot_uname")
@admin_only
def cb_set_bot_uname(call: types.CallbackQuery):
    bot.set_state(call.from_user.id, AdminStates.wait_bot_username, call.message.chat.id)
    safe_edit(call, "🤖 Bot username kiriting (@sizsiz):\nBekor: /admin")
    answer_callback(call)

@bot.message_handler(state=AdminStates.wait_bot_username, content_types=["text"])
@admin_only
def process_bot_uname(message: types.Message):
    try:
        val = (message.text or "").strip().lstrip("@")
        if not val:
            bot.send_message(message.chat.id, "❌ Username yozing.")
            return
        set_setting("bot_username", val)
        admin_done(message, f"✅ <b>Saqlandi!</b>\n\n🤖 Bot username: @{val}")
    except Exception as e:
        logger.error(f"bot_uname: {e}")
        bot.send_message(message.chat.id, f"❌ Xatolik: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "adm_set_proof_ch")
@admin_only
def cb_set_proof_ch(call: types.CallbackQuery):
    bot.set_state(call.from_user.id, AdminStates.wait_payment_channel, call.message.chat.id)
    safe_edit(
        call,
        "📢 <b>To‘lov isbotlari kanalini kiriting</b>\n\n"
        "Quyidagilardan birini yuboring:\n"
        "• <code>@kanal_username</code>\n"
        "• <code>-1001234567890</code> (kanal ID)\n"
        "• Kanaldan istalgan xabarni <b>forward</b> qiling\n\n"
        "Bekor: /admin"
    )
    answer_callback(call)


def extract_channel_ref(message: types.Message) -> Optional[str]:
    """Matn, link yoki forward dan kanal @username yoki ID ni ajratib olish."""
    # Forward qilingan kanal
    if message.forward_from_chat:
        ch = message.forward_from_chat
        if ch.username:
            return f"@{ch.username}"
        return str(ch.id)

    text = (message.text or message.caption or "").strip()
    if not text:
        return None

    # To'g'ridan-to'g'ri ID
    if text.lstrip("-").isdigit():
        return text

    # @username
    m = re.search(r"@([A-Za-z0-9_]{4,})", text)
    if m:
        return f"@{m.group(1)}"

    # t.me/username yoki t.me/c/...
    m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)", text)
    if m:
        part = m.group(1)
        if part != "c" and not part.startswith("+"):
            return f"@{part}"

    # Entities ichidagi URL
    entities = message.entities or message.caption_entities or []
    for ent in entities:
        if ent.type in ("url", "text_link"):
            url = ent.url if ent.type == "text_link" else text[ent.offset:ent.offset + ent.length]
            m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)", url or "")
            if m and m.group(1) not in ("c", "share", "joinchat"):
                return f"@{m.group(1)}"

    # Butun matn username ko'rinishida
    clean = text.lstrip("@").strip()
    if re.fullmatch(r"[A-Za-z0-9_]{4,}", clean):
        return f"@{clean}"

    return None


@bot.message_handler(
    state=AdminStates.wait_payment_channel,
    content_types=["text", "photo", "video", "document", "audio", "voice", "sticker"]
)
@admin_only
def process_proof_ch(message: types.Message):
    try:
        val = extract_channel_ref(message)
        if not val:
            bot.send_message(
                message.chat.id,
                "❌ Kanal topilmadi.\n\n"
                "Yuboring:\n"
                "• <code>@kanal_username</code>\n"
                "• yoki kanaldan xabarni <b>forward</b> qiling\n\n"
                "Bekor: /admin"
            )
            return
        set_setting("payment_proof_channel", val)
        admin_done(message, f"✅ <b>Saqlandi!</b>\n\n📢 To‘lov isbotlari kanali:\n<code>{val}</code>")
    except Exception as e:
        logger.error(f"proof_ch: {e}\n{traceback.format_exc()}")
        bot.send_message(message.chat.id, f"❌ Xatolik: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "adm_set_wd_methods")
@admin_only
def cb_set_wd_methods(call: types.CallbackQuery):
    bot.set_state(call.from_user.id, AdminStates.wait_withdraw_methods, call.message.chat.id)
    safe_edit(
        call,
        "💳 Withdraw usullarini vergul bilan yozing:\n"
        "Masalan: <code>karta,telefon,boshqa</code>\n\n"
        "Mumkin: karta, telefon, boshqa\nBekor: /admin"
    )
    answer_callback(call)

@bot.message_handler(state=AdminStates.wait_withdraw_methods, content_types=["text"])
@admin_only
def process_wd_methods(message: types.Message):
    try:
        val = (message.text or "").strip().lower()
        if not val:
            bot.send_message(message.chat.id, "❌ Usullarni yozing.")
            return
        set_setting("withdraw_methods", val)
        admin_done(message, f"✅ <b>Saqlandi!</b>\n\n💳 Withdraw usullari:\n{val}")
    except Exception as e:
        logger.error(f"wd_methods: {e}")
        bot.send_message(message.chat.id, f"❌ Xatolik: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "adm_toggle_mandatory")
@admin_only
def cb_toggle_mandatory(call: types.CallbackQuery):
    cur = get_setting("mandatory_sub_enabled", "0")
    new = "0" if cur == "1" else "1"
    set_setting("mandatory_sub_enabled", new)
    answer_callback(call, f"Majburiy obuna: {'✅ YOQILDI' if new=='1' else '❌ O‘CHIRILDI'}", show_alert=True)
    # Qaysi paneldan bosilganiga qarab qaytish
    try:
        cb_adm_channels(call)
    except Exception:
        try:
            cb_adm_settings(call)
        except Exception:
            pass

# ----- Bonuses admin -----
@bot.callback_query_handler(func=lambda c: c.data == "adm_bonuses")
@admin_only
def cb_adm_bonuses(call: types.CallbackQuery):
    if not is_super_admin(call.from_user.id):
        answer_callback(call, "Faqat super admin!", show_alert=True)
        return
    try:
        bonuses = db_execute("SELECT * FROM bonuses ORDER BY id DESC LIMIT 20", fetchall=True) or []
        text = "🎁 <b>Bonuslar</b>\n\n"
        if not bonuses:
            text += "Hozircha yo‘q."
        else:
            for b in bonuses:
                status = "✅" if b["is_active"] else "❌"
                text += f"{status} {b['name']}: {fmt_money(b['amount'])} so‘m ({b['claimed_count']}/{b['max_users'] or '∞'})\n"
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("➕ Bonus yaratish", callback_data="adm_bonus_create"),
            types.InlineKeyboardButton("📋 Boshqarish", callback_data="adm_bonus_manage"),
            types.InlineKeyboardButton("↩️ Orqaga", callback_data="adm_back"),
        )
        safe_edit(call, text, kb)
        answer_callback(call)
    except Exception as e:
        logger.error(f"adm_bonuses: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "adm_bonus_create")
@admin_only
def cb_bonus_create(call: types.CallbackQuery):
    bot.set_state(call.from_user.id, AdminStates.wait_bonus_name, call.message.chat.id)
    safe_edit(call, "🎁 Bonus nomini kiriting:")
    answer_callback(call)

@bot.message_handler(state=AdminStates.wait_bonus_name)
@admin_only
def process_bonus_name(message: types.Message):
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data["b_name"] = message.text.strip()
    bot.set_state(message.from_user.id, AdminStates.wait_bonus_sum, message.chat.id)
    bot.send_message(message.chat.id, "💰 Bonus summasini kiriting (so‘m):")

@bot.message_handler(state=AdminStates.wait_bonus_sum)
@admin_only
def process_bonus_sum(message: types.Message):
    try:
        amount = float(message.text.strip().replace(" ", ""))
        with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            data["b_amount"] = amount
        bot.set_state(message.from_user.id, AdminStates.wait_bonus_limit, message.chat.id)
        bot.send_message(message.chat.id, "👥 Nechta foydalanuvchiga beriladi? (0 = cheksiz):")
    except:
        bot.send_message(message.chat.id, "❌ Noto‘g‘ri summa.")

@bot.message_handler(state=AdminStates.wait_bonus_limit)
@admin_only
def process_bonus_limit(message: types.Message):
    try:
        limit = int(message.text.strip())
        with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            name = data.get("b_name", "Bonus")
            amount = data.get("b_amount", 0)
        db_execute(
            "INSERT INTO bonuses (name, amount, max_users, is_active) VALUES (?, ?, ?, 1)",
            (name, amount, limit)
        )
        bot.delete_state(message.from_user.id, message.chat.id)
        bot.send_message(message.chat.id, f"✅ Bonus yaratildi: {name} — {fmt_money(amount)} so‘m")
        bot.send_message(message.chat.id, "👑 ADMIN PANEL", reply_markup=admin_panel_kb(True))
    except Exception as e:
        logger.error(f"bonus_limit: {e}")
        bot.send_message(message.chat.id, "Xatolik.")

@bot.callback_query_handler(func=lambda c: c.data == "adm_bonus_manage")
@admin_only
def cb_bonus_manage(call: types.CallbackQuery):
    bonuses = db_execute("SELECT * FROM bonuses ORDER BY id DESC LIMIT 15", fetchall=True) or []
    if not bonuses:
        answer_callback(call, "Bonus yo‘q", show_alert=True)
        return
    kb = types.InlineKeyboardMarkup(row_width=2)
    for b in bonuses:
        status = "✅" if b["is_active"] else "❌"
        kb.add(
            types.InlineKeyboardButton(
                f"{status} {b['name'][:15]}",
                callback_data=f"adm_bonus_tog_{b['id']}"
            ),
            types.InlineKeyboardButton(
                f"🗑 {b['id']}",
                callback_data=f"adm_bonus_del_{b['id']}"
            ),
        )
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="adm_bonuses"))
    safe_edit(call, "🎁 Bonuslarni boshqarish:", kb)
    answer_callback(call)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_bonus_tog_"))
@admin_only
def cb_bonus_toggle(call: types.CallbackQuery):
    bid = int(call.data.replace("adm_bonus_tog_", ""))
    row = db_execute("SELECT is_active FROM bonuses WHERE id=?", (bid,), fetchone=True)
    if row:
        new = 0 if row["is_active"] else 1
        db_execute("UPDATE bonuses SET is_active=? WHERE id=?", (new, bid))
        answer_callback(call, "O‘zgartirildi")
        cb_bonus_manage(call)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_bonus_del_"))
@admin_only
def cb_bonus_del(call: types.CallbackQuery):
    bid = int(call.data.replace("adm_bonus_del_", ""))
    db_execute("DELETE FROM bonuses WHERE id=?", (bid,))
    answer_callback(call, "O‘chirildi")
    cb_bonus_manage(call)

# ----- Payments (proof channel info) -----
@bot.callback_query_handler(func=lambda c: c.data == "adm_payments")
@admin_only
def cb_adm_payments(call: types.CallbackQuery):
    ch = get_setting("payment_proof_channel", "")
    text = f"💵 <b>To‘lov isbotlari</b>\n\nKanal: {ch or 'Sozlanmagan'}"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📢 Kanalni o‘zgartirish", callback_data="adm_set_proof_ch"))
    kb.add(types.InlineKeyboardButton("↩️ Orqaga", callback_data="adm_back"))
    safe_edit(call, text, kb)
    answer_callback(call)

# ==================== FALLBACK ====================
@bot.message_handler(func=lambda m: True, content_types=["text"], state=None)
def fallback_text(message: types.Message):
    if message.text and message.text.startswith("/"):
        return
    bot.send_message(
        message.chat.id,
        "🏠 Asosiy menyudan foydalaning yoki /start bosing.",
        reply_markup=main_menu_kb(message.from_user.id)
    )

# ==================== STARTUP ====================
def startup():
    """DB, adminlar, bot username."""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("=" * 50)
        print("XATO: BOT_TOKEN yo'q!")
        print("Lokal: main.py da BOT_TOKEN yozing")
        print("Render: Environment → BOT_TOKEN")
        print("=" * 50)
        raise SystemExit(1)

    init_db()
    sync_admins_from_config()
    logger.info(f"Adminlar: {ADMIN_IDS}")

    try:
        chs = get_active_channels()
        logger.info(f"Faol majburiy kanallar: {len(chs)}")
        # Kanal bor, lekin majburiy o'chiq bo'lsa — eslatma
        if chs and get_setting("mandatory_sub_enabled", "0") != "1":
            logger.warning(
                "Kanal(lar) bor, lekin majburiy obuna O'CHIQ. "
                "Admin panel → Majburiy kanallar → Obunani YOQISH"
            )
        logger.info(f"Majburiy obuna: {get_setting('mandatory_sub_enabled', '0')}")
    except Exception as e:
        logger.warning(f"channel list: {e}")

    try:
        me = bot.get_me()
        if me.username:
            set_setting("bot_username", me.username)
            logger.info(f"Bot: @{me.username} (id={me.id})")
        else:
            logger.warning("Bot username topilmadi")
    except Exception as e:
        logger.error(f"TOKEN NOTO'G'RI yoki internet yo'q: {e}")
        raise SystemExit(1)

    t = threading.Thread(target=confirm_pending_referrals, daemon=True)
    t.start()


def run_polling():
    """Termux / lokal — long polling."""
    logger.info("MODE: POLLING — Telegramda /start bosing.")
    # Webhook o'chiq bo'lishi kerak
    try:
        bot.remove_webhook()
        time.sleep(0.5)
    except Exception as e:
        logger.warning(f"remove_webhook: {e}")

    while True:
        try:
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=60,
                skip_pending=True,
                none_stop=True,
                interval=0,
            )
        except KeyboardInterrupt:
            logger.info("Bot to'xtatildi.")
            break
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)


def run_webhook():
    """Render.com — Flask + webhook."""
    if not FLASK_OK:
        logger.error("Flask o'rnatilmagan: pip install flask")
        raise SystemExit(1)

    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL bo'sh! Render Environment da to'ldiring.")
        raise SystemExit(1)

    app = Flask(__name__)
    full_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"

    @app.route("/", methods=["GET"])
    def index():
        return "Bot ishlayapti ✅", 200

    @app.route("/health", methods=["GET"])
    def health():
        return {"status": "ok"}, 200

    @app.route(WEBHOOK_PATH, methods=["POST"])
    def webhook():
        if request.headers.get("content-type") == "application/json":
            try:
                json_str = request.get_data().decode("utf-8")
                update = types.Update.de_json(json_str)
                bot.process_new_updates([update])
            except Exception as e:
                logger.error(f"Webhook process error: {e}\n{traceback.format_exc()}")
            return "", 200
        return "Bad request", 403

    # Eski webhook/polling tozalash
    try:
        bot.remove_webhook()
        time.sleep(0.5)
    except Exception as e:
        logger.warning(f"remove_webhook: {e}")

    # Yangi webhook o'rnatish
    try:
        bot.set_webhook(
            url=full_url,
            max_connections=40,
            drop_pending_updates=True,
        )
        logger.info(f"Webhook o'rnatildi: {full_url}")
    except Exception as e:
        logger.error(f"set_webhook xato: {e}")
        raise SystemExit(1)

    logger.info(f"MODE: WEBHOOK — port {PORT}")
    # Render PORT ni o'zi beradi
    app.run(host="0.0.0.0", port=PORT, threaded=True)


# ==================== MAIN ====================
if __name__ == "__main__":
    startup()
    if WEBHOOK_URL:
        run_webhook()
    else:
        run_polling()
