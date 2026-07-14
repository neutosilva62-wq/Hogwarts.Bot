import os
import asyncio
import sqlite3
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import (
    Message,
    ChatPermissions,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    BotCommand,
    ReplyParameters
)

TOKEN = os.getenv("BOT_TOKEN")
OWNER_IDS = [1130170420, 8672397104]

# Grupo único da Biblioteca de Hogwarts
# Link interno: https://t.me/c/3553956365/...
GRUPO_UNICO_ID = -1003553956365
GRUPO_UNICO_NOME = "Biblioteca de Hogwarts 🏰📖"

bot = Bot(token=TOKEN)
dp = Dispatcher()

db = sqlite3.connect("grouphelp.db", check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=NORMAL")
db.execute("PRAGMA temp_store=MEMORY")
db.execute("PRAGMA cache_size=-64000")
cur = db.cursor()

# =========================
# TABELAS
# =========================

cur.execute("""
CREATE TABLE IF NOT EXISTS warns (
    chat_id INTEGER,
    user_id INTEGER,
    warns INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS settings (
    chat_id INTEGER PRIMARY KEY,
    rules TEXT DEFAULT 'Sem regras definidas.',
    welcome TEXT DEFAULT 'Bem-vindo(a) ao grupo!'
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS rules_config (
    chat_id INTEGER PRIMARY KEY,
    rules_text TEXT DEFAULT '',
    rules_media_type TEXT DEFAULT '',
    rules_media_file_id TEXT DEFAULT '',
    rules_media_caption TEXT DEFAULT ''
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS welcome_config (
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER DEFAULT 0,
    welcome_text TEXT DEFAULT '',
    welcome_media_type TEXT DEFAULT '',
    welcome_media_file_id TEXT DEFAULT '',
    welcome_media_caption TEXT DEFAULT '',
    mode TEXT DEFAULT 'first',
    delete_last INTEGER DEFAULT 0,
    topic_id INTEGER
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS topic_config (
    chat_id INTEGER PRIMARY KEY,
    welcome_topic_id INTEGER
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS permissions_config (
    chat_id INTEGER PRIMARY KEY,
    staff_perm TEXT DEFAULT 'admins',
    rules_perm TEXT DEFAULT 'admins',
    me_perm TEXT DEFAULT 'admins',
    translate_perm TEXT DEFAULT 'admins',
    link_perm TEXT DEFAULT 'admins'
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS night_config (
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER DEFAULT 0,
    action TEXT DEFAULT 'disabled',
    start_hour INTEGER DEFAULT 22,
    end_hour INTEGER DEFAULT 7,
    warning_enabled INTEGER DEFAULT 0,
    timezone_name TEXT DEFAULT 'America/Fortaleza',
    timezone_offset INTEGER DEFAULT -3
)
""")

db.commit()


def add_column_if_missing(table, column, definition):
    cur.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cur.fetchall()]
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        db.commit()


add_column_if_missing("night_config", "text_start", "TEXT DEFAULT ''")
add_column_if_missing("night_config", "text_end", "TEXT DEFAULT ''")
add_column_if_missing("night_config", "media_start_type", "TEXT DEFAULT ''")
add_column_if_missing("night_config", "media_start_file_id", "TEXT DEFAULT ''")
add_column_if_missing("night_config", "media_end_type", "TEXT DEFAULT ''")
add_column_if_missing("night_config", "media_end_file_id", "TEXT DEFAULT ''")
add_column_if_missing("night_config", "last_message_id", "INTEGER DEFAULT 0")
add_column_if_missing("night_config", "last_state", "TEXT DEFAULT ''")

# Tópico que o Modo Noturno deve abrir/fechar.
# Câmara de Invocação: https://t.me/c/3553956365/510
TOPICO_MODO_NOTURNO_ID = 510

# =========================
# ESTADOS
# =========================

aguardando_rules_text = set()
aguardando_rules_media = set()

aguardando_welcome_text = set()
aguardando_welcome_media = set()

aguardando_timezone_location = set()
night_select_start = set()
night_select_end = {}

night_waiting_text_start = set()
night_waiting_text_end = set()
night_waiting_media_start = set()
night_waiting_media_end = set()

grupos = {GRUPO_UNICO_ID: GRUPO_UNICO_NOME}
grupo_selecionado = {}


# =========================
# FUNÇÕES BÁSICAS
# =========================

def is_owner(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in OWNER_IDS)


def get_selected_group(user_id: int):
    # Bot secretaria travado em um único grupo.
    return GRUPO_UNICO_ID


def is_grupo_unico(chat_id: int) -> bool:
    return chat_id == GRUPO_UNICO_ID


async def safe_answer(callback: CallbackQuery, text: str = None, show_alert: bool = False):
    try:
        await callback.answer(text, show_alert=show_alert)
    except Exception:
        pass


async def safe_edit(message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        try:
            await message.answer(text, reply_markup=reply_markup)
        except Exception:
            pass
    except Exception:
        try:
            await message.answer(text, reply_markup=reply_markup)
        except Exception:
            pass


async def is_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except Exception as e:
        print("ERRO EM is_admin:", repr(e))
        return False


def is_anonymous_admin(message: Message) -> bool:
    try:
        return bool(
            message.sender_chat
            and message.chat
            and message.sender_chat.id == message.chat.id
        )
    except Exception:
        return False


def ensure_chat_settings(chat_id: int):
    cur.execute("SELECT chat_id FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO settings (chat_id, rules, welcome) VALUES (?, ?, ?)",
            (chat_id, "Sem regras definidas.", "Bem-vindo(a) ao grupo!")
        )
        db.commit()


def add_warn(chat_id: int, user_id: int) -> int:
    cur.execute("SELECT warns FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = cur.fetchone()

    if row:
        total = row[0] + 1
        cur.execute(
            "UPDATE warns SET warns=? WHERE chat_id=? AND user_id=?",
            (total, chat_id, user_id)
        )
    else:
        total = 1
        cur.execute(
            "INSERT INTO warns (chat_id, user_id, warns) VALUES (?, ?, ?)",
            (chat_id, user_id, total)
        )

    db.commit()
    return total


def get_warns(chat_id: int, user_id: int) -> int:
    cur.execute("SELECT warns FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = cur.fetchone()
    return row[0] if row else 0


# =========================
# FUNÇÕES DAS REGRAS
# =========================

def ensure_rules_config(chat_id: int):
    cur.execute("SELECT chat_id FROM rules_config WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("""
            INSERT INTO rules_config (
                chat_id, rules_text, rules_media_type, rules_media_file_id, rules_media_caption
            ) VALUES (?, '', '', '', '')
        """, (chat_id,))
        db.commit()


def get_rules_data(chat_id: int):
    ensure_rules_config(chat_id)
    cur.execute("""
        SELECT rules_text, rules_media_type, rules_media_file_id, rules_media_caption
        FROM rules_config
        WHERE chat_id=?
    """, (chat_id,))
    row = cur.fetchone()

    return {
        "text": row[0] or "",
        "media_type": row[1] or "",
        "media_file_id": row[2] or "",
        "media_caption": row[3] or "",
    }


def save_rules_text(chat_id: int, text: str):
    ensure_rules_config(chat_id)
    cur.execute("UPDATE rules_config SET rules_text=? WHERE chat_id=?", (text, chat_id))
    db.commit()


def save_rules_media(chat_id: int, media_type: str, file_id: str, caption: str = ""):
    ensure_rules_config(chat_id)
    cur.execute("""
        UPDATE rules_config
        SET rules_media_type=?, rules_media_file_id=?, rules_media_caption=?
        WHERE chat_id=?
    """, (media_type, file_id, caption, chat_id))
    db.commit()


def remove_rules_text(chat_id: int):
    ensure_rules_config(chat_id)
    cur.execute("UPDATE rules_config SET rules_text='' WHERE chat_id=?", (chat_id,))
    db.commit()


def remove_rules_media(chat_id: int):
    ensure_rules_config(chat_id)
    cur.execute("""
        UPDATE rules_config
        SET rules_media_type='', rules_media_file_id='', rules_media_caption=''
        WHERE chat_id=?
    """, (chat_id,))
    db.commit()


def rules_status_text(chat_id: int) -> str:
    data = get_rules_data(chat_id)
    text_ok = "✅" if data["text"] else "❌"
    media_ok = "✅" if data["media_file_id"] else "❌"

    return (
        "📜 Regras\n\n"
        f"📄 Texto {text_ok}\n"
        f"🖼️ Mídias {media_ok}\n\n"
        "👉 Use os botões abaixo para escolher o que você deseja definir."
    )


def build_rules_buttons_keyboard(show_back: bool = False):
    if not show_back:
        return None

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Voltar", callback_data="rules_open_editor")]
        ]
    )


# =========================
# FUNÇÕES DAS BOAS-VINDAS
# =========================

def ensure_welcome_config(chat_id: int):
    cur.execute("SELECT chat_id FROM welcome_config WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("""
            INSERT INTO welcome_config (
                chat_id, enabled, welcome_text,
                welcome_media_type, welcome_media_file_id,
                welcome_media_caption, mode, delete_last, topic_id
            )
            VALUES (?, 0, '', '', '', '', 'first', 0, NULL)
        """, (chat_id,))
        db.commit()


def get_welcome_data(chat_id: int):
    ensure_welcome_config(chat_id)
    cur.execute("""
        SELECT enabled, welcome_text, welcome_media_type,
               welcome_media_file_id, welcome_media_caption,
               mode, delete_last, topic_id
        FROM welcome_config
        WHERE chat_id=?
    """, (chat_id,))
    row = cur.fetchone()

    return {
        "enabled": row[0],
        "text": row[1] or "",
        "media_type": row[2] or "",
        "media_file_id": row[3] or "",
        "media_caption": row[4] or "",
        "mode": row[5] or "first",
        "delete_last": row[6],
        "topic_id": row[7],
    }


def save_welcome_text(chat_id: int, text: str):
    ensure_welcome_config(chat_id)
    cur.execute("UPDATE welcome_config SET welcome_text=? WHERE chat_id=?", (text, chat_id))
    db.commit()


def save_welcome_media(chat_id: int, media_type: str, file_id: str, caption: str = ""):
    ensure_welcome_config(chat_id)
    cur.execute("""
        UPDATE welcome_config
        SET welcome_media_type=?, welcome_media_file_id=?, welcome_media_caption=?
        WHERE chat_id=?
    """, (media_type, file_id, caption, chat_id))
    db.commit()


def remove_welcome_text(chat_id: int):
    ensure_welcome_config(chat_id)
    cur.execute("UPDATE welcome_config SET welcome_text='' WHERE chat_id=?", (chat_id,))
    db.commit()


def remove_welcome_media(chat_id: int):
    ensure_welcome_config(chat_id)
    cur.execute("""
        UPDATE welcome_config
        SET welcome_media_type='', welcome_media_file_id='', welcome_media_caption=''
        WHERE chat_id=?
    """, (chat_id,))
    db.commit()


def welcome_status_text(chat_id: int):
    data = get_welcome_data(chat_id)
    text_ok = "✅" if data["text"] else "❌"
    media_ok = "✅" if data["media_file_id"] else "❌"

    return (
        "💬 Mensagem de boas-vindas\n\n"
        f"📄 Texto {text_ok}\n"
        f"🖼️ Mídias {media_ok}\n\n"
        "👉 Use os botões abaixo para escolher o que deseja definir."
    )


def welcome_editor_text():
    return (
        "🦉 Jessyca 🦉, agora envie a\n"
        "mensagem que você quer definir!\n\n"
        "Você pode usar HTML e:\n"
        "• {ID} = ID do usuário\n"
        "• {NAME} = nome do usuário\n"
        "• {SURNAME} = sobrenome do usuário\n"
        "• {NAMESURNAME} = nome e sobrenome do usuário\n"
        "• {LANG} = idioma do usuário\n"
        "• {DATE} = data de entrada\n"
        "• {TIME} = horário de entrada\n"
        "• {WEEKDAY} = dia da semana\n"
        "• {MENTION} = menção ao usuário\n"
        "• {USERNAME} = nome de usuário\n"
        "• {GROUPNAME} = nome do grupo\n"
        "• {RULES} = regras do grupo"
    )


def render_welcome_text(template: str, user, chat_id: int, group_name: str = ""):
    now = datetime.now()
    rules_data = get_rules_data(chat_id)
    rules_text = rules_data["text"] or "Sem regras definidas."

    first_name = getattr(user, "first_name", "") or ""
    last_name = getattr(user, "last_name", "") or ""
    full_name = (first_name + " " + last_name).strip()
    username = getattr(user, "username", None)
    language = getattr(user, "language_code", "") or ""

    mention = f'<a href="tg://user?id={user.id}">{full_name or first_name or "usuário"}</a>'
    username_text = f"@{username}" if username else "Sem username"

    text = template or ""
    text = text.replace("{ID}", str(user.id))
    text = text.replace("{NAME}", first_name)
    text = text.replace("{SURNAME}", last_name)
    text = text.replace("{NAMESURNAME}", full_name)
    text = text.replace("{LANG}", language)
    text = text.replace("{DATE}", now.strftime("%d/%m/%Y"))
    text = text.replace("{TIME}", now.strftime("%H:%M"))
    text = text.replace("{WEEKDAY}", now.strftime("%A"))
    text = text.replace("{MENTION}", mention)
    text = text.replace("{USERNAME}", username_text)
    text = text.replace("{GROUPNAME}", group_name or str(chat_id))
    text = text.replace("{RULES}", rules_text)
    return text


def fake_user_for_preview(owner_id: int):
    class FakeUser:
        id = owner_id
        first_name = "Jessyca"
        last_name = ""
        username = "jessyca"
        language_code = "pt-br"

    return FakeUser()


# =========================
# FUNÇÕES DOS TÓPICOS
# =========================

def ensure_topic_config(chat_id: int):
    cur.execute("SELECT chat_id FROM topic_config WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO topic_config (chat_id, welcome_topic_id) VALUES (?, ?)",
            (chat_id, None)
        )
        db.commit()


def set_welcome_topic(chat_id: int, topic_id: int):
    ensure_topic_config(chat_id)
    cur.execute(
        "UPDATE topic_config SET welcome_topic_id=? WHERE chat_id=?",
        (topic_id, chat_id)
    )
    db.commit()


def get_welcome_topic(chat_id: int):
    ensure_topic_config(chat_id)
    cur.execute("SELECT welcome_topic_id FROM topic_config WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    return row[0] if row and row[0] else None


# =========================
# FUNÇÕES DAS PERMISSÕES
# =========================

def ensure_permissions_config(chat_id: int):
    cur.execute("""
        INSERT OR IGNORE INTO permissions_config (
            chat_id, staff_perm, rules_perm, me_perm, translate_perm, link_perm
        ) VALUES (?, 'admins', 'admins', 'admins', 'admins', 'admins')
    """, (chat_id,))
    db.commit()


def get_permissions_data(chat_id: int):
    ensure_permissions_config(chat_id)

    cur.execute("""
        SELECT staff_perm, rules_perm, me_perm, translate_perm, link_perm
        FROM permissions_config
        WHERE chat_id=?
    """, (chat_id,))
    row = cur.fetchone()

    if not row:
        return {
            "staff": "admins",
            "rules": "admins",
            "me": "admins",
            "translate": "admins",
            "link": "admins",
        }

    return {
        "staff": row[0] or "admins",
        "rules": row[1] or "admins",
        "me": row[2] or "admins",
        "translate": row[3] or "admins",
        "link": row[4] or "admins",
    }


def set_command_permission(chat_id: int, command_name: str, value: str):
    ensure_permissions_config(chat_id)

    allowed_columns = {
        "staff": "staff_perm",
        "rules": "rules_perm",
        "me": "me_perm",
        "translate": "translate_perm",
        "link": "link_perm",
    }

    if command_name not in allowed_columns:
        return False

    if value not in {"none", "admins", "everyone", "private"}:
        return False

    column = allowed_columns[command_name]
    cur.execute(
        f"UPDATE permissions_config SET {column}=? WHERE chat_id=?",
        (value, chat_id)
    )
    db.commit()
    return True


async def can_use_command(chat_id: int, user_id: int, command_name: str, message: Message) -> bool:
    data = get_permissions_data(chat_id)
    value = data.get(command_name, "admins")

    if user_id not in OWNER_IDS:
        return True

    if value == "none":
        return False

    if value == "everyone":
        return True

    if value == "private":
        return message.chat.type == "private"

    if value == "admins":
        if is_anonymous_admin(message):
            return True

        if message.chat.type == "private":
            return False

        return await is_admin(chat_id, user_id)

    return False


def build_permissions_text(chat_id: int):
    data = get_permissions_data(chat_id)

    def label(v):
        if v == "none":
            return "❌ Ninguém"
        if v == "everyone":
            return "👥 Todos"
        if v == "private":
            return "🤖 Privado"
        if v == "admins":
            return "👮 Admins"
        return "❌ Ninguém"

    return (
        "📍 Permissões de Comandos\n"
        "Neste menu você pode configurar\n"
        "as permissões de uso dos\n"
        "seguintes comandos.\n\n"
        "❌ = ninguém   |   👥 = todos\n"
        "🤖 = todos, em chat privado\n"
        "👮 = admins e moderadores\n\n"
        f"• /staff » {label(data['staff'])}\n"
        f"• /rules » {label(data['rules'])}\n"
        f"• /me » {label(data['me'])}\n"
        f"• /translate » {label(data['translate'])}\n"
        f"• /link » {label(data['link'])}"
    )


def build_permissions_keyboard(chat_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="/staff", callback_data="perm_row_staff"),
                InlineKeyboardButton(text="❌", callback_data="perm_set_staff_none"),
                InlineKeyboardButton(text="👮", callback_data="perm_set_staff_admins"),
                InlineKeyboardButton(text="👥", callback_data="perm_set_staff_everyone"),
                InlineKeyboardButton(text="🤖", callback_data="perm_set_staff_private"),
            ],
            [
                InlineKeyboardButton(text="/rules", callback_data="perm_row_rules"),
                InlineKeyboardButton(text="❌", callback_data="perm_set_rules_none"),
                InlineKeyboardButton(text="👮", callback_data="perm_set_rules_admins"),
                InlineKeyboardButton(text="👥", callback_data="perm_set_rules_everyone"),
                InlineKeyboardButton(text="🤖", callback_data="perm_set_rules_private"),
            ],
            [
                InlineKeyboardButton(text="/me", callback_data="perm_row_me"),
                InlineKeyboardButton(text="❌", callback_data="perm_set_me_none"),
                InlineKeyboardButton(text="👮", callback_data="perm_set_me_admins"),
                InlineKeyboardButton(text="👥", callback_data="perm_set_me_everyone"),
                InlineKeyboardButton(text="🤖", callback_data="perm_set_me_private"),
            ],
            [
                InlineKeyboardButton(text="/translate", callback_data="perm_row_translate"),
                InlineKeyboardButton(text="❌", callback_data="perm_set_translate_none"),
                InlineKeyboardButton(text="👮", callback_data="perm_set_translate_admins"),
                InlineKeyboardButton(text="👥", callback_data="perm_set_translate_everyone"),
                InlineKeyboardButton(text="🤖", callback_data="perm_set_translate_private"),
            ],
            [
                InlineKeyboardButton(text="/link", callback_data="perm_row_link"),
                InlineKeyboardButton(text="❌", callback_data="perm_set_link_none"),
                InlineKeyboardButton(text="👮", callback_data="perm_set_link_admins"),
                InlineKeyboardButton(text="👥", callback_data="perm_set_link_everyone"),
                InlineKeyboardButton(text="🤖", callback_data="perm_set_link_private"),
            ],
            [InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_rules")]
        ]
    )


# =========================
# FUNÇÕES DO MODO NOTURNO
# =========================

def ensure_night_config(chat_id: int):
    cur.execute("SELECT chat_id FROM night_config WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("""
            INSERT INTO night_config (
                chat_id, enabled, action, start_hour, end_hour,
                warning_enabled, timezone_name, timezone_offset
            ) VALUES (?, 0, 'disabled', 22, 7, 0, 'America/Fortaleza', -3)
        """, (chat_id,))
        db.commit()


def get_night_data(chat_id: int):
    ensure_night_config(chat_id)

    cur.execute("""
        SELECT enabled, action, start_hour, end_hour,
               warning_enabled, timezone_name, timezone_offset
        FROM night_config
        WHERE chat_id=?
    """, (chat_id,))
    row = cur.fetchone()

    return {
        "enabled": row[0],
        "action": row[1] or "disabled",
        "start_hour": row[2],
        "end_hour": row[3],
        "warning_enabled": row[4],
        "timezone_name": row[5] or "America/Fortaleza",
        "timezone_offset": row[6] if row[6] is not None else -3,
    }


def set_night_action(chat_id: int, action: str):
    ensure_night_config(chat_id)

    if action == "disabled":
        cur.execute(
            "UPDATE night_config SET enabled=0, action='disabled' WHERE chat_id=?",
            (chat_id,)
        )
    else:
        cur.execute(
            "UPDATE night_config SET enabled=1, action=? WHERE chat_id=?",
            (action, chat_id)
        )

    db.commit()


def set_night_hours(chat_id: int, start_hour: int, end_hour: int):
    ensure_night_config(chat_id)
    cur.execute(
        "UPDATE night_config SET start_hour=?, end_hour=? WHERE chat_id=?",
        (start_hour, end_hour, chat_id)
    )
    db.commit()


def set_night_warning(chat_id: int):
    data = get_night_data(chat_id)
    novo = 0 if data["warning_enabled"] else 1

    cur.execute(
        "UPDATE night_config SET warning_enabled=? WHERE chat_id=?",
        (novo, chat_id)
    )
    db.commit()


def set_night_timezone(chat_id: int, timezone_name: str, timezone_offset: int):
    ensure_night_config(chat_id)
    cur.execute(
        "UPDATE night_config SET timezone_name=?, timezone_offset=? WHERE chat_id=?",
        (timezone_name, timezone_offset, chat_id)
    )
    db.commit()


def get_current_time_by_offset(offset: int):
    return datetime.utcnow() + timedelta(hours=offset)


def night_action_label(action: str):
    if action == "media":
        return "🗑️ Deletar mídias"
    if action == "silence":
        return "🌕 Silêncio Global"
    return "❌ Desativado"


def night_current_time_text(chat_id: int):
    data = get_night_data(chat_id)
    now = get_current_time_by_offset(data["timezone_offset"])
    return now.strftime("%d de abr. de %Y,\n%H:%M")


def night_status_text(chat_id: int):
    data = get_night_data(chat_id)
    atual = get_current_time_by_offset(data["timezone_offset"])

    situacao = night_action_label(data["action"])
    ativo = f"Ativo das {data['start_hour']}h às {data['end_hour']}h" if data["enabled"] else "Desativado"
    aviso = "✓" if data["warning_enabled"] else "×"

    return (
        "🌙 Modo Noturno\n"
        "Selecione as limitações que você\n"
        "pretende impor durante a noite.\n\n"
        f"Situação: {situacao}\n"
        f"└ {ativo}\n"
        f"└ Mensagens de aviso: {aviso}\n\n"
        f"Hora atual: {atual.strftime('%d de abr. de %Y,')}\n"
        f"{atual.strftime('%H:%M')}"
    )


def night_timezone_text(chat_id: int):
    data = get_night_data(chat_id)
    now = get_current_time_by_offset(data["timezone_offset"])

    return (
        "🌎 Fuso horário\n"
        "Neste menu, você pode definir o\n"
        "fuso horário do grupo.\n\n"
        "O bot precisa dessa informação\n"
        "para enviar corretamente as\n"
        "mensagens com datas.\n\n"
        f"Atual: {data['timezone_name']} "
        f"({now.strftime('%d de abr. de %Y, %H:%M')})"
    )


# =========================
# MENUS INLINE
# =========================

menu_inline_principal = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="➕ Adicione-me a um grupo", callback_data="add_group")],
        [InlineKeyboardButton(text="⚙️ Gerenciar configurações", callback_data="settings")],
        [
            InlineKeyboardButton(text="👥 Grupo", callback_data="group"),
            InlineKeyboardButton(text="📢 Canal", callback_data="channel"),
        ],
        [
            InlineKeyboardButton(text="🆘 Suporte", callback_data="support"),
            InlineKeyboardButton(text="💬 Informações", callback_data="info"),
        ],
        [InlineKeyboardButton(text="🌐 Languages", callback_data="languages")],
    ]
)

menu_inline_voltar = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Voltar", callback_data="back_main")]
    ]
)

menu_configuracoes = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="📜 Regras", callback_data="cfg_rules"),
            InlineKeyboardButton(text="💬 Boas-vindas", callback_data="cfg_welcome"),
        ],
        [
            InlineKeyboardButton(text="❗ Advertências", callback_data="cfg_warns"),
            InlineKeyboardButton(text="🌙 Noturno", callback_data="cfg_night"),
        ],
        [
            InlineKeyboardButton(text="🔐 Bloquear", callback_data="cfg_block"),
            InlineKeyboardButton(text="🗑️ Apagar mensagens", callback_data="cfg_delete"),
        ],
        [
            InlineKeyboardButton(text="🧾 Permissões", callback_data="cfg_permissions"),
            InlineKeyboardButton(text="📡 Canal de registro", callback_data="cfg_logs"),
        ],
        [
            InlineKeyboardButton(text="🪪 Modo de aprovação", callback_data="cfg_approval"),
        ],
        [
            InlineKeyboardButton(text="📁 Tópico", callback_data="cfg_topic"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Voltar", callback_data="back_main"),
        ],
        [
            InlineKeyboardButton(text="✅ Concluir", callback_data="finish_settings"),
        ],
    ]
)


# =========================
# MENUS REGRAS / BOAS-VINDAS
# =========================

def menu_rules_main(chat_id: int):
    data = get_rules_data(chat_id)
    texto_ok = "✅" if data["text"] else "❌"
    media_ok = "✅" if data["media_file_id"] else "❌"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"📄 Texto {texto_ok}", callback_data="rules_set_text"),
                InlineKeyboardButton(text="👀 Veja", callback_data="rules_view_text"),
            ],
            [
                InlineKeyboardButton(text=f"🖼️ Mídias {media_ok}", callback_data="rules_set_media"),
                InlineKeyboardButton(text="👀 Veja", callback_data="rules_view_media"),
            ],
            [InlineKeyboardButton(text="👀 Visualização completa", callback_data="rules_view_full")],
            [InlineKeyboardButton(text="📍 Permissões de Comandos", callback_data="cfg_permissions")],
            [InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_rules")],
        ]
    )


def menu_rules_prompt_text():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Remover mensagem", callback_data="rules_remove_text")],
            [InlineKeyboardButton(text="❌ Cancelar", callback_data="rules_cancel")],
        ]
    )


def menu_rules_prompt_media():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Remover mensagem", callback_data="rules_remove_media")],
            [InlineKeyboardButton(text="❌ Cancelar", callback_data="rules_cancel")],
        ]
    )


def menu_welcome_main(chat_id: int):
    data = get_welcome_data(chat_id) or {}

    texto_ok = "✅" if data.get("text") else "❌"
    media_ok = "✅" if data.get("media_file_id") else "❌"

    keyboard = [
        [
            InlineKeyboardButton(
                text=f"📄 Texto {texto_ok}",
                callback_data="welcome_set_text"
            ),
            InlineKeyboardButton(
                text="👀 Veja",
                callback_data="welcome_view_text"
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"🖼️ Mídias {media_ok}",
                callback_data="welcome_set_media"
            ),
            InlineKeyboardButton(
                text="👀 Veja",
                callback_data="welcome_view_media"
            ),
        ],
        [
            InlineKeyboardButton(
                text="👀 Visualização completa",
                callback_data="welcome_view_full"
            )
        ],
        [
            InlineKeyboardButton(
                text="📂 Selecionar tópico",
                callback_data="cfg_topic"
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Voltar",
                callback_data="settings"
            )
        ],
        [
            InlineKeyboardButton(
                text="✅ Concluir",
                callback_data="finish_settings"
            )
        ],
    ]

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def menu_welcome_prompt_text():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Remover mensagem", callback_data="welcome_remove_text")],
            [InlineKeyboardButton(text="❌ Cancelar", callback_data="welcome_cancel")],
        ]
    )


def menu_welcome_prompt_media():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Remover mensagem", callback_data="welcome_remove_media")],
            [InlineKeyboardButton(text="❌ Cancelar", callback_data="welcome_cancel")],
        ]
    )


# =========================
# MENUS MODO NOTURNO
# =========================

def menu_night_main(chat_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Ativar", callback_data="night_enable"),
                InlineKeyboardButton(text="❌ Desativar", callback_data="night_disable"),
            ],
            [
                InlineKeyboardButton(text="✍️ Texto Início", callback_data="night_text_start"),
                InlineKeyboardButton(text="✍️ Texto Fim", callback_data="night_text_end"),
            ],
            [
                InlineKeyboardButton(text="🖼️ Mídia Início", callback_data="night_media_start"),
                InlineKeyboardButton(text="🖼️ Mídia Fim", callback_data="night_media_end"),
            ],
            [
                InlineKeyboardButton(text="🕘 Horário", callback_data="night_set_time"),
                InlineKeyboardButton(text="🌎 Fuso", callback_data="night_timezone"),
            ],
            [InlineKeyboardButton(text="⬅️ Voltar", callback_data="settings")],
        ]
    )


def menu_night_hours(prefix: str):
    rows = []
    horas = list(range(24))

    for i in range(0, 24, 5):
        row = []
        for h in horas[i:i + 5]:
            row.append(InlineKeyboardButton(text=str(h), callback_data=f"{prefix}:{h}"))
        rows.append(row)

    rows.append([InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def menu_timezone():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")]
        ]
    )


menu_principal = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Adicione-me a um grupo")],
        [KeyboardButton(text="⚙️ Gerenciar configurações")],
        [KeyboardButton(text="👥 Grupo"), KeyboardButton(text="📢 Canal")],
        [KeyboardButton(text="🆘 Suporte"), KeyboardButton(text="💬 Informações")],
        [KeyboardButton(text="🌐 Languages")]
    ],
    resize_keyboard=True
)


# =========================
# COMANDOS
# =========================

def menu_hogwarts_principal():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 Abrir menu Arquivos Hogwarts", callback_data="abrir_arquivos_hogwarts")],
            [InlineKeyboardButton(text="🪄 Abrir menu Secretaria Hogwarts", callback_data="abrir_secretaria_hogwarts")],
        ]
    )


@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_owner(message):
        return

    await message.answer(
        "🏰 Bem-vinda à Biblioteca de Hogwarts.\n\n"
        "Escolha qual ala deseja abrir:",
        reply_markup=menu_hogwarts_principal()
    )


@dp.message(Command("menu"))
async def cmd_menu_hogwarts(message: Message):
    if not is_owner(message):
        return

    await message.answer(
        "🏰 Menu principal de Hogwarts:",
        reply_markup=menu_hogwarts_principal()
    )


@dp.message(Command("meuid"))
async def cmd_meuid(message: Message):
    if not is_owner(message):
        return

    if message.from_user:
        await message.answer(f"O teu ID é: {message.from_user.id}")


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    if not is_owner(message):
        return

    if message.chat.type != "private":
        await message.answer("Use este comando no privado.")
        return

    grupo_selecionado[message.from_user.id] = GRUPO_UNICO_ID
    ensure_chat_settings(GRUPO_UNICO_ID)

    await message.answer(
        f"CONFIGURAÇÕES\n"
        f"Grupo: {GRUPO_UNICO_NOME}\n\n"
        f"Selecione a configuração que deseja alterar.",
        reply_markup=menu_configuracoes
    )


@dp.message(Command("regras"))
@dp.message(Command("rules"))
async def cmd_regras(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    if not await can_use_command(chat_id, user_id, "rules", message):
        await message.answer("❌ Você não tem permissão para usar este comando.")
        return

    data = get_rules_data(chat_id)
    texto = data["text"] or "Sem regras definidas."
    kb = None

    if message.chat.type == "private":
        kb = build_rules_buttons_keyboard(show_back=True)

    if data["media_file_id"]:
        caption = data["media_caption"] or texto

        if data["media_type"] == "photo":
            await message.answer_photo(data["media_file_id"], caption=caption, reply_markup=kb)
        elif data["media_type"] == "video":
            await message.answer_video(data["media_file_id"], caption=caption, reply_markup=kb)
        elif data["media_type"] == "document":
            await message.answer_document(data["media_file_id"], caption=caption, reply_markup=kb)
        elif data["media_type"] == "sticker":
            await message.answer_sticker(data["media_file_id"])
            await message.answer(texto, reply_markup=kb)
        else:
            await message.answer(texto, reply_markup=kb)
    else:
        await message.answer(texto, reply_markup=kb)


@dp.message(Command("staff"))
async def cmd_staff(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    if not await can_use_command(chat_id, user_id, "staff", message):
        await message.answer("❌ Você não tem permissão para usar este comando.")
        return

    await message.answer("👮 Lista de staff do grupo em construção.")


@dp.message(Command("me"))
async def cmd_me(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    if not await can_use_command(chat_id, user_id, "me", message):
        await message.answer("❌ Você não tem permissão para usar este comando.")
        return

    if is_anonymous_admin(message):
        await message.answer("👤 Você está usando o modo administrador anônimo.")
        return

    await message.answer(
        f"👤 Seu perfil\n\n"
        f"• Nome: {message.from_user.full_name if message.from_user else 'Sem nome'}\n"
        f"• ID: {user_id}"
    )


@dp.message(Command("translate"))
async def cmd_translate(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    if not await can_use_command(chat_id, user_id, "translate", message):
        await message.answer("❌ Você não tem permissão para usar este comando.")
        return

    await message.answer("🌐 Sistema de tradução ainda está em construção.")


@dp.message(Command("link"))
async def cmd_link(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    if not await can_use_command(chat_id, user_id, "link", message):
        await message.answer("❌ Você não tem permissão para usar este comando.")
        return

    if message.chat.type == "private":
        await message.answer("❌ Este comando só funciona em grupos.")
        return

    try:
        invite_link = await bot.export_chat_invite_link(chat_id)
        await message.answer(f"🔗 Link do grupo:\n{invite_link}")
    except Exception:
        await message.answer("❌ Não consegui gerar o link. Verifica se o bot é administrador.")


@dp.message(Command("topic_welcome"))
async def cmd_topic_welcome(message: Message):
    if not message.from_user and not is_anonymous_admin(message):
        return

    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("❌ Este comando só funciona em grupos.")
        return

    if not is_anonymous_admin(message):
        if not await is_admin(message.chat.id, message.from_user.id):
            await message.answer("❌ Só admins podem usar este comando.")
            return

    if not getattr(message, "message_thread_id", None):
        await message.answer("❌ Envie este comando dentro de um tópico.")
        return

    set_welcome_topic(message.chat.id, message.message_thread_id)
    await message.answer("✅ Tópico de boas-vindas definido com sucesso.")


# =========================
# EVENTO BOT ADICIONADO
# =========================

@dp.my_chat_member()
async def bot_added(event):
    if event.chat.type in ["group", "supergroup"]:
        status = event.new_chat_member.status
        if event.chat.id == GRUPO_UNICO_ID and status in ["member", "administrator"]:
            grupos[GRUPO_UNICO_ID] = event.chat.title or GRUPO_UNICO_NOME


# =========================
# CALLBACKS PRINCIPAIS
# =========================

@dp.callback_query(F.data.startswith("select_group:"))
async def select_group(callback: CallbackQuery):
    if not callback.from_user or callback.from_user.id not in OWNER_IDS:
        await safe_answer(callback, "Sem permissão.", show_alert=True)
        return

    grupo_selecionado[callback.from_user.id] = GRUPO_UNICO_ID
    ensure_chat_settings(GRUPO_UNICO_ID)

    await safe_edit(
        callback.message,
        f"CONFIGURAÇÕES\n"
        f"Grupo: {GRUPO_UNICO_NOME}\n\n"
        f"Selecione a configuração que deseja alterar.",
        reply_markup=menu_configuracoes
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "settings")
async def cb_settings(callback: CallbackQuery):
    if not callback.from_user or callback.from_user.id not in OWNER_IDS:
        await safe_answer(callback, "Sem permissão.", show_alert=True)
        return

    grupo_selecionado[callback.from_user.id] = GRUPO_UNICO_ID
    ensure_chat_settings(GRUPO_UNICO_ID)

    await safe_edit(
        callback.message,
        f"CONFIGURAÇÕES\n"
        f"Grupo: {GRUPO_UNICO_NOME}\n\n"
        f"Selecione a configuração que deseja alterar.",
        reply_markup=menu_configuracoes
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "add_group")
async def cb_add_group(callback: CallbackQuery):
    await safe_edit(
        callback.message,
        "➕ Para me adicionares a um grupo:\n\n"
        "1. Abre o teu grupo\n"
        "2. Vai em adicionar membros\n"
        "3. Procura o username do bot\n"
        "4. Adiciona-me ao grupo\n"
        "5. Coloca-me como administrador\n\n"
        "Depois volta aqui e usa /settings.",
        reply_markup=menu_inline_voltar
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery):
    await safe_edit(
        callback.message,
        "Olá!\n\n"
        "Sou o teu bot de gestão de grupos.\n"
        "Adiciona-me num supergrupo e coloca-me como admin para eu entrar em ação.\n\n"
        "Escolhe uma opção abaixo:",
        reply_markup=menu_inline_principal
    )
    await safe_answer(callback)


@dp.callback_query(F.data.in_({"group", "channel", "support", "info", "languages"}))
async def cb_simple_buttons(callback: CallbackQuery):
    textos = {
        "group": "Funções de grupo:\n\n• Regras\n• Permissões\n• Boas-vindas\n• Staff\n• Link",
        "channel": "Área de canal.\n\nAqui depois podemos configurar funções de canal.",
        "support": "Suporte do bot.\n\nSó o dono tem acesso às configurações.",
        "info": "Informações do bot:\n\n• Regras\n• Boas-vindas\n• Permissões\n• Tópicos",
        "languages": "Idiomas disponíveis:\n\n• Português\n• Português (Brasil)\n• English",
    }

    await safe_edit(
        callback.message,
        textos.get(callback.data, "Opção disponível."),
        reply_markup=menu_inline_voltar
    )
    await safe_answer(callback)


# =========================
# CALLBACKS CONFIGURAÇÕES
# =========================

@dp.callback_query(F.data == "cfg_rules")
async def cb_cfg_rules(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    if not chat_id:
        await safe_answer(callback, "Escolhe primeiro um grupo no /settings.", show_alert=True)
        return

    texto_menu = (
        "📜 Regras do grupo\n"
        "Nesse menu, você pode gerenciar\n"
        "as regras do grupo que estarão\n"
        "disponíveis quando os usuários\n"
        "enviarem /rules.\n\n"
        "Para editar quem pode usar o\n"
        "comando /rules, vá para o menu\n"
        "\"Permissões de comandos\"."
    )

    menu_regras_inicio = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✍🏻 Personalizar mensagem", callback_data="rules_open_editor")],
            [InlineKeyboardButton(text="📍 Permissões de Comandos", callback_data="cfg_permissions")],
            [InlineKeyboardButton(text="⬅️ Voltar", callback_data="settings")]
        ]
    )

    await safe_edit(callback.message, texto_menu, reply_markup=menu_regras_inicio)
    await safe_answer(callback)


@dp.callback_query(F.data == "cfg_welcome")
async def cb_cfg_welcome(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    if not chat_id:
        await safe_answer(callback, "Escolhe primeiro um grupo no /settings.", show_alert=True)
        return

    # Abre direto o painel simples de Boas-vindas:
    # Texto, Mídias, Visualização completa, Selecionar tópico, Voltar e Concluir.
    await safe_edit(
        callback.message,
        welcome_status_text(chat_id),
        reply_markup=menu_welcome_main(chat_id)
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "cfg_permissions")
async def cb_cfg_permissions(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    if not chat_id:
        await safe_answer(callback, "Escolhe primeiro um grupo no /settings.", show_alert=True)
        return

    await safe_edit(
        callback.message,
        build_permissions_text(chat_id),
        reply_markup=build_permissions_keyboard(chat_id)
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "cfg_topic")
async def cb_cfg_topic(callback: CallbackQuery):
    await safe_edit(
        callback.message,
        "🗂️ Selecione um Tópico\n"
        "Se você usa Tópicos em seu grupo,\n"
        "você precisa decidir em qual tópico\n"
        "o bot deve enviar mensagens deste tipo.\n\n"
        "Para isso, vá até o Tópico\n"
        "escolhido e envie este comando:\n"
        "/topic_welcome\n\n"
        "Se você não usar \"Tópicos\",\n"
        "ignore essa configuração.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_welcome")]
            ]
        )
    )
    await safe_answer(callback)


# =========================
# MODO NOTURNO NOVO
# =========================

night_waiting_text_start = set()
night_waiting_text_end = set()
night_waiting_media_start = set()
night_waiting_media_end = set()


def add_column_if_missing(table, column, definition):
    cur.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cur.fetchall()]
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        db.commit()


add_column_if_missing("night_config", "text_start", "TEXT DEFAULT ''")
add_column_if_missing("night_config", "text_end", "TEXT DEFAULT ''")
add_column_if_missing("night_config", "media_start_type", "TEXT DEFAULT ''")
add_column_if_missing("night_config", "media_start_file_id", "TEXT DEFAULT ''")
add_column_if_missing("night_config", "media_end_type", "TEXT DEFAULT ''")
add_column_if_missing("night_config", "media_end_file_id", "TEXT DEFAULT ''")
add_column_if_missing("night_config", "last_message_id", "INTEGER DEFAULT 0")
add_column_if_missing("night_config", "last_state", "TEXT DEFAULT ''")

# Tópico que o Modo Noturno deve abrir/fechar.
# Câmara de Invocação: https://t.me/c/3553956365/510
TOPICO_MODO_NOTURNO_ID = 510


def get_night_data(chat_id: int):
    ensure_night_config(chat_id)

    cur.execute("""
        SELECT enabled, start_hour, end_hour, timezone_name, timezone_offset,
               text_start, text_end, media_start_type, media_start_file_id,
               media_end_type, media_end_file_id
        FROM night_config
        WHERE chat_id=?
    """, (chat_id,))
    row = cur.fetchone()

    return {
        "enabled": row[0],
        "start_hour": row[1] or 22,
        "end_hour": row[2] or 8,
        "timezone_name": row[3] or "America/Fortaleza",
        "timezone_offset": row[4] if row[4] is not None else -3,
        "text_start": row[5] or "",
        "text_end": row[6] or "",
        "media_start_type": row[7] or "",
        "media_start_file_id": row[8] or "",
        "media_end_type": row[9] or "",
        "media_end_file_id": row[10] or "",
    }


def night_is_open(chat_id: int):
    data = get_night_data(chat_id)
    now = datetime.utcnow() + timedelta(hours=data["timezone_offset"])
    h = now.hour

    start = data["start_hour"]
    end = data["end_hour"]

    if start < end:
        return not (start <= h < end)

    return not (h >= start or h < end)


def night_status_text(chat_id: int):
    data = get_night_data(chat_id)
    now = datetime.utcnow() + timedelta(hours=data["timezone_offset"])

    status = "Ativado ✅" if data["enabled"] else "Desativado ❌"
    situacao = "Aberto ☀️" if night_is_open(chat_id) else "Fechado 🌙"

    text_start = "✅" if data["text_start"] else "❌"
    media_start = "✅" if data["media_start_file_id"] else "❌"
    text_end = "✅" if data["text_end"] else "❌"
    media_end = "✅" if data["media_end_file_id"] else "❌"

    return (
        "🌙 Modo Noturno\n\n"
        f"Status: {status}\n"
        f"Situação atual: {situacao}\n"
        f"Horário: {data['start_hour']}h até {data['end_hour']}h\n\n"
        f"📄 Texto de entrada {text_start}\n"
        f"🖼️ Mídia de entrada {media_start}\n"
        f"📄 Texto de encerramento {text_end}\n"
        f"🖼️ Mídia de encerramento {media_end}\n\n"
        f"Hora atual: {now.strftime('%d/%m/%Y %H:%M')}"
    )


def menu_night_main(chat_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Ativar", callback_data="night_enable"),
                InlineKeyboardButton(text="❌ Desativar", callback_data="night_disable"),
            ],
            [
                InlineKeyboardButton(text="✍️ Texto Início", callback_data="night_text_start"),
                InlineKeyboardButton(text="✍️ Texto Fim", callback_data="night_text_end"),
            ],
            [
                InlineKeyboardButton(text="🖼️ Mídia Início", callback_data="night_media_start"),
                InlineKeyboardButton(text="🖼️ Mídia Fim", callback_data="night_media_end"),
            ],
            [
                InlineKeyboardButton(text="🕘 Horário", callback_data="night_set_time"),
                InlineKeyboardButton(text="🌎 Fuso", callback_data="night_timezone"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Voltar", callback_data="settings")
            ],
        ]
    )


def menu_night_hours(prefix: str):
    rows = []
    for i in range(0, 24, 6):
        rows.append([
            InlineKeyboardButton(text=str(h), callback_data=f"{prefix}:{h}")
            for h in range(i, min(i + 6, 24))
        ])

    rows.append([InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "__desativado_cfg_night")
async def cb_cfg_night(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)

    await safe_edit(
        callback.message,
        night_status_text(chat_id),
        reply_markup=menu_night_main(chat_id)
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "__desativado_night_enable")
async def cb_night_enable(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)

    cur.execute("UPDATE night_config SET enabled=1 WHERE chat_id=?", (chat_id,))
    db.commit()

    await safe_edit(callback.message, night_status_text(chat_id), reply_markup=menu_night_main(chat_id))
    await safe_answer(callback, "Modo noturno ativado.")


@dp.callback_query(F.data == "__desativado_night_disable")
async def cb_night_disable(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)

    cur.execute("UPDATE night_config SET enabled=0 WHERE chat_id=?", (chat_id,))
    db.commit()

    await safe_edit(callback.message, night_status_text(chat_id), reply_markup=menu_night_main(chat_id))
    await safe_answer(callback, "Modo noturno desativado.")


@dp.callback_query(F.data == "__desativado_night_text_start")
async def cb_night_text_start(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    night_waiting_text_start.add(chat_id)

    await safe_edit(
        callback.message,
        "✍️ Envie agora o texto de início do modo noturno:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")]]
        )
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "__desativado_night_text_end")
async def cb_night_text_end(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    night_waiting_text_end.add(chat_id)

    await safe_edit(
        callback.message,
        "✍️ Envie agora o texto de fim do modo noturno:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")]]
        )
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "__desativado_night_media_start")
async def cb_night_media_start(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    night_waiting_media_start.add(chat_id)

    await safe_edit(
        callback.message,
        "🖼️ Envie agora a mídia de início do modo noturno:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")]]
        )
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "__desativado_night_media_end")
async def cb_night_media_end(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    night_waiting_media_end.add(chat_id)

    await safe_edit(
        callback.message,
        "🖼️ Envie agora a mídia de fim do modo noturno:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")]]
        )
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "__desativado_night_set_time")
async def cb_night_set_time(callback: CallbackQuery):
    await safe_edit(
        callback.message,
        "🌙 Modo Noturno\n\n👉 Selecione a hora de INÍCIO:",
        reply_markup=menu_night_hours("night_start")
    )
    await safe_answer(callback)


@dp.callback_query(F.data.startswith("__desativado_night_start:"))
async def cb_night_start(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    hour = int(callback.data.split(":")[1])
    night_select_end[chat_id] = hour

    await safe_edit(
        callback.message,
        f"✅ Início: {hour}h\n\n👉 Agora selecione a hora de FIM:",
        reply_markup=menu_night_hours("night_end")
    )
    await safe_answer(callback)


@dp.callback_query(F.data.startswith("__desativado_night_end:"))
async def cb_night_end(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    end_hour = int(callback.data.split(":")[1])
    start_hour = night_select_end.get(chat_id, 22)

    cur.execute(
        "UPDATE night_config SET start_hour=?, end_hour=? WHERE chat_id=?",
        (start_hour, end_hour, chat_id)
    )
    db.commit()

    night_select_end.pop(chat_id, None)

    await safe_edit(callback.message, night_status_text(chat_id), reply_markup=menu_night_main(chat_id))
    await safe_answer(callback, "Horário definido.")


@dp.callback_query(F.data == "__desativado_night_text_start")
async def cb_night_text_start(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    night_waiting_text_start.add(chat_id)

    await safe_edit(
        callback.message,
        "✍️ Envie agora o texto de início do modo noturno:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")]]
        )
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "__desativado_night_text_end")
async def cb_night_text_end(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    night_waiting_text_end.add(chat_id)

    await safe_edit(
        callback.message,
        "✍️ Envie agora o texto de fim do modo noturno:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")]]
        )
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "__desativado_night_media_start")
async def cb_night_media_start(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    night_waiting_media_start.add(chat_id)

    await safe_edit(
        callback.message,
        "🖼️ Envie agora a mídia de início do modo noturno:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")]]
        )
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "__desativado_night_media_end")
async def cb_night_media_end(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    night_waiting_media_end.add(chat_id)

    await safe_edit(
        callback.message,
        "🖼️ Envie agora a mídia de fim do modo noturno:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")]]
        )
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "__desativado_night_text_start")
async def cb_night_text_start(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    night_waiting_text_start.add(chat_id)
    await safe_edit(callback.message, "✍️ Envie agora o texto de início do modo noturno:")
    await safe_answer(callback)


@dp.callback_query(F.data == "__desativado_night_text_end")
async def cb_night_text_end(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    night_waiting_text_end.add(chat_id)
    await safe_edit(callback.message, "✍️ Envie agora o texto de fim do modo noturno:")
    await safe_answer(callback)


@dp.callback_query(F.data == "__desativado_night_media_start")
async def cb_night_media_start(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    night_waiting_media_start.add(chat_id)
    await safe_edit(callback.message, "🖼️ Envie agora a mídia de início do modo noturno:")
    await safe_answer(callback)


@dp.callback_query(F.data == "__desativado_night_media_end")
async def cb_night_media_end(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    night_waiting_media_end.add(chat_id)
    await safe_edit(callback.message, "🖼️ Envie agora a mídia de fim do modo noturno:")
    await safe_answer(callback)


# =========================
# CALLBACKS PERMISSÕES
# =========================

@dp.callback_query(F.data.startswith("perm_set_"))
async def cb_perm_set(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    if not chat_id:
        await safe_answer(callback, "Escolhe primeiro um grupo no /settings.", show_alert=True)
        return

    payload = callback.data.replace("perm_set_", "")
    parts = payload.split("_")

    value = parts[-1]
    command_name = "_".join(parts[:-1])

    atual = get_permissions_data(chat_id).get(command_name)

    if atual == value:
        await safe_answer(callback, "Esse valor já está selecionado.", show_alert=True)
        return

    ok = set_command_permission(chat_id, command_name, value)
    if not ok:
        await safe_answer(callback, "Erro ao salvar permissão.", show_alert=True)
        return

    await safe_edit(
        callback.message,
        build_permissions_text(chat_id),
        reply_markup=build_permissions_keyboard(chat_id)
    )
    await safe_answer(callback, "Permissão atualizada.")


@dp.callback_query(F.data.startswith("perm_row_"))
async def cb_perm_row(callback: CallbackQuery):
    await safe_answer(callback)


# =========================
# CALLBACKS REGRAS
# =========================

@dp.callback_query(F.data == "rules_open_editor")
async def cb_rules_open_editor(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)

    await safe_edit(
        callback.message,
        rules_status_text(chat_id),
        reply_markup=menu_rules_main(chat_id)
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "rules_set_text")
async def cb_rules_set_text(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)

    aguardando_rules_text.add(chat_id)
    aguardando_rules_media.discard(chat_id)

    await safe_edit(
        callback.message,
        "👉 Envie agora a mensagem que deseja definir.\n"
        "Você pode enviá-lo já formatado ou usar HTML.",
        reply_markup=menu_rules_prompt_text()
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "rules_set_media")
async def cb_rules_set_media(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)

    aguardando_rules_media.add(chat_id)
    aguardando_rules_text.discard(chat_id)

    await safe_edit(
        callback.message,
        "👉 Envie agora a mídia (fotos, vídeos, sticker...) que você deseja definir.\n"
        "Você também pode inserir uma legenda.",
        reply_markup=menu_rules_prompt_media()
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "rules_view_text")
async def cb_rules_view_text(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    data = get_rules_data(chat_id)

    await safe_edit(
        callback.message,
        f"📄 Texto das regras:\n\n{data['text'] or 'Sem texto definido.'}",
        reply_markup=menu_rules_main(chat_id)
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "rules_view_media")
async def cb_rules_view_media(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    data = get_rules_data(chat_id)

    if not data["media_file_id"]:
        await safe_answer(callback, "Sem mídia definida.", show_alert=True)
        return

    caption = data["media_caption"] or "🖼️ Mídia atual das regras"

    if data["media_type"] == "photo":
        await callback.message.answer_photo(data["media_file_id"], caption=caption)
    elif data["media_type"] == "video":
        await callback.message.answer_video(data["media_file_id"], caption=caption)
    elif data["media_type"] == "document":
        await callback.message.answer_document(data["media_file_id"], caption=caption)
    elif data["media_type"] == "sticker":
        await callback.message.answer_sticker(data["media_file_id"])

    await safe_answer(callback, "Mídia enviada.")


@dp.callback_query(F.data == "rules_view_full")
async def cb_rules_view_full(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    data = get_rules_data(chat_id)
    texto = data["text"] or "Sem texto definido."
    kb = build_rules_buttons_keyboard(show_back=True)

    if data["media_file_id"]:
        caption = data["media_caption"] or texto

        if data["media_type"] == "photo":
            await callback.message.answer_photo(data["media_file_id"], caption=caption, reply_markup=kb)
        elif data["media_type"] == "video":
            await callback.message.answer_video(data["media_file_id"], caption=caption, reply_markup=kb)
        elif data["media_type"] == "document":
            await callback.message.answer_document(data["media_file_id"], caption=caption, reply_markup=kb)
        elif data["media_type"] == "sticker":
            await callback.message.answer_sticker(data["media_file_id"])
            await callback.message.answer(texto, reply_markup=kb)
    else:
        await callback.message.answer(texto, reply_markup=kb)

    await safe_answer(callback, "Pré-visualização enviada.")


@dp.callback_query(F.data == "rules_remove_text")
async def cb_rules_remove_text(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    remove_rules_text(chat_id)
    aguardando_rules_text.discard(chat_id)

    await safe_edit(callback.message, rules_status_text(chat_id), reply_markup=menu_rules_main(chat_id))
    await safe_answer(callback, "Texto removido.")


@dp.callback_query(F.data == "rules_remove_media")
async def cb_rules_remove_media(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    remove_rules_media(chat_id)
    aguardando_rules_media.discard(chat_id)

    await safe_edit(callback.message, rules_status_text(chat_id), reply_markup=menu_rules_main(chat_id))
    await safe_answer(callback, "Mídia removida.")


@dp.callback_query(F.data == "rules_cancel")
async def cb_rules_cancel(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)

    aguardando_rules_text.discard(chat_id)
    aguardando_rules_media.discard(chat_id)

    await safe_edit(callback.message, rules_status_text(chat_id), reply_markup=menu_rules_main(chat_id))
    await safe_answer(callback, "Cancelado.")


# =========================
# CALLBACKS BOAS-VINDAS
# =========================

@dp.callback_query(F.data == "welcome_enable")
async def cb_welcome_enable(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    ensure_welcome_config(chat_id)
    cur.execute("UPDATE welcome_config SET enabled=1 WHERE chat_id=?", (chat_id,))
    db.commit()
    await cb_cfg_welcome(callback)


@dp.callback_query(F.data == "welcome_disable")
async def cb_welcome_disable(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    ensure_welcome_config(chat_id)
    cur.execute("UPDATE welcome_config SET enabled=0 WHERE chat_id=?", (chat_id,))
    db.commit()
    await cb_cfg_welcome(callback)


@dp.callback_query(F.data == "welcome_mode_always")
async def cb_welcome_mode_always(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    ensure_welcome_config(chat_id)
    cur.execute("UPDATE welcome_config SET mode='always' WHERE chat_id=?", (chat_id,))
    db.commit()
    await cb_cfg_welcome(callback)


@dp.callback_query(F.data == "welcome_mode_first")
async def cb_welcome_mode_first(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    ensure_welcome_config(chat_id)
    cur.execute("UPDATE welcome_config SET mode='first' WHERE chat_id=?", (chat_id,))
    db.commit()
    await cb_cfg_welcome(callback)


@dp.callback_query(F.data == "welcome_toggle_delete")
async def cb_welcome_toggle_delete(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    data = get_welcome_data(chat_id)
    novo = 0 if data["delete_last"] else 1

    cur.execute("UPDATE welcome_config SET delete_last=? WHERE chat_id=?", (novo, chat_id))
    db.commit()
    await cb_cfg_welcome(callback)


@dp.callback_query(F.data == "welcome_open_editor")
async def cb_welcome_open_editor(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)

    await safe_edit(
        callback.message,
        welcome_status_text(chat_id),
        reply_markup=menu_welcome_main(chat_id)
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "welcome_set_text")
async def cb_welcome_set_text(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)

    aguardando_welcome_text.add(chat_id)
    aguardando_welcome_media.discard(chat_id)

    await safe_edit(
        callback.message,
        welcome_editor_text(),
        reply_markup=menu_welcome_prompt_text()
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "welcome_set_media")
async def cb_welcome_set_media(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)

    aguardando_welcome_media.add(chat_id)
    aguardando_welcome_text.discard(chat_id)

    await safe_edit(
        callback.message,
        "👉 Envie agora a mídia (foto, vídeo, documento ou sticker) que deseja definir.\n"
        "Você também pode inserir uma legenda.",
        reply_markup=menu_welcome_prompt_media()
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "welcome_view_text")
async def cb_welcome_view_text(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    data = get_welcome_data(chat_id)

    texto = render_welcome_text(
        data["text"] or "Sem texto definido.",
        fake_user_for_preview(OWNER_IDS[0]),
        chat_id,
        grupos.get(chat_id, "")
    )

    await safe_edit(
        callback.message,
        f"📄 Texto da mensagem de boas-vindas:\n\n{texto}",
        reply_markup=menu_welcome_main(chat_id)
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "welcome_view_media")
async def cb_welcome_view_media(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    data = get_welcome_data(chat_id)

    if not data["media_file_id"]:
        await safe_answer(callback, "Sem mídia definida.", show_alert=True)
        return

    caption = render_welcome_text(
        data["media_caption"] or data["text"] or "🖼️ Mídia atual da mensagem de boas-vindas",
        fake_user_for_preview(OWNER_IDS[0]),
        chat_id,
        grupos.get(chat_id, "")
    )

    if data["media_type"] == "photo":
        await callback.message.answer_photo(data["media_file_id"], caption=caption, parse_mode="HTML")
    elif data["media_type"] == "video":
        await callback.message.answer_video(data["media_file_id"], caption=caption, parse_mode="HTML")
    elif data["media_type"] == "document":
        await callback.message.answer_document(data["media_file_id"], caption=caption, parse_mode="HTML")
    elif data["media_type"] == "sticker":
        await callback.message.answer_sticker(data["media_file_id"])

    await safe_answer(callback, "Mídia enviada.")


@dp.callback_query(F.data == "welcome_view_full")
async def cb_welcome_view_full(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    data = get_welcome_data(chat_id)

    texto = render_welcome_text(
        data["text"] or "Sem texto definido.",
        fake_user_for_preview(OWNER_IDS[0]),
        chat_id,
        grupos.get(chat_id, "")
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Voltar", callback_data="welcome_open_editor")]
        ]
    )

    if data["media_file_id"]:
        caption = render_welcome_text(
            data["media_caption"] or data["text"] or texto,
            fake_user_for_preview(OWNER_IDS[0]),
            chat_id,
            grupos.get(chat_id, "")
        )

        if data["media_type"] == "photo":
            await callback.message.answer_photo(data["media_file_id"], caption=caption, reply_markup=kb,
                                                parse_mode="HTML")
        elif data["media_type"] == "video":
            await callback.message.answer_video(data["media_file_id"], caption=caption, reply_markup=kb,
                                                parse_mode="HTML")
        elif data["media_type"] == "document":
            await callback.message.answer_document(data["media_file_id"], caption=caption, reply_markup=kb,
                                                   parse_mode="HTML")
        elif data["media_type"] == "sticker":
            await callback.message.answer_sticker(data["media_file_id"])
            await callback.message.answer(texto, reply_markup=kb, parse_mode="HTML")
    else:
        await callback.message.answer(texto, reply_markup=kb, parse_mode="HTML")

    await safe_answer(callback, "Pré-visualização enviada.")


@dp.callback_query(F.data == "welcome_remove_text")
async def cb_welcome_remove_text(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    remove_welcome_text(chat_id)
    aguardando_welcome_text.discard(chat_id)

    await safe_edit(
        callback.message,
        welcome_status_text(chat_id),
        reply_markup=menu_welcome_main(chat_id)
    )
    await safe_answer(callback, "Texto removido.")


@dp.callback_query(F.data == "welcome_remove_media")
async def cb_welcome_remove_media(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)
    remove_welcome_media(chat_id)
    aguardando_welcome_media.discard(chat_id)

    await safe_edit(
        callback.message,
        welcome_status_text(chat_id),
        reply_markup=menu_welcome_main(chat_id)
    )
    await safe_answer(callback, "Mídia removida.")


@dp.callback_query(F.data == "welcome_cancel")
async def cb_welcome_cancel(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id)

    aguardando_welcome_text.discard(chat_id)
    aguardando_welcome_media.discard(chat_id)

    await safe_edit(
        callback.message,
        welcome_status_text(chat_id),
        reply_markup=menu_welcome_main(chat_id)
    )
    await safe_answer(callback, "Cancelado.")


@dp.callback_query(F.data == "finish_settings")
async def cb_finish_settings(callback: CallbackQuery):
    if not callback.from_user or callback.from_user.id not in OWNER_IDS:
        await safe_answer(callback, "Sem permissão.", show_alert=True)
        return

    chat_id = get_selected_group(callback.from_user.id)
    if not chat_id:
        await safe_answer(callback, "Escolha primeiro um grupo.", show_alert=True)
        return

    await safe_edit(
        callback.message,
        "✅ Configurações concluídas!\n\n"
        "As alterações foram salvas e já estão ativas no grupo.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⚙️ Voltar às configurações", callback_data="settings")]
            ]
        )
    )
    await safe_answer(callback, "Configurações aplicadas.")


# =========================
# EVENTO DE BOAS-VINDAS — NOVOS MEMBROS
# =========================

@dp.message(F.new_chat_members)
async def boas_vindas_novos_membros(message: Message):
    chat_id = message.chat.id

    data = get_welcome_data(chat_id)

    if not data["enabled"]:
        return

    if not message.new_chat_members:
        return

    group_name = message.chat.title or str(chat_id)

    for membro in message.new_chat_members:
        if membro.is_bot:
            continue

        texto = render_welcome_text(
            data["text"] or "✨ Bem-vindo(a), {MENTION}!",
            membro,
            chat_id,
            group_name
        )

        topic_id = get_welcome_topic(chat_id) or data.get("topic_id")

        try:
            if data["media_file_id"]:
                caption = render_welcome_text(
                    data["media_caption"] or data["text"] or texto,
                    membro,
                    chat_id,
                    group_name
                )

                if data["media_type"] == "photo":
                    await bot.send_photo(
                        chat_id,
                        data["media_file_id"],
                        caption=caption,
                        parse_mode="HTML",
                        message_thread_id=topic_id
                    )

                elif data["media_type"] == "video":
                    await bot.send_video(
                        chat_id,
                        data["media_file_id"],
                        caption=caption,
                        parse_mode="HTML",
                        message_thread_id=topic_id
                    )

                elif data["media_type"] == "document":
                    await bot.send_document(
                        chat_id,
                        data["media_file_id"],
                        caption=caption,
                        parse_mode="HTML",
                        message_thread_id=topic_id
                    )

                elif data["media_type"] == "sticker":
                    await bot.send_sticker(
                        chat_id,
                        data["media_file_id"],
                        message_thread_id=topic_id
                    )
                    await bot.send_message(
                        chat_id,
                        texto,
                        parse_mode="HTML",
                        message_thread_id=topic_id
                    )

                else:
                    await bot.send_message(
                        chat_id,
                        texto,
                        parse_mode="HTML",
                        message_thread_id=topic_id
                    )

            else:
                await bot.send_message(
                    chat_id,
                    texto,
                    parse_mode="HTML",
                    message_thread_id=topic_id
                )

        except Exception as e:
            print("ERRO AO ENVIAR BOAS-VINDAS:", repr(e))


# =========================
# CAPTURAR TEXTO / MÍDIA / LOCALIZAÇÃO
# =========================


# ============================================================
# ALA DOS ARQUIVOS DE HOGWARTS — PERGAMINHOS OTIMIZADO
# ============================================================

# Mantém a Secretaria intacta; esta parte cuida apenas dos Arquivos/Pergaminhos.
GRUPO_ID = GRUPO_UNICO_ID
TOPICO_PEDIDOS_ID = 510  # Câmara de Invocação
TOPICO_ARQUIVOS_ID = 193  # Arquivos de Hogwarts
ADMINS = OWNER_IDS


def limpar_estado_secretaria(chat_id: int):
    """Limpa esperas da Secretaria quando a pessoa entra no menu Arquivos."""
    night_waiting_text_start.discard(chat_id)
    night_waiting_text_end.discard(chat_id)
    night_waiting_media_start.discard(chat_id)
    night_waiting_media_end.discard(chat_id)
    aguardando_timezone_location.discard(chat_id)
    aguardando_welcome_text.discard(chat_id)
    aguardando_welcome_media.discard(chat_id)
    aguardando_rules_text.discard(chat_id)
    aguardando_rules_media.discard(chat_id)


def limpar_estado_arquivos(user_id: int):
    """Limpa esperas dos Arquivos quando a pessoa entra no menu Secretaria."""
    modo_edicao.pop(user_id, None)
    pedido_selecionado.pop(user_id, None)
    arquivos_pendentes.pop(user_id, None)


conn = sqlite3.connect("pedidos.db", check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")
conn.execute("PRAGMA temp_store=MEMORY")
conn.execute("PRAGMA cache_size=-64000")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS pedidos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    nome TEXT,
    username TEXT,
    pedido TEXT,
    status TEXT,
    grupo_msg_id INTEGER,
    arquivo_id TEXT,
    arquivo_tipo TEXT,
    figurinha_id TEXT,
    chave_livro TEXT,
    registro_msg_id INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS config (
    chave TEXT,
    valor TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS entregues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chave_livro TEXT UNIQUE,
    nome_livro TEXT,
    pedido_id INTEGER,
    arquivo_id TEXT,
    data_registro TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS arquivos_enviados (
    pedido_id INTEGER,
    message_id INTEGER,
    tipo TEXT DEFAULT 'arquivo'
)
""")

conn.commit()

# Garante compatibilidade se o banco pedidos.db já existia sem essa coluna.
try:
    cursor.execute("ALTER TABLE pedidos ADD COLUMN registro_msg_id INTEGER")
    conn.commit()
except sqlite3.OperationalError:
    pass

pedido_selecionado = {}
arquivos_pendentes = {}
modo_edicao = {}


def autorizado(user_id: int):
    return user_id in ADMINS


def pegar_config(chave):
    cursor.execute(
        "SELECT valor FROM config WHERE chave = ? ORDER BY rowid DESC LIMIT 1",
        (chave,)
    )
    resultado = cursor.fetchone()
    return resultado[0] if resultado else ""


def salvar_config(chave, valor):
    cursor.execute("SELECT rowid FROM config WHERE chave = ?", (chave,))
    existe = cursor.fetchone()

    if existe:
        cursor.execute(
            "UPDATE config SET valor = ? WHERE chave = ?",
            (valor, chave)
        )
    else:
        cursor.execute(
            "INSERT INTO config (chave, valor) VALUES (?, ?)",
            (chave, valor)
        )

    conn.commit()


configs_padrao = {
    "msg_pedido": "📜 Pergaminho registrado em Hogwarts.\n\nOs professores da biblioteca já iniciaram as buscas. 📚✨",
    "msg_arquivo": "🦉📚 Grimório localizado.\nA coruja já encaminhou o exemplar aos Arquivos de Hogwarts. ✨📜",
    "msg_nao_encontrei": "🔎 Este pergaminho ainda não foi localizado no acervo.\nEle ficará guardado nos Pergaminhos Não Localizados.",
    "msg_ja_postado": "🧠📚 Este grimório já repousa no Acervo de Hogwarts.\n\nConsulte os Arquivos de Hogwarts para localizar o pergaminho solicitado. ✨📜"
}

for chave, valor in configs_padrao.items():
    if not pegar_config(chave):
        salvar_config(chave, valor)


def remover_acentos(texto):
    texto = unicodedata.normalize("NFD", texto)
    texto = texto.encode("ascii", "ignore").decode("utf-8")
    return texto


def extrair_nome_livro(texto):
    linhas = texto.splitlines()
    campos_validos = (
        "livro:",
        "nome:",
        "nome do livro:",
        "grimório:",
        "grimorio:",
        "grimório/livros solicitado:",
        "grimorio/livros solicitado:",
        "grimório/livro solicitado:",
        "grimorio/livro solicitado:",
        "grimório solicitado:",
        "grimorio solicitado:",
    )

    for i, linha in enumerate(linhas):
        linha_limpa = linha.strip()
        linha_lower = linha_limpa.lower()

        for campo in campos_validos:
            if linha_lower.startswith(campo):
                valor = linha_limpa.split(":", 1)[1].strip() if ":" in linha_limpa else ""
                if valor:
                    return valor
                if i + 1 < len(linhas):
                    proxima = linhas[i + 1].strip()
                    if proxima:
                        return proxima

    return texto[:80].strip()


def criar_chave_livro(texto):
    nome = extrair_nome_livro(texto)
    nome = remover_acentos(nome.lower())
    nome = re.sub(r"[^a-z0-9]+", " ", nome)
    nome = re.sub(r"\s+", " ", nome).strip()
    return nome


def formatar_mensagem_config(chave, **dados):
    texto = pegar_config(chave)
    try:
        return texto.format(**dados)
    except Exception:
        return texto


def parece_ficha(texto: str):
    texto = (texto or "").lower()
    return (
            "pergaminho de solicitação" in texto
            or "pergaminho de solicitacao" in texto
            or "#pedido" in texto
            or "livro:" in texto
            or "nome do livro:" in texto
            or "grimório/livros solicitado" in texto
            or "grimorio/livros solicitado" in texto
            or "grimório:" in texto
            or "grimorio:" in texto
    )


def numero_visual(pedido_id, status):
    cursor.execute("""
    SELECT id FROM pedidos
    WHERE status = ?
    ORDER BY id ASC
    """, (status,))

    ids = [linha[0] for linha in cursor.fetchall()]

    if pedido_id in ids:
        return ids.index(pedido_id) + 1

    return pedido_id


def contadores_texto():
    cursor.execute("SELECT COUNT(*) FROM entregues")
    total_acervo = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM pedidos WHERE status = 'pendente'")
    total_missoes = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM pedidos WHERE status = 'nao_encontrado'")
    total_nao_encontradas = cursor.fetchone()[0]

    return (
        "📊 Registro da Biblioteca\n\n"
        f"📚 Pergaminhos entregues ao acervo: {total_acervo}\n"
        f"📜 Pergaminhos aguardando: {total_missoes}\n"
        f"🔎 Pergaminhos não localizados: {total_nao_encontradas}"
    )


def menu_pv():
    kb = InlineKeyboardBuilder()
    kb.button(text="📜 Pergaminhos registrados", callback_data="missoes")
    kb.button(text="🔎 Pergaminhos Não Localizados", callback_data="missoes_nao_encontradas")
    kb.button(text="📊 Registro da Biblioteca", callback_data="contadores")
    kb.button(text="✏️ Personalizar Mensagens", callback_data="personalizar")
    kb.button(text="🧠 Arquivo Inteligente", callback_data="arquivo_inteligente")
    kb.button(text="🧹 Limpar pergaminhos concluídos", callback_data="limpar")
    kb.adjust(1)
    return kb.as_markup()


def menu_personalizar():
    kb = InlineKeyboardBuilder()
    kb.button(text="📜 Mensagem do pergaminho", callback_data="editar_msg_pedido")
    kb.button(text="🎯 Mensagem do arquivo", callback_data="editar_msg_arquivo")
    kb.button(text="🔎 Mensagem: não encontrei", callback_data="editar_msg_nao_encontrei")
    kb.button(text="🖼️ Figurinha: não encontrei", callback_data="editar_sticker_nao_encontrei")
    kb.button(text="⬅️ Voltar", callback_data="voltar_menu")
    kb.adjust(1)
    return kb.as_markup()


def menu_arquivo_inteligente():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Mensagem: já está no acervo", callback_data="editar_msg_ja_postado")
    kb.button(text="📊 Ver contadores", callback_data="contadores")
    kb.button(text="⬅️ Voltar", callback_data="voltar_menu")
    kb.adjust(1)
    return kb.as_markup()


def menu_pedidos(pedidos):
    kb = InlineKeyboardBuilder()

    for indice, pedido in enumerate(pedidos, start=1):
        pedido_id, nome = pedido
        kb.button(
            text=f"📜 Pergaminho {indice} - {nome}",
            callback_data=f"selecionar_{pedido_id}"
        )

    kb.adjust(1)
    return kb.as_markup()


def menu_missao_acoes(pedido_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="🧠 Já está no acervo", callback_data=f"ja_acervo_{pedido_id}")
    kb.button(text="🔎 Não localizei o grimório", callback_data=f"nao_encontrei_{pedido_id}")
    kb.button(text="❌ Cancelar envio do pergaminho", callback_data=f"cancelar_envio_{pedido_id}")
    kb.button(text="✅ Arquivar pergaminho", callback_data=f"finalizar_{pedido_id}")
    kb.button(text="⬅️ Voltar aos pergaminhos", callback_data="missoes")
    kb.adjust(1)
    return kb.as_markup()


@dp.message(F.chat.type == "private", F.text)
async def receber_texto_personalizado(message: Message):
    if not autorizado(message.from_user.id):
        return

    chave = modo_edicao.get(message.from_user.id)

    if not chave:
        return

    if chave == "sticker_nao_encontrei":
        await message.answer("⚠️ Envie uma figurinha, não uma mensagem de texto.")
        return

    salvar_config(chave, message.text)
    modo_edicao.pop(message.from_user.id, None)

    nova = pegar_config(chave)

    await message.answer(
        "✅ Mensagem personalizada salva com sucesso!\n\n"
        "📌 Nova mensagem salva:\n\n"
        f"{nova}",
        reply_markup=menu_pv()
    )


@dp.message(F.chat.id == GRUPO_ID, F.text)
async def registrar_pedido(message: Message):
    # Só registra pedidos feitos na Câmara de Invocação.
    if getattr(message, "message_thread_id", None) != TOPICO_PEDIDOS_ID:
        return

    texto = message.text

    if not parece_ficha(texto):
        return

    user = message.from_user
    nome = user.full_name
    username = user.username or "sem username"

    chave_livro = criar_chave_livro(texto)
    nome_livro = extrair_nome_livro(texto)

    cursor.execute("""
    INSERT INTO pedidos
    (user_id, nome, username, pedido, status, grupo_msg_id, chave_livro)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        user.id,
        nome,
        username,
        texto,
        "pendente",
        message.message_id,
        chave_livro
    ))
    conn.commit()

    await message.reply(pegar_config("msg_pedido"))


@dp.callback_query(F.data == "missoes")
async def missoes(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()

    cursor.execute("""
    SELECT id, nome
    FROM pedidos
    WHERE status = 'pendente'
    ORDER BY id ASC
    """)
    pedidos = cursor.fetchall()

    if not pedidos:
        await callback.message.answer(
            "✅ Não há pergaminhos registrados no momento.",
            reply_markup=menu_pv()
        )
        return

    await callback.message.answer(
        "📜 Escolha qual pergaminho deseja abrir:",
        reply_markup=menu_pedidos(pedidos)
    )


@dp.callback_query(F.data == "missoes_nao_encontradas")
async def missoes_nao_encontradas(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()

    cursor.execute("""
    SELECT id, nome
    FROM pedidos
    WHERE status = 'nao_encontrado'
    ORDER BY id ASC
    """)
    pedidos = cursor.fetchall()

    if not pedidos:
        await callback.message.answer(
            "✅ Não há pergaminhos não localizados no momento.",
            reply_markup=menu_pv()
        )
        return

    await callback.message.answer(
        "🔎 Pergaminhos guardados como não localizados:",
        reply_markup=menu_pedidos(pedidos)
    )


@dp.callback_query(F.data.startswith("selecionar_"))
async def selecionar_pedido(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()

    pedido_id = int(callback.data.replace("selecionar_", ""))

    cursor.execute("""
    SELECT id, nome, pedido, status
    FROM pedidos
    WHERE id = ? AND status IN ('pendente', 'nao_encontrado')
    """, (pedido_id,))
    pedido = cursor.fetchone()

    if not pedido:
        await callback.message.answer("⚠️ Esse pergaminho não está mais disponível.")
        return

    id_pedido, nome, pedido_texto, status = pedido
    numero = numero_visual(id_pedido, status)

    pedido_selecionado[callback.from_user.id] = pedido_id
    arquivos_pendentes[callback.from_user.id] = []

    await callback.message.answer(
        f"🕯️✨ DIRETORA RAVENNA BLACK\n\n"
        f"🦉 Pergaminho {numero} selecionado\n\n"
        f"👤 Aluno: {nome}\n\n"
        f"{pedido_texto}\n\n"
        "📦 Envie os grimórios no PV.\n"
        "🦉 Após concluir, envie a figurinha de confirmação.\n\n"
        "✅ O pergaminho só sai da lista quando você tocar em Arquivar pergaminho.",
        reply_markup=menu_missao_acoes(pedido_id)
    )


@dp.message(F.chat.type == "private", F.document)
async def receber_arquivo(message: Message):
    if not autorizado(message.from_user.id):
        return

    admin_id = message.from_user.id
    pedido_id = pedido_selecionado.get(admin_id)

    if not pedido_id:
        await message.answer("⚠️ Primeiro escolha um pergaminho em 📜 Pergaminhos registrados.")
        return

    arquivos_pendentes.setdefault(admin_id, [])
    arquivos_pendentes[admin_id].append(message.document.file_id)

    total = len(arquivos_pendentes[admin_id])

    await message.answer(
        f"✅ Arquivo recebido.\n"
        f"📦 Total de grimórios preparados neste pergaminho: {total}\n\n"
        "Pode enviar mais PDF/EPUB.\n"
        "Quando terminar, envie a figurinha de confirmação.",
        reply_markup=menu_missao_acoes(pedido_id)
    )


@dp.message(F.chat.type == "private", F.sticker)
async def receber_figurinha(message: Message):
    if not autorizado(message.from_user.id):
        return

    admin_id = message.from_user.id
    chave_edicao = modo_edicao.get(admin_id)

    if chave_edicao == "sticker_nao_encontrei":
        salvar_config("sticker_nao_encontrei", message.sticker.file_id)
        modo_edicao.pop(admin_id, None)

        await message.answer(
            "✅ Figurinha de “não encontrei” salva com sucesso!",
            reply_markup=menu_pv()
        )
        return

    pedido_id = pedido_selecionado.get(admin_id)

    if not pedido_id:
        await message.answer("⚠️ Primeiro escolha um pergaminho em 📜 Pergaminhos registrados.")
        return

    lista_arquivos = arquivos_pendentes.get(admin_id, [])

    if not lista_arquivos:
        await message.answer("⚠️ Envie pelo menos um arquivo antes da figurinha.")
        return

    cursor.execute("""
    SELECT id, nome, pedido, grupo_msg_id, chave_livro, status
    FROM pedidos
    WHERE id = ? AND status IN ('pendente', 'nao_encontrado')
    """, (pedido_id,))
    pedido = cursor.fetchone()

    if not pedido:
        await message.answer("⚠️ Pergaminho não encontrado ou já arquivado.")
        return

    id_pedido, nome, pedido_texto, grupo_msg_id, chave_livro, status = pedido
    numero = numero_visual(id_pedido, status)

    legenda = formatar_mensagem_config(
        "msg_arquivo",
        nome=nome,
        id_pedido=id_pedido,
        numero_missao=numero,
        nome_livro=extrair_nome_livro(pedido_texto)
    )

    # Envia SOMENTE os arquivos/figurinhas no tópico Arquivos de Hogwarts.
    # A mensagem personalizada de entrega vai separada, respondendo a ficha na Câmara de Invocação.
    enviados_ids = []

    for arquivo_id in lista_arquivos:
        enviado = await bot.send_document(
            chat_id=GRUPO_ID,
            document=arquivo_id,
            message_thread_id=TOPICO_ARQUIVOS_ID
        )
        enviados_ids.append(enviado.message_id)

    sticker_msg = await bot.send_sticker(
        chat_id=GRUPO_ID,
        sticker=message.sticker.file_id,
        message_thread_id=TOPICO_ARQUIVOS_ID
    )
    enviados_ids.append(sticker_msg.message_id)

    msg_registro = await bot.send_message(
        chat_id=GRUPO_ID,
        text=legenda,
        message_thread_id=TOPICO_PEDIDOS_ID,
        reply_to_message_id=grupo_msg_id
    )

    enviados_ids.append(msg_registro.message_id)

    # salva a mensagem de registro para apagar depois, quando houver nova resposta
    cursor.execute(
        "UPDATE pedidos SET registro_msg_id = ? WHERE id = ?",
        (msg_registro.message_id, pedido_id)
    )
    conn.commit()

    cursor.executemany(
        "INSERT INTO arquivos_enviados (pedido_id, message_id, tipo) VALUES (?, ?, ?)",
        [(pedido_id, mid, "envio") for mid in enviados_ids]
    )

    cursor.execute("""
    UPDATE pedidos
    SET status = 'pendente', figurinha_id = ?
    WHERE id = ?
    """, (
        message.sticker.file_id,
        pedido_id
    ))

    conn.commit()

    arquivos_pendentes[admin_id] = []

    await message.answer(
        "✅ Grimório(s) enviados aos Arquivos de Hogwarts.\n\n"
        "📜 O pergaminho continua aberto.\n"
        "Você pode enviar mais arquivos para esse mesmo pergaminho.\n\n"
        "Quando terminar tudo, toque em ✅ Arquivar pergaminho.",
        reply_markup=menu_missao_acoes(pedido_id)
    )


@dp.callback_query(F.data.startswith("cancelar_envio_"))
async def cancelar_envio(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()

    admin_id = callback.from_user.id
    pedido_id = int(callback.data.replace("cancelar_envio_", ""))

    # Apaga somente os arquivos/avisos enviados para ESTE pergaminho.
    cursor.execute("SELECT message_id FROM arquivos_enviados WHERE pedido_id = ?", (pedido_id,))
    ids_para_apagar = [row[0] for row in cursor.fetchall()]

    for msg_id in ids_para_apagar:
        try:
            await bot.delete_message(chat_id=GRUPO_ID, message_id=msg_id)
        except Exception:
            pass

    cursor.execute("DELETE FROM arquivos_enviados WHERE pedido_id = ?", (pedido_id,))
    conn.commit()

    arquivos_pendentes[admin_id] = []
    pedido_selecionado[admin_id] = pedido_id

    await callback.message.answer(
        "❌ Envio cancelado para este pergaminho.\n\n"
        "Só os arquivos/avisos deste pedido foram removidos.\n"
        "O pergaminho continua aberto.\n\n"
        "Agora envie os arquivos corretos novamente.",
        reply_markup=menu_missao_acoes(pedido_id)
    )


@dp.callback_query(F.data.startswith("ja_acervo_"))
async def ja_esta_no_acervo(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()

    pedido_id = int(callback.data.replace("ja_acervo_", ""))

    cursor.execute("""
    SELECT id, nome, pedido, grupo_msg_id, status
    FROM pedidos
    WHERE id = ? AND status IN ('pendente', 'nao_encontrado')
    """, (pedido_id,))
    pedido = cursor.fetchone()

    if not pedido:
        await callback.message.answer("⚠️ Esse pergaminho não está mais disponível.")
        return

    id_pedido, nome, pedido_texto, grupo_msg_id, status = pedido

    mensagem = formatar_mensagem_config(
        "msg_ja_postado",
        nome=nome,
        id_pedido=id_pedido,
        numero_missao=numero_visual(id_pedido, status),
        nome_livro=extrair_nome_livro(pedido_texto)
    )

    await bot.send_message(
        chat_id=GRUPO_ID,
        text=mensagem,
        message_thread_id=TOPICO_PEDIDOS_ID,
        reply_to_message_id=grupo_msg_id
    )

    # Apaga a mensagem antiga de "pergaminho registrado"
    try:
        cursor.execute(
            "SELECT registro_msg_id FROM pedidos WHERE id = ?",
            (pedido_id,)
        )
        row = cursor.fetchone()

        if row and row[0]:
            await bot.delete_message(
                chat_id=GRUPO_ID,
                message_id=row[0]
            )
    except Exception as e:
        print("Erro ao apagar mensagem de registro:", e)

    pedido_selecionado[callback.from_user.id] = pedido_id

    await callback.message.answer(
        "🧠 Resposta do Arquivo Inteligente enviada.\n\n"
        "O pergaminho continua aberto até você tocar em ✅ Arquivar pergaminho.",
        reply_markup=menu_missao_acoes(pedido_id)
    )


@dp.callback_query(F.data.startswith("nao_encontrei_"))
async def nao_encontrei(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()

    pedido_id = int(callback.data.replace("nao_encontrei_", ""))

    cursor.execute("""
    SELECT id, nome, pedido, grupo_msg_id
    FROM pedidos
    WHERE id = ? AND status IN ('pendente', 'nao_encontrado')
    """, (pedido_id,))
    pedido = cursor.fetchone()

    if not pedido:
        await callback.message.answer("⚠️ Esse pergaminho não está mais disponível.")
        return

    id_pedido, nome, pedido_texto, grupo_msg_id = pedido

    mensagem = formatar_mensagem_config(
        "msg_nao_encontrei",
        nome=nome,
        id_pedido=id_pedido,
        numero_missao=numero_visual(id_pedido, "pendente"),
        nome_livro=extrair_nome_livro(pedido_texto)
    )

    await bot.send_message(
        chat_id=GRUPO_ID,
        text=mensagem,
        message_thread_id=TOPICO_PEDIDOS_ID,
        reply_to_message_id=grupo_msg_id
    )

    sticker_id = pegar_config("sticker_nao_encontrei")

    if sticker_id:
        await bot.send_sticker(
            chat_id=GRUPO_ID,
            sticker=sticker_id,
            message_thread_id=TOPICO_PEDIDOS_ID,
            reply_to_message_id=grupo_msg_id
        )

    cursor.execute("""
    UPDATE pedidos
    SET status = 'nao_encontrado'
    WHERE id = ?
    """, (pedido_id,))
    conn.commit()

    pedido_selecionado.pop(callback.from_user.id, None)
    arquivos_pendentes.pop(callback.from_user.id, None)

    await callback.message.answer(
        "🔍 Pergaminho enviado para Pergaminhos Não Localizados.\n"
        "Ela saiu da lista principal, mas continua guardada.",
        reply_markup=menu_pv()
    )


@dp.callback_query(F.data.startswith("voltar_pendente_"))
async def voltar_pendente(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()

    pedido_id = int(callback.data.replace("voltar_pendente_", ""))

    cursor.execute("""
    UPDATE pedidos
    SET status = 'pendente'
    WHERE id = ? AND status = 'nao_encontrado'
    """, (pedido_id,))
    conn.commit()

    await callback.message.answer(
        "📜 Pergaminho voltou para Pergaminhos Registrados.",
        reply_markup=menu_pv()
    )


@dp.callback_query(F.data.startswith("finalizar_"))
async def finalizar_missao(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()

    admin_id = callback.from_user.id
    pedido_id = int(callback.data.replace("finalizar_", ""))

    cursor.execute("""
    SELECT id, pedido
    FROM pedidos
    WHERE id = ? AND status IN ('pendente', 'nao_encontrado')
    """, (pedido_id,))
    pedido = cursor.fetchone()

    if not pedido:
        await callback.message.answer("⚠️ Esse pergaminho não está mais disponível ou já foi arquivado.")
        return

    _, pedido_texto = pedido
    nome_livro = extrair_nome_livro(pedido_texto)

    # Conta 1 pergaminho arquivado, não a quantidade de arquivos enviados.
    # A chave por pedido permite contar pedidos repetidos sem guardar ficha completa no acervo.
    cursor.execute("""
    INSERT OR IGNORE INTO entregues
    (chave_livro, nome_livro, pedido_id, arquivo_id)
    VALUES (?, ?, ?, ?)
    """, (f"pedido:{pedido_id}", nome_livro, pedido_id, ""))

    # Depois de arquivar, mantém só o contador em "entregues".
    # A ficha completa sai do banco para o bot ficar mais leve 24h.
    cursor.execute("DELETE FROM arquivos_enviados WHERE pedido_id = ?", (pedido_id,))
    cursor.execute("DELETE FROM pedidos WHERE id = ?", (pedido_id,))
    conn.commit()

    pedido_selecionado.pop(admin_id, None)
    arquivos_pendentes.pop(admin_id, None)

    await callback.message.answer(
        "✅ Pergaminho arquivado com sucesso!\n"
        "📜 Ele saiu das listas abertas e entrou no Registro da Biblioteca.",
        reply_markup=menu_pv()
    )


@dp.callback_query(F.data == "personalizar")
async def personalizar(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()

    await callback.message.answer(
        "✏️ Escolha qual mensagem deseja personalizar:",
        reply_markup=menu_personalizar()
    )


@dp.callback_query(F.data == "arquivo_inteligente")
async def arquivo_inteligente(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()

    await callback.message.answer(
        "🧠 Arquivo Inteligente\n\n"
        "Aqui você personaliza a resposta manual para pergaminhos que já existem no acervo.",
        reply_markup=menu_arquivo_inteligente()
    )


@dp.callback_query(F.data == "contadores")
async def contadores(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()

    await callback.message.answer(
        contadores_texto(),
        reply_markup=menu_pv()
    )


@dp.callback_query(F.data == "editar_msg_pedido")
async def editar_msg_pedido(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()
    modo_edicao[callback.from_user.id] = "msg_pedido"

    atual = pegar_config("msg_pedido")

    await callback.message.answer(
        "📜 Envie agora a nova mensagem automática do pergaminho.\n\n"
        f"Mensagem atual:\n\n{atual}"
    )


@dp.callback_query(F.data == "editar_msg_arquivo")
async def editar_msg_arquivo(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()
    modo_edicao[callback.from_user.id] = "msg_arquivo"

    atual = pegar_config("msg_arquivo")

    await callback.message.answer(
        "🎯 Envie agora a nova legenda dos arquivos.\n\n"
        "Você pode usar:\n"
        "{nome} = nome da pessoa\n"
        "{id_pedido} = número interno do pergaminho\n"
        "{numero_missao} = número visual do pergaminho\n"
        "{nome_livro} = nome do livro\n\n"
        f"Mensagem atual:\n\n{atual}"
    )


@dp.callback_query(F.data == "editar_msg_nao_encontrei")
async def editar_msg_nao_encontrei(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()
    modo_edicao[callback.from_user.id] = "msg_nao_encontrei"

    atual = pegar_config("msg_nao_encontrei")

    await callback.message.answer(
        "🔎 Envie agora a nova mensagem de “não encontrei o livro”.\n\n"
        "Você pode usar:\n"
        "{nome} = nome da pessoa\n"
        "{id_pedido} = número interno do pergaminho\n"
        "{numero_missao} = número visual do pergaminho\n"
        "{nome_livro} = nome do livro\n\n"
        f"Mensagem atual:\n\n{atual}"
    )


@dp.callback_query(F.data == "editar_msg_ja_postado")
async def editar_msg_ja_postado(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()
    modo_edicao[callback.from_user.id] = "msg_ja_postado"

    atual = pegar_config("msg_ja_postado")

    await callback.message.answer(
        "✅ Envie agora a nova mensagem do Arquivo Inteligente.\n\n"
        "Essa mensagem será enviada pelo botão 🧠 Já está no acervo.\n\n"
        "Você pode usar:\n"
        "{nome} = nome da pessoa\n"
        "{nome_livro} = nome do livro\n\n"
        f"Mensagem atual:\n\n{atual}"
    )


@dp.callback_query(F.data == "editar_sticker_nao_encontrei")
async def editar_sticker_nao_encontrei(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()
    modo_edicao[callback.from_user.id] = "sticker_nao_encontrei"

    await callback.message.answer(
        "🖼️ Envie agora a figurinha usada em “não encontrei o livro”."
    )


@dp.callback_query(F.data == "voltar_menu")
async def voltar_menu(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()

    await callback.message.answer(
        "📚 Menu dos Arquivos de Hogwarts:",
        reply_markup=menu_pv()
    )


@dp.callback_query(F.data == "limpar")
async def limpar(callback: CallbackQuery):
    if not autorizado(callback.from_user.id):
        await callback.answer("Sem permissão.", show_alert=True)
        return

    await callback.answer()

    cursor.execute("SELECT COUNT(*) FROM pedidos WHERE status = 'concluido'")
    total = cursor.fetchone()[0]

    if total == 0:
        await callback.message.answer("✅ Não há pergaminhos concluídos para limpar.")
        return

    cursor.execute("DELETE FROM pedidos WHERE status = 'concluido'")
    conn.commit()

    await callback.message.answer(
        f"🧹 {total} pergaminho(s) concluído(s) foram apagados.",
        reply_markup=menu_pv()
    )


# ============================================================
# BOTÕES PRINCIPAIS: SECRETARIA / ARQUIVOS
# ============================================================

@dp.callback_query(F.data == "abrir_secretaria_hogwarts")
async def abrir_secretaria_hogwarts(callback: CallbackQuery):
    if callback.from_user.id not in OWNER_IDS:
        await callback.answer("Sem permissão.", show_alert=True)
        return

    grupo_selecionado[callback.from_user.id] = GRUPO_UNICO_ID
    ensure_chat_settings(GRUPO_UNICO_ID)
    limpar_estado_arquivos(callback.from_user.id)

    await callback.message.answer(
        f"📜 CONFIGURAÇÕES\n"
        f"Grupo: {GRUPO_UNICO_NOME}\n\n"
        f"Selecione a configuração que deseja alterar.",
        reply_markup=menu_configuracoes
    )
    await callback.answer()


@dp.callback_query(F.data == "abrir_arquivos_hogwarts")
async def abrir_arquivos_hogwarts(callback: CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Sem permissão.", show_alert=True)
        return

    grupo_selecionado[callback.from_user.id] = GRUPO_UNICO_ID
    limpar_estado_secretaria(GRUPO_UNICO_ID)

    await callback.message.answer(
        "📚 Ala dos Arquivos de Hogwarts\n\n"
        "Escolha uma opção abaixo:",
        reply_markup=menu_pv()
    )
    await callback.answer()


@dp.message(F.chat.type == "private")
async def capturar_configuracoes(message: Message):
    chat_id = get_selected_group(message.from_user.id) or GRUPO_UNICO_ID
    if not chat_id:
        return

    def voltar_modo_noturno_kb():
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🌙 Voltar ao Modo Noturno", callback_data="cfg_night")]
            ]
        )

    if chat_id in night_waiting_text_start:
        texto = message.text or ""
        cur.execute("UPDATE night_config SET text_start=? WHERE chat_id=?", (texto, chat_id))
        db.commit()
        night_waiting_text_start.discard(chat_id)
        await message.answer(
            "✅ Texto de início do Modo Noturno salvo com sucesso!\n\n"
            "📌 Texto salvo:\n\n"
            f"{texto}",
            reply_markup=voltar_modo_noturno_kb()
        )
        return

    if chat_id in night_waiting_text_end:
        texto = message.text or ""
        cur.execute("UPDATE night_config SET text_end=? WHERE chat_id=?", (texto, chat_id))
        db.commit()
        night_waiting_text_end.discard(chat_id)
        await message.answer(
            "✅ Texto de encerramento do Modo Noturno salvo com sucesso!\n\n"
            "📌 Texto salvo:\n\n"
            f"{texto}",
            reply_markup=voltar_modo_noturno_kb()
        )
        return

    if chat_id in night_waiting_media_start:
        if message.photo:
            media_type = "photo"
            file_id = message.photo[-1].file_id
        elif message.video:
            media_type = "video"
            file_id = message.video.file_id
        elif message.document:
            media_type = "document"
            file_id = message.document.file_id
        else:
            await message.answer("Envie foto, vídeo ou documento.", reply_markup=voltar_modo_noturno_kb())
            return

        cur.execute(
            "UPDATE night_config SET media_start_type=?, media_start_file_id=? WHERE chat_id=?",
            (media_type, file_id, chat_id)
        )
        db.commit()
        night_waiting_media_start.discard(chat_id)
        await message.answer(
            "✅ Mídia de início do Modo Noturno salva com sucesso!",
            reply_markup=voltar_modo_noturno_kb()
        )
        return

    if chat_id in night_waiting_media_end:
        if message.photo:
            media_type = "photo"
            file_id = message.photo[-1].file_id
        elif message.video:
            media_type = "video"
            file_id = message.video.file_id
        elif message.document:
            media_type = "document"
            file_id = message.document.file_id
        else:
            await message.answer("Envie foto, vídeo ou documento.", reply_markup=voltar_modo_noturno_kb())
            return

        cur.execute(
            "UPDATE night_config SET media_end_type=?, media_end_file_id=? WHERE chat_id=?",
            (media_type, file_id, chat_id)
        )
        db.commit()
        night_waiting_media_end.discard(chat_id)
        await message.answer(
            "✅ Mídia de encerramento do Modo Noturno salva com sucesso!",
            reply_markup=voltar_modo_noturno_kb()
        )
        return

    if chat_id in aguardando_timezone_location:
        if not message.location:
            await message.answer("Envie uma localização para definir o fuso horário.")
            return

        longitude = message.location.longitude

        if longitude < -45:
            set_night_timezone(chat_id, "America/Fortaleza", -3)
        elif longitude < -30:
            set_night_timezone(chat_id, "America/Sao_Paulo", -3)
        else:
            set_night_timezone(chat_id, "UTC", 0)

        aguardando_timezone_location.discard(chat_id)

        await message.answer("Fuso horário definido para America/Fortaleza.")
        await message.answer(
            night_timezone_text(chat_id),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")]
                ]
            )
        )
        return

    if chat_id in aguardando_welcome_text:
        if not message.text:
            await message.answer("Envie uma mensagem de texto.")
            return

        save_welcome_text(chat_id, message.text)
        aguardando_welcome_text.discard(chat_id)

        await message.answer(
            "✅ Texto de boas-vindas salvo com sucesso!\n\n"
            "Use os botões abaixo para continuar configurando.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Voltar ao menu Boas-vindas", callback_data="cfg_welcome")]
                ]
            )
        )
        return

    if chat_id in aguardando_welcome_media:
        caption = message.caption or ""

        if message.photo:
            save_welcome_media(chat_id, "photo", message.photo[-1].file_id, caption)
        elif message.video:
            save_welcome_media(chat_id, "video", message.video.file_id, caption)
        elif message.document:
            save_welcome_media(chat_id, "document", message.document.file_id, caption)
        elif message.sticker:
            save_welcome_media(chat_id, "sticker", message.sticker.file_id, "")
        else:
            await message.answer("Envie foto, vídeo, documento ou sticker.")
            return

        aguardando_welcome_media.discard(chat_id)

        await message.answer(
            "✅ Mídia de boas-vindas salva com sucesso!\n\n"
            "Use os botões abaixo para continuar configurando.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Voltar ao menu Boas-vindas", callback_data="cfg_welcome")]
                ]
            )
        )
        return

    if chat_id in aguardando_rules_text:
        if not message.text:
            await message.answer("Envie uma mensagem de texto.")
            return

        save_rules_text(chat_id, message.text)
        aguardando_rules_text.discard(chat_id)

        await message.answer("✅ Mensagem salva.")
        await message.answer(
            rules_status_text(chat_id),
            reply_markup=menu_rules_main(chat_id)
        )
        return

    if chat_id in aguardando_rules_media:
        caption = message.caption or ""

        if message.photo:
            save_rules_media(chat_id, "photo", message.photo[-1].file_id, caption)
        elif message.video:
            save_rules_media(chat_id, "video", message.video.file_id, caption)
        elif message.document:
            save_rules_media(chat_id, "document", message.document.file_id, caption)
        elif message.sticker:
            save_rules_media(chat_id, "sticker", message.sticker.file_id, "")
        else:
            await message.answer("Envie foto, vídeo, documento ou sticker.")
            return

        aguardando_rules_media.discard(chat_id)

        await message.answer("✅ Mídia salva.")
        await message.answer(
            rules_status_text(chat_id),
            reply_markup=menu_rules_main(chat_id)
        )
        return


night_last_event = {}


async def apagar_ultima_msg_noturna(chat_id: int):
    """Apaga a última mensagem automática do modo noturno, se existir."""
    try:
        cur.execute("SELECT last_message_id FROM night_config WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        last_id = int(row[0] or 0) if row else 0
        if last_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=last_id)
            except Exception as e:
                print("AVISO: não consegui apagar a mensagem noturna anterior:", repr(e))
            cur.execute("UPDATE night_config SET last_message_id=0 WHERE chat_id=?", (chat_id,))
            db.commit()
    except Exception as e:
        print("ERRO AO LIMPAR MENSAGEM NOTURNA:", repr(e))


async def fechar_topico_invocacao(chat_id: int):
    """Fecha somente o tópico Câmara de Invocação."""
    try:
        await bot.close_forum_topic(
            chat_id=chat_id,
            message_thread_id=TOPICO_MODO_NOTURNO_ID
        )
    except Exception as e:
        if "TOPIC_NOT_MODIFIED" not in repr(e):
            print("ERRO AO FECHAR TÓPICO DO MODO NOTURNO:", repr(e))


async def abrir_topico_invocacao(chat_id: int):
    """Reabre somente o tópico Câmara de Invocação."""
    try:
        await bot.reopen_forum_topic(
            chat_id=chat_id,
            message_thread_id=TOPICO_MODO_NOTURNO_ID
        )
    except Exception as e:
        if "TOPIC_NOT_MODIFIED" not in repr(e):
            print("ERRO AO ABRIR TÓPICO DO MODO NOTURNO:", repr(e))


async def enviar_mensagem_noturna_no_topico(chat_id: int, tipo: str):
    """Envia texto/mídia personalizada no tópico da Câmara de Invocação e guarda o ID."""
    data = get_night_data(chat_id)

    if tipo == "start":
        text = data.get("text_start") or ""
        media_type = data.get("media_start_type") or ""
        file_id = data.get("media_start_file_id") or ""
    else:
        text = data.get("text_end") or ""
        media_type = data.get("media_end_type") or ""
        file_id = data.get("media_end_file_id") or ""

    sent = None
    try:
        if file_id:
            if media_type == "photo":
                sent = await bot.send_photo(chat_id=chat_id, photo=file_id, caption=text or None,
                                            message_thread_id=TOPICO_MODO_NOTURNO_ID)
            elif media_type == "video":
                sent = await bot.send_video(chat_id=chat_id, video=file_id, caption=text or None,
                                            message_thread_id=TOPICO_MODO_NOTURNO_ID)
            elif media_type == "document":
                sent = await bot.send_document(chat_id=chat_id, document=file_id, caption=text or None,
                                               message_thread_id=TOPICO_MODO_NOTURNO_ID)
        elif text:
            sent = await bot.send_message(chat_id=chat_id, text=text, message_thread_id=TOPICO_MODO_NOTURNO_ID)

        if sent:
            cur.execute("UPDATE night_config SET last_message_id=? WHERE chat_id=?", (sent.message_id, chat_id))
            db.commit()
    except Exception as e:
        print("ERRO AO ENVIAR MENSAGEM DO MODO NOTURNO:", repr(e))


def noite_ativa_agora(data: dict) -> bool:
    now = datetime.utcnow() + timedelta(hours=data["timezone_offset"])
    h = now.hour
    start = int(data["start_hour"])
    end = int(data["end_hour"])

    if start < end:
        return start <= h < end
    return h >= start or h < end


async def aplicar_modo_noturno(chat_id: int, estado: str):
    """
    estado='closed' fecha a Câmara de Invocação e manda mensagem de início.
    estado='open' abre a Câmara de Invocação e manda mensagem de fim.
    """
    ensure_night_config(chat_id)

    cur.execute("SELECT last_state FROM night_config WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    estado_atual = (row[0] or "") if row else ""

    if estado_atual == estado:
        return

    await apagar_ultima_msg_noturna(chat_id)

    if estado == "closed":
        await fechar_topico_invocacao(chat_id)
        await enviar_mensagem_noturna_no_topico(chat_id, "start")
    else:
        await abrir_topico_invocacao(chat_id)
        await enviar_mensagem_noturna_no_topico(chat_id, "end")

    cur.execute("UPDATE night_config SET last_state=? WHERE chat_id=?", (estado, chat_id))
    db.commit()


async def send_night_message(chat_id: int, tipo: str):
    # Compatibilidade com chamadas antigas.
    await enviar_mensagem_noturna_no_topico(chat_id, tipo)


async def night_mode_checker():
    while True:
        try:
            cur.execute("SELECT chat_id FROM night_config WHERE enabled=1")
            chats = cur.fetchall()

            for row in chats:
                chat_id = row[0]
                data = get_night_data(chat_id)
                estado = "closed" if noite_ativa_agora(data) else "open"
                await aplicar_modo_noturno(chat_id, estado)

        except Exception as e:
            print("ERRO NO CHECKER NOTURNO:", repr(e))

        await asyncio.sleep(60)


# =========================
# MAIN
# =========================


# ============================================================
# INICIALIZAÇÃO
# ============================================================

async def set_commands_hogwarts():
    await bot.set_my_commands([
        BotCommand(command="start", description="Abrir menu Hogwarts"),
        BotCommand(command="menu", description="Abrir menu Hogwarts"),
        BotCommand(command="settings", description="Abrir Secretaria Hogwarts"),
        BotCommand(command="regras", description="Ver regras"),
        BotCommand(command="rules", description="View rules"),
        BotCommand(command="staff", description="Ver staff"),
        BotCommand(command="me", description="Meu perfil"),
        BotCommand(command="link", description="Link do grupo"),
        BotCommand(command="translate", description="Traduzir"),
        BotCommand(command="topic_welcome", description="Definir tópico"),
    ])


# =========================
# MODO NOTURNO — TÓPICO CÂMARA DE INVOCAÇÃO
# =========================

@dp.callback_query(F.data == "cfg_night")
async def cb_cfg_night_real(callback: CallbackQuery):
    if not callback.from_user or callback.from_user.id not in OWNER_IDS:
        await safe_answer(callback, "Sem permissão.", show_alert=True)
        return

    chat_id = get_selected_group(callback.from_user.id) or GRUPO_UNICO_ID
    ensure_night_config(chat_id)

    await safe_edit(
        callback.message,
        night_status_text(chat_id),
        reply_markup=menu_night_main(chat_id)
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "night_enable")
async def cb_night_enable_real(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id) or GRUPO_UNICO_ID

    cur.execute("UPDATE night_config SET enabled=1 WHERE chat_id=?", (chat_id,))
    db.commit()

    data = get_night_data(chat_id)
    if noite_ativa_agora(data):
        await aplicar_modo_noturno(chat_id, "closed")
    else:
        await aplicar_modo_noturno(chat_id, "open")

    await safe_edit(callback.message, night_status_text(chat_id), reply_markup=menu_night_main(chat_id))
    await safe_answer(callback, "Modo noturno ativado.")


@dp.callback_query(F.data == "night_disable")
async def cb_night_disable_real(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id) or GRUPO_UNICO_ID

    cur.execute("UPDATE night_config SET enabled=0 WHERE chat_id=?", (chat_id,))
    db.commit()

    await apagar_ultima_msg_noturna(chat_id)
    await abrir_topico_invocacao(chat_id)
    cur.execute("UPDATE night_config SET last_state='open' WHERE chat_id=?", (chat_id,))
    db.commit()

    await safe_edit(callback.message, night_status_text(chat_id), reply_markup=menu_night_main(chat_id))
    await safe_answer(callback, "Modo noturno desativado.")


@dp.callback_query(F.data == "night_set_time")
async def cb_night_set_time_real(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id) or GRUPO_UNICO_ID
    night_select_start.add(chat_id)
    await safe_edit(callback.message, "🕘 Selecione a hora de INÍCIO:", reply_markup=menu_night_hours("night_start"))
    await safe_answer(callback)


@dp.callback_query(F.data.startswith("night_start:"))
async def cb_night_start_real(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id) or GRUPO_UNICO_ID
    hour = int(callback.data.split(":")[1])
    night_select_end[chat_id] = hour

    await safe_edit(
        callback.message,
        f"✅ Início: {hour}h\n\n👉 Agora selecione a hora de FIM:",
        reply_markup=menu_night_hours("night_end")
    )
    await safe_answer(callback)


@dp.callback_query(F.data.startswith("night_end:"))
async def cb_night_end_real(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id) or GRUPO_UNICO_ID
    end_hour = int(callback.data.split(":")[1])
    start_hour = night_select_end.get(chat_id, 22)

    cur.execute("UPDATE night_config SET start_hour=?, end_hour=? WHERE chat_id=?", (start_hour, end_hour, chat_id))
    db.commit()
    night_select_end.pop(chat_id, None)

    data = get_night_data(chat_id)
    if data["enabled"]:
        if noite_ativa_agora(data):
            await aplicar_modo_noturno(chat_id, "closed")
        else:
            await aplicar_modo_noturno(chat_id, "open")

    await safe_edit(callback.message, night_status_text(chat_id), reply_markup=menu_night_main(chat_id))
    await safe_answer(callback, "Horário definido.")


@dp.callback_query(F.data == "night_text_start")
async def cb_night_text_start_real(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id) or GRUPO_UNICO_ID
    night_waiting_text_start.add(chat_id)
    await safe_edit(
        callback.message,
        "✍️ Envie agora o texto de início do modo noturno:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")]])
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "night_text_end")
async def cb_night_text_end_real(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id) or GRUPO_UNICO_ID
    night_waiting_text_end.add(chat_id)
    await safe_edit(
        callback.message,
        "✍️ Envie agora o texto de fim do modo noturno:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")]])
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "night_media_start")
async def cb_night_media_start_real(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id) or GRUPO_UNICO_ID
    night_waiting_media_start.add(chat_id)
    await safe_edit(
        callback.message,
        "🖼️ Envie agora a mídia de início do modo noturno:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")]])
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "night_media_end")
async def cb_night_media_end_real(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id) or GRUPO_UNICO_ID
    night_waiting_media_end.add(chat_id)
    await safe_edit(
        callback.message,
        "🖼️ Envie agora a mídia de fim do modo noturno:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")]])
    )
    await safe_answer(callback)


@dp.callback_query(F.data == "night_timezone")
async def cb_night_timezone_real(callback: CallbackQuery):
    chat_id = get_selected_group(callback.from_user.id) or GRUPO_UNICO_ID
    aguardando_timezone_location.add(chat_id)
    await safe_edit(
        callback.message,
        night_timezone_text(chat_id) + "\n\n📍 Envie uma localização para definir o fuso.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Voltar", callback_data="cfg_night")]])
    )
    await safe_answer(callback)


async def main():
    ensure_chat_settings(GRUPO_UNICO_ID)
    ensure_rules_config(GRUPO_UNICO_ID)
    ensure_welcome_config(GRUPO_UNICO_ID)
    ensure_topic_config(GRUPO_UNICO_ID)
    ensure_permissions_config(GRUPO_UNICO_ID)
    ensure_night_config(GRUPO_UNICO_ID)

    await bot.delete_webhook(drop_pending_updates=True)
    await set_commands_hogwarts()

    asyncio.create_task(night_mode_checker())

    print("Bot Hogwarts online...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())