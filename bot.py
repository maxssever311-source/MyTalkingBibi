import os
import sys
import traceback
import random
import asyncio
from datetime import datetime, timedelta
from aiohttp import web
import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatPermissions,
    BotCommand
)
from google import genai

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

print("--> Инициализация скрипта bot.py...", flush=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

if not BOT_TOKEN:
    print("КРИТИЧЕСКАЯ ОШИБКА: Переменная BOT_TOKEN пуста или не задана в Render!", flush=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

gemini_client = None
if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        print("--> Клиент Gemini успешно подключен.", flush=True)
    except Exception as e:
        print(f"Ошибка инициализации Gemini: {e}", flush=True)

DB_FILE = "bibi_rpg.db"

RANKS = {
    0: {
        "name": "Облитый водометом крикун с улицы Каплан",
        "req_loyalty": 0,
        "req_shekels": 0,
        "desc": "Палаточный активист без обуви и прав. Питается грантами и лозунгами."
    },
    1: {
        "name": "Инспектор по кошерности фалафеля в Бат-Яме",
        "req_loyalty": 25,
        "req_shekels": 100,
        "desc": "Мелкий чиновник-вымогатель. Проверяет майонез на кошерность."
    },
    2: {
        "name": "Депутат-кнопкодав из буфета Кнессета",
        "req_loyalty": 50,
        "req_shekels": 300,
        "desc": "Жмет нужные кнопки за бутерброд с тунцом и пассивный депутатский оклад."
    },
    3: {
        "name": "Глава комиссии по распилу кондиционеров",
        "req_loyalty": 70,
        "req_shekels": 800,
        "desc": "Аппаратный хищник с правом назначать налоговые проверки конкурентам."
    },
    4: {
        "name": "Министр национальной паники и закулисного дерибана",
        "req_loyalty": 85,
        "req_shekels": 1800,
        "desc": "Член узкого кабинета. Включает сирены, чтобы скрыть офшоры."
    },
    5: {
        "name": "Личный носитель чемоданов Сары Нетаньяху",
        "req_loyalty": 95,
        "req_shekels": 4000,
        "desc": "Серый кардинал и абсолютный фаворит премьера. Неприкосновенная персона."
    }
}

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT NULL,
                full_name TEXT,
                loyalty INTEGER DEFAULT 30,
                shekels INTEGER DEFAULT 120,
                rank INTEGER DEFAULT 0,
                role_title TEXT DEFAULT 'Свободный гражданин',
                bloc_id INTEGER DEFAULT NULL,
                immune_until TEXT DEFAULT NULL,
                bunker_until TEXT DEFAULT NULL,
                karma_counter INTEGER DEFAULT 0,
                last_salary TEXT DEFAULT NULL,
                last_gesheft TEXT DEFAULT NULL
            )
        """)
        # Безопасное добавление колонки username, если база уже была создана ранее
        try:
            await db.execute("ALTER TABLE users ADD COLUMN username TEXT DEFAULT NULL")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS blocs (
                bloc_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                leader_id INTEGER,
                sponsor TEXT DEFAULT 'Внутренний резерв',
                treasury INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.commit()
    print("--> База данных SQLite готова.", flush=True)

async def get_or_create_user(user_id: int, full_name: str, username: str = None):
    clean_uname = username.lower().lstrip("@") if username else None
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row:
            await db.execute(
                "INSERT INTO users (user_id, username, full_name, loyalty, shekels, rank, role_title) VALUES (?, ?, ?, 30, 120, 0, ?)",
                (user_id, clean_uname, full_name, RANKS[0]["name"])
            )
            await db.commit()
            return await get_or_create_user(user_id, full_name, username)
        
        cols = [d[0] for d in cursor.description]
        user = dict(zip(cols, row))
        
        # Обновляем имя и username при активности
        if clean_uname and user.get("username") != clean_uname:
            await db.execute("UPDATE users SET username = ?, full_name = ? WHERE user_id = ?", (clean_uname, full_name, user_id))
            await db.commit()
            user["username"] = clean_uname
            user["full_name"] = full_name

        new_rank = 0
        for r in sorted(RANKS.keys(), reverse=True):
            if user["loyalty"] >= RANKS[r]["req_loyalty"] and user["shekels"] >= RANKS[r]["req_shekels"]:
                new_rank = r
                break
        
        if new_rank != user["rank"]:
            await db.execute("UPDATE users SET rank = ?, role_title = ? WHERE user_id = ?", (new_rank, RANKS[new_rank]["name"], user_id))
            await db.commit()
            user["rank"] = new_rank
            user["role_title"] = RANKS[new_rank]["name"]
            
        return user

async def get_user_by_username(username: str):
    clean_uname = username.lower().lstrip("@").strip()
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT * FROM users WHERE username = ?", (clean_uname,))
        row = await cursor.fetchone()
        if row:
            cols = [d[0] for d in cursor.description]
            user = dict(zip(cols, row))
            return await get_or_create_user(user["user_id"], user["full_name"], user.get("username"))
    return None

async def update_user(user_id: int, **kwargs):
    fields = [f"{k} = ?" for k in kwargs.keys()]
    values = list(kwargs.values()) + [user_id]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?", values)
        await db.commit()

async def get_user_bloc(bloc_id: int):
    if not bloc_id:
        return None
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT * FROM blocs WHERE bloc_id = ?", (bloc_id,))
        row = await cursor.fetchone()
        if row:
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
    return None

def is_shabbat() -> bool:
    now = datetime.now()
    if (now.weekday() == 4 and now.hour >= 17) or (now.weekday() == 5 and now.hour < 20):
        return True
    return False

# --- РЕГИСТРАЦИЯ КОМАНД В ТЕЛЕГРАМ ---
async def set_bot_commands(bot_instance: Bot):
    commands = [
        BotCommand(command="menu", description="Оперативный пульт коалиции (или слово «Пульт»)"),
        BotCommand(command="dossier", description="Анкета коалиции (или слово «Досье»)"),
        BotCommand(command="gesheft", description="Распил бюджета (или слово «Гешефт»)"),
        BotCommand(command="strike", description="Превентивный удар (или слово «Удар»)"),
        BotCommand(command="slots", description="Слоты (/slots 50 или «Слоты 50»)"),
        BotCommand(command="thimbles", description="Наперстки (/thimbles 30 1 или «Наперстки 30 1»)"),
        BotCommand(command="shop", description="Спецбуфет канцелярии (или слово «Буфет»)"),
        BotCommand(command="bribe", description="Занести сигары (или слово «Сигары»)"),
        BotCommand(command="protest", description="Орать в мегафон (или слово «Протест»)"),
        BotCommand(command="shakedown", description="Реплаем: потрясти ларек (или «Ларек»)"),
        BotCommand(command="audit", description="Реплаем: налоговая проверка (или «Аудит»)"),
        BotCommand(command="kompromat", description="Реплаем: слить компромат (или «Компромат»)"),
        BotCommand(command="martial_law", description="Военное положение на 20 мин (Ранг 4+)"),
        BotCommand(command="veto", description="Реплаем: право вето (или слово «Вето»)"),
        BotCommand(command="found_bloc", description="Основать фракцию (/found_bloc Название)"),
        BotCommand(command="bloc_sponsor", description="Внешний вектор (/bloc_sponsor Умань)"),
        BotCommand(command="threat", description="Очертить красную линию (/threat тема)"),
        BotCommand(command="pager", description="Реплаем: проверка пейджера (или «Пейджер»)"),
        BotCommand(command="pay", description="Реплаем: перевести шекели (или «Откат 50»)")
    ]
    await bot_instance.set_my_commands(commands)
    print("--> Команды меню успешно зарегистрированы в Telegram.", flush=True)

# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎯 Превентивный удар", callback_data="btn_strike"),
            InlineKeyboardButton(text="📁 Моё досье", callback_data="btn_dossier")
        ],
        [
            InlineKeyboardButton(text="🎰 Слоты «Мифаль ха-Блеф»", callback_data="btn_slots_info"),
            InlineKeyboardButton(text="🛍 Спецбуфет Кейсарии", callback_data="btn_shop")
        ],
        [
            InlineKeyboardButton(text="🍾 Занести сигары (50 ₪)", callback_data="btn_bribe"),
            InlineKeyboardButton(text="💰 Получить гешефт", callback_data="btn_gesheft")
        ]
    ])

def get_shop_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🛡 Железный купол (250 ₪)", callback_data="buy_dome"),
            InlineKeyboardButton(text="🚪 Бункер Кирьи (500 ₪)", callback_data="buy_bunker")
        ],
        [
            InlineKeyboardButton(text="🍾 Ящик Dom Pérignon (300 ₪)", callback_data="buy_champagne"),
            InlineKeyboardButton(text="📟 Заминированный пейджер (400 ₪)", callback_data="buy_pager")
        ],
        [
            InlineKeyboardButton(text="🔙 Назад в пульт", callback_data="btn_back")
        ]
    ])

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def render_progress_bar(val: int, max_val: int = 100) -> str:
    filled = int(max(0, min(val, max_val)) / 10)
    return "■" * filled + "□" * (10 - filled)

async def apply_chat_mute(message: Message, user_id: int, minutes: int, reason: str):
    until = datetime.now() + timedelta(minutes=minutes)
    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        await message.reply(f"⛓ <b>ШАБАК изолировал нарушителя на {minutes} мин!</b>\nПричина: {reason}", parse_mode="HTML")
    except Exception:
        await message.reply("⚠️ Юридический советник заблокировал изоляцию (нет прав админа у бота), но факт нарушения занесен в реестр.")

async def build_dossier_text(user: dict) -> str:
    bloc = await get_user_bloc(user["bloc_id"])
    bloc_title = f"{bloc['name']} (Куратор: {bloc['sponsor']})" if bloc else "Внефракционный одиночка"
    
    now_iso = datetime.now().isoformat()
    dome_status = "🛡 Активен" if (user["immune_until"] and user["immune_until"] > now_iso) else "Не активен"
    bunker_status = "🚪 В укрытии (Иммунитет к ШАБАК)" if (user["bunker_until"] and user["bunker_until"] > now_iso) else "Не активен"

    analysis = "Досье находится под грифом «Совершенно секретно»."
    if gemini_client:
        try:
            prompt = (
                f"Ты — премьер Израиля Биньямин Нетаньяху. Дай короткую, едкую саркастическую характеристику политику "
                f"по имени {user['full_name']}. Его ранг: [{user['rank']}] {user['role_title']}, лояльность: {user['loyalty']}%, "
                f"фракция: '{bloc_title}'. Максимум 2 предложения в высокомерном и циничном тоне."
            )
            res = gemini_client.models.generate_content(model='gemini-3.8-flash', contents=prompt)
            analysis = res.text
        except Exception as e:
            print(f"Ошибка Gemini API: {e}", flush=True)

    return (
        f"🏛 <b>ГОСУДАРСТВЕННЫЙ РЕЕСТР КОАЛИЦИИ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Субъект:</b> {user['full_name']}\n"
        f"🎖 <b>Ранг [{user['rank']}/5]:</b> <i>{user['role_title']}</i>\n"
        f"🚩 <b>Фракция:</b> {bloc_title}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Лояльность:</b> [{render_progress_bar(user['loyalty'])}] {user['loyalty']}%\n"
        f"💰 <b>Казна:</b> {user['shekels']} ₪\n"
        f"🛡 <b>Железный купол:</b> {dome_status}\n"
        f"🚪 <b>Бункер Кирьи:</b> {bunker_status}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Резолюция канцелярии премьера:</b>\n<i>«{analysis}»</i>"
    )

# --- ЛОГИКА ОСНОВНЫХ КОМАНД ---
async def logic_menu(message: Message):
    text = (
        "🏛 <b>Канцелярия премьер-министра Израиля</b>\n\n"
        "Оперативный пульт коалиции активирован. Можно жать кнопки или писать команды прямо в чат по-русски "
        "(<i>Досье, Гешефт, Удар, Сигары, Буфет, Слоты 50</i>):"
    )
    await message.reply(text, reply_markup=get_main_keyboard(), parse_mode="HTML")

async def logic_dossier(message: Message, args_str: str = None):
    # Сохраняем вызывающего в БД
    await get_or_create_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    
    target_user = None
    # 1. Если это ответ (реплай) на чье-то сообщение — берем автора того сообщения
    if message.reply_to_message and message.reply_to_message.from_user:
        t = message.reply_to_message.from_user
        if t.is_bot:
            await message.reply("🤖 У роботов канцелярии нет политических амбиций и счетов в банке.")
            return
        target_user = await get_or_create_user(t.id, t.full_name, t.username)
    
    # 2. Если передан @username или текстовое упоминание (text_mention)
    elif args_str or message.entities:
        if message.entities:
            for ent in message.entities:
                if ent.type == "text_mention" and ent.user:
                    target_user = await get_or_create_user(ent.user.id, ent.user.full_name, ent.user.username)
                    break
        if not target_user and args_str:
            word = args_str.split()[0]
            if word.startswith("@"):
                target_user = await get_user_by_username(word)
                if not target_user:
                    await message.reply(
                        f"🕵️‍♂️ Агент <b>{word}</b> ещё не засветился в базе ШАБАК.\n"
                        f"Пусть напишет хотя бы одно сообщение в чат, или вызовите «Досье» ответом (реплаем) на его сообщение!",
                        parse_mode="HTML"
                    )
                    return

    # 3. Если никого не отметили — показываем досье самого автора
    if not target_user:
        target_user = await get_or_create_user(message.from_user.id, message.from_user.full_name, message.from_user.username)

    card = await build_dossier_text(target_user)
    await message.reply(card, parse_mode="HTML")

async def logic_gesheft(message: Message, u_id: int = None, u_name: str = None, u_username: str = None):
    uid = u_id or message.from_user.id
    uname = u_name or message.from_user.full_name
    uuser = u_username or message.from_user.username
    user = await get_or_create_user(uid, uname, uuser)
    now = datetime.now()
    
    if user["last_gesheft"]:
        last = datetime.fromisoformat(user["last_gesheft"])
        if now - last < timedelta(hours=4):
            rem = timedelta(hours=4) - (now - last)
            mins = int(rem.total_seconds() // 60)
            await message.reply(f"⏳ Казначейство опечатано. Следующий распил бюджета доступен через {mins} мин.")
            return

    reward = random.randint(35, 75) + (user["rank"] * 10)
    await update_user(uid, shekels=user["shekels"] + reward, last_gesheft=now.isoformat())
    await message.reply(
        f"💰 <b>Гешефт успешно оформлен!</b>\n"
        f"Списано по статье: «Закупка противодиверсионных фиников для канцелярии».\n"
        f"Гражданину {user['full_name']} начислено: <b>+{reward} ₪</b>.",
        parse_mode="HTML"
    )

async def logic_bribe(message: Message, u_id: int = None, u_name: str = None, u_username: str = None):
    uid = u_id or message.from_user.id
    uname = u_name or message.from_user.full_name
    uuser = u_username or message.from_user.username
    user = await get_or_create_user(uid, uname, uuser)
    if is_shabbat():
        await message.reply("🕯 <b>Шаббат шалом!</b> Брать и давать взятки в святой день строжайше запрещено раввинатом.", parse_mode="HTML")
        return
    if user["shekels"] < 50:
        await message.reply("🍾 <b>Канцелярия премьера:</b> В вашем бюджете нет 50 ₪. Коалиция не работает под честное слово.", parse_mode="HTML")
        return
    
    new_loyalty = min(100, user["loyalty"] + 15)
    await update_user(uid, loyalty=new_loyalty, shekels=user["shekels"] - 50)
    await message.reply(
        f"🍾 <b>Поставка дипломатической почтой от {user['full_name']} оформлена.</b>\n"
        "Ящик кубинских сигар доставлен в резиденцию в Кейсарии. Лояльность выросла на <b>+15%</b>.",
        parse_mode="HTML"
    )

async def logic_pay(message: Message, args_str: str):
    if not args_str or not message.reply_to_message:
        await message.reply("Использование: ответьте на сообщение получателя и напишите <code>/pay 50</code> или <code>Откат 50</code>", parse_mode="HTML")
        return
    try:
        amount = int(args_str.strip().split()[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.reply("Укажите корректную сумму шекелей цифрами.")
        return

    sender = await get_or_create_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if sender["shekels"] < amount:
        await message.reply("У вас недостаточно шекелей для осуществления финансовой проводки.")
        return

    target_user = message.reply_to_message.from_user
    receiver = await get_or_create_user(target_user.id, target_user.full_name, target_user.username)

    tax = int(amount * 0.10)
    net_amount = amount - tax
    await update_user(sender["user_id"], shekels=sender["shekels"] - amount)
    await update_user(receiver["user_id"], shekels=receiver["shekels"] + net_amount)

    await message.reply(
        f"💸 <b>Откат переведён!</b>\n"
        f"Отправитель: {sender['full_name']}\n"
        f"Получатель: {receiver['full_name']}\n"
        f"Сумма: <b>{net_amount} ₪</b> (10% госпошлины ушли в фонд Ликуда).",
        parse_mode="HTML"
    )

async def logic_protest(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    outcome = random.random()
    if outcome < 0.70:
        await apply_chat_mute(
            message,
            user["user_id"],
            15,
            "Несанкционированный митинг на перекрестке Азриэли, перекрытие трассы Аялон и крики в мегафон."
        )
    else:
        grant = random.randint(25, 60)
        await update_user(message.from_user.id, shekels=user["shekels"] + grant, loyalty=max(0, user["loyalty"] - 5))
        await message.reply(f"📢 <b>Митинг устоял!</b> Зарубежный фонд выделил вам грант за защиту демократии: <b>+{grant} ₪</b> (лояльность -5%).", parse_mode="HTML")

async def logic_shakedown(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if user["rank"] < 1:
        await message.reply("Вы ещё не доросли до права трясти ларьки. Достигните хотя бы Ранга 1.")
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение жертвы реплаем.")
        return
    
    target_obj = message.reply_to_message.from_user
    victim = await get_or_create_user(target_obj.id, target_obj.full_name, target_obj.username)
    
    if victim["rank"] >= 1:
        await message.reply("Коллег по муниципальному аппарату трясти нельзя — жалуйтесь выше.")
        return
    
    steal = min(victim["shekels"], 25)
    await update_user(victim["user_id"], shekels=victim["shekels"] - steal)
    await update_user(user["user_id"], shekels=user["shekels"] + steal)
    await message.reply(
        f"🧆 <b>Инспекция по кошерности завершена!</b>\n"
        f"Инспектор {user['full_name']} оштрафовал {victim['full_name']} на <b>{steal} ₪</b> за подозрительную тхину.",
        parse_mode="HTML"
    )

async def logic_audit(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if user["rank"] < 3:
        await message.reply("Назначать налоговые проверки разрешено только с Ранга 3 (Глава комиссии).")
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение цели реплаем.")
        return

    target_obj = message.reply_to_message.from_user
    victim = await get_or_create_user(target_obj.id, target_obj.full_name, target_obj.username)
    
    now_iso = datetime.now().isoformat()
    if victim["immune_until"] and victim["immune_until"] > now_iso:
        await message.reply("🛡 У цели активен «Железный купол»! Налоговые аудиторы развернуты на подступах.")
        return

    fine = int(victim["shekels"] * 0.20)
    await update_user(victim["user_id"], shekels=victim["shekels"] - fine)
    await update_user(user["user_id"], shekels=user["shekels"] + fine)
    await message.reply(
        f"📑 <b>Внеплановый финансовый аудит Кнессета!</b>\n"
        f"У субъекта {victim['full_name']} выявлены скрытые доходы.\n"
        f"Списано: <b>{fine} ₪</b> в фонд комиссии депутата {user['full_name']}.",
        parse_mode="HTML"
    )

async def logic_kompromat(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if user["rank"] < 4:
        await message.reply("Аппаратные войны компроматов доступны только министрам кабинета (Ранг 4+).")
        return
    if not message.reply_to_message:
        await message.reply("Укажите реплаем соперника для слива компромата.")
        return

    target_obj = message.reply_to_message.from_user
    victim = await get_or_create_user(target_obj.id, target_obj.full_name, target_obj.username)

    cost = 100
    if user["shekels"] < cost:
        await message.reply(f"Услуги частных детективов Моссада стоят {cost} ₪. У вас не хватает средств.")
        return

    await update_user(user["user_id"], shekels=user["shekels"] - cost)
    success = random.random() < 0.60
    if success:
        new_loyalty = max(0, victim["loyalty"] - 30)
        await update_user(victim["user_id"], loyalty=new_loyalty)
        await message.reply(
            f"📁 <b>Слив в прессу удался!</b>\n"
            f"На министра {victim['full_name']} заведено «Дело 5000». Лояльность сбита на <b>-30%</b>, позиции в коалиции пошатнулись!",
            parse_mode="HTML"
        )
    else:
        await apply_chat_mute(
            message,
            user["user_id"],
            20,
            "Клевета на коллег по коалиции и фальсификация документов канцелярии."
        )

async def logic_martial_law(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if user["rank"] < 4:
        await message.reply("Объявлять чрезвычайное положение могут только члены узкого кабинета (Ранг 4+).")
        return
    until = datetime.now() + timedelta(minutes=20)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR REPLACE INTO system_state (key, value) VALUES ('martial_law', ?)", (until.isoformat(),))
        await db.commit()
    await message.reply(
        "🚨 <b>ВНИМАНИЕ! В ЧАТЕ ОБЪЯВЛЕНО ВОЕННОЕ ПОЛОЖЕНИЕ НА 20 МИНУТ!</b>\n"
        "Любая критика премьера или коалиции карается немедленной изоляцией без предупреждения.",
        parse_mode="HTML"
    )

async def logic_veto(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if user["rank"] < 5:
        await message.reply("Право абсолютного вето принадлежит исключительно любимчику премьера (Ранг 5).")
        return
    await message.reply(
        f"👑 <b>АБСОЛЮТНОЕ ВЕТО КАНЦЕЛЯРИИ!</b>\n"
        f"По прямому указанию {user['full_name']} любое решение, штраф или претензия аннулируются в пользу стабильности коалиции.",
        parse_mode="HTML"
    )

async def logic_found_bloc(message: Message, args_str: str):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if user["rank"] < 2:
        await message.reply("Основывать собственные партии разрешено только с Ранга 2 (Депутат Кнессета).")
        return
    if not args_str:
        await message.reply("Укажите название партии: <code>/found_bloc Блок радикальных евреев Умани</code>", parse_mode="HTML")
        return

    name = args_str.strip()
    cost = 350
    if user["shekels"] < cost:
        await message.reply(f"Регистрация фракции в Минюсте стоит {cost} ₪. У вас недостаточно средств.")
        return

    try:
        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute("INSERT INTO blocs (name, leader_id) VALUES (?, ?)", (name, user["user_id"]))
            bloc_id = cursor.lastrowid
            await db.execute("UPDATE users SET bloc_id = ?, shekels = shekels - ? WHERE user_id = ?", (bloc_id, cost, user["user_id"]))
            await db.commit()
        await message.reply(
            f"🎉 <b>Партия официально зарегистрирована!</b>\n"
            f"Фракция: <b>«{name}»</b>\n"
            f"Лидер: {user['full_name']}\n"
            f"Назначьте внешнего спонсора командой <code>/bloc_sponsor &lt;страна/город&gt;</code>.",
            parse_mode="HTML"
        )
    except Exception:
        await message.reply("Фракция с таким названием уже существует в реестре Кнессета.")

async def logic_bloc_sponsor(message: Message, args_str: str):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if not user["bloc_id"]:
        await message.reply("Вы не состоите ни в одной фракции.")
        return
    bloc = await get_user_bloc(user["bloc_id"])
    if bloc["leader_id"] != user["user_id"]:
        await message.reply("Только лидер фракции может менять внешнего куратора.")
        return
    if not args_str:
        await message.reply("Укажите вектор: <code>/bloc_sponsor Умань (Брацлавский транзит)</code>", parse_mode="HTML")
        return

    sponsor = args_str.strip()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE blocs SET sponsor = ? WHERE bloc_id = ?", (sponsor, bloc["bloc_id"]))
        await db.commit()
    await message.reply(f"🌍 <b>Внешний вектор фракции «{bloc['name']}» изменен:</b> {sponsor}.", parse_mode="HTML")

async def logic_slots(message: Message, args_str: str):
    if is_shabbat():
        await message.reply("🕯 Казино «Мифаль ха-Блеф» опечатано Главным раввинатом до исхода субботы.")
        return
    if not args_str:
        await message.reply("Использование: <code>/slots 50</code> или <code>Слоты 50</code> (ставка в шекелях).", parse_mode="HTML")
        return
    try:
        bet = int(args_str.strip().split()[0])
        if bet <= 0:
            raise ValueError
    except ValueError:
        await message.reply("Укажите корректную сумму ставки цифрами.")
        return

    user = await get_or_create_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if user["shekels"] < bet:
        await message.reply(f"В вашей казне недостаточно шекелей (баланс: {user['shekels']} ₪).")
        return

    symbols = ["🍾", "🚬", "🚁", "⚖️", "👑"]
    weights = [30, 25, 25, 15, 5]
    res = random.choices(symbols, weights=weights, k=3)

    text = f"🎰 <b>ПОДПОЛЬНЫЙ СЛОТ-АВТОМАТ В ЭЙЛАТЕ:</b>\n[ {res[0]} | {res[1]} | {res[2]} ]\n\n"

    if res[0] == res[1] == res[2]:
        sym = res[0]
        if sym == "👑":
            win = bet * 10
            await update_user(user["user_id"], shekels=user["shekels"] + win)
            text += f"👑👑👑 <b>ВЕЧНЫЙ ПРЕМЬЕР! ДЖЕКПОТ x10!</b>\nВы сорвали <b>+{win} ₪</b>! В бункере гремит салют!"
        elif sym == "🚬":
            win = bet * 5
            await update_user(user["user_id"], shekels=user["shekels"] + win)
            text += f"🚬🚬🚬 <b>ДИПЛОМАТИЧЕСКИЙ ГРУЗ x5!</b>\nКонтрабанда прошла успешно: <b>+{win} ₪</b>."
        elif sym == "🍾":
            win = bet * 3
            await update_user(user["user_id"], shekels=user["shekels"] + win, loyalty=min(100, user["loyalty"] + 10))
            text += f"🍾🍾🍾 <b>ВИЛЛА В КЕЙСАРИИ x3!</b>\nВыигрыш: <b>+{win} ₪</b> и +10% к лояльности."
        elif sym == "🚁":
            win = bet * 2
            await update_user(user["user_id"], shekels=user["shekels"] + win)
            text += f"🚁🚁🚁 <b>УСПЕШНЫЙ АВИАНАЛЕТ x2!</b>\nВыигрыш: <b>+{win} ₪</b>."
        elif sym == "⚖️":
            await update_user(user["user_id"], shekels=user["shekels"] - bet)
            await message.reply(text + "⚖️⚖️⚖️ <b>ПРИГОВОР ОКРУЖНОГО СУДА!</b>\nСтавка конфискована, вы отправляетесь на допрос!", parse_mode="HTML")
            await apply_chat_mute(message, user["user_id"], 20, "Нелегальная игорная деятельность в Эйлате.")
            return
    else:
        await update_user(user["user_id"], shekels=user["shekels"] - bet)
        text += f"❌ Комбинация не сыграла. Казна потеряла: <b>-{bet} ₪</b>."

    await message.reply(text, parse_mode="HTML")

async def logic_thimbles(message: Message, args_str: str):
    if is_shabbat():
        await message.reply("🕯 В Шаббат напёрсточники на шуке Кармель закрыты.")
        return
    args = args_str.split() if args_str else []
    if len(args) != 2:
        await message.reply("Использование: <code>/thimbles &lt;ставка&gt; &lt;1-3&gt;</code> или <code>Наперстки 30 1</code>", parse_mode="HTML")
        return
    try:
        bet = int(args[0])
        choice = int(args[1])
        if bet <= 0 or choice not in [1, 2, 3]:
            raise ValueError
    except ValueError:
        await message.reply("Укажите ставку и число от 1 до 3.")
        return

    user = await get_or_create_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if user["shekels"] < bet:
        await message.reply("Недостаточно средств для игры с напёрсточниками.")
        return

    win_pos = random.randint(1, 3)
    if choice == win_pos:
        win = int(bet * 2.5)
        await update_user(user["user_id"], shekels=user["shekels"] + win)
        await message.reply(
            f"🎪 <b>Шук Кармель: ПОБЕДА!</b>\n"
            f"Шекель лежал под наперстком [{win_pos}]! Вы раскусили трюк одесских репатриантов: <b>+{win} ₪</b>.",
            parse_mode="HTML"
        )
    else:
        await update_user(user["user_id"], shekels=user["shekels"] - bet)
        await message.reply(
            f"🎪 <b>Шук Кармель: ОБМАН!</b>\n"
            f"Под стаканчиком [{choice}] ничего не было! Шекель был под [{win_pos}]. Вы потеряли <b>-{bet} ₪</b>.",
            parse_mode="HTML"
        )

async def logic_shop(message: Message):
    text = (
        "🛍 <b>СПЕЦБУФЕТ КАНЦЕЛЯРИИ ПРЕМЬЕРА</b>\n"
        "Товары двойного назначения и дипломатические привилегии:\n\n"
        "🛡 <b>Железный купол (250 ₪):</b> Иммунитет от чужих штрафов, аудитов и ударов на 12 ч.\n"
        "🚪 <b>Бункер Кирьи (500 ₪):</b> Полный щит от прослушки ШАБАК на 24 ч (лояльность не падает).\n"
        "🍾 <b>Ящик Dom Pérignon (300 ₪):</b> Мгновенно аннулирует претензии суда (+25% лояльности).\n"
        "📟 <b>Заминированный пейджер (400 ₪):</b> Разовый инструмент для нейтрализации конкурента."
    )
    await message.reply(text, reply_markup=get_shop_keyboard(), parse_mode="HTML")

async def execute_strike(target_user, chat_id: int, reply_func, restrict_func):
    outcome = random.random()
    win_threshold = 0.35 + (target_user["rank"] * 0.05)
    
    if outcome < win_threshold:
        gain = 50 + (target_user["rank"] * 15)
        new_shekels = target_user["shekels"] + gain
        until = datetime.now() + timedelta(hours=6)
        await update_user(target_user["user_id"], shekels=new_shekels, immune_until=until.isoformat())
        await reply_func(
            f"🎯 <b>Превентивный удар завершен триумфом!</b>\n"
            f"Ликвидирован бункер с вражескими центрифугами.\n"
            f"Вам выделено <b>+{gain} ₪</b> и активирован «Железный купол» на 6 часов.",
            parse_mode="HTML"
        )
    elif outcome < 0.70:
        directive = "Никаких уступок. Мы продолжаем давить на всех дипломатических и военных фронтах."
        if gemini_client:
            try:
                res = gemini_client.models.generate_content(
                    model='gemini-3.8-flash',
                    contents="Сформулируй ультра-короткую жесткую директиву премьера Биби на закрытом заседании кабинета."
                )
                directive = res.text
            except Exception:
                pass
        await reply_func(f"📜 <b>Закрытая директива премьера:</b>\n<i>«{directive}»</i>", parse_mode="HTML")
    else:
        new_shekels = max(0, target_user["shekels"] - 40)
        await update_user(target_user["user_id"], shekels=new_shekels)
        try:
            mute_until = datetime.now() + timedelta(minutes=15)
            await restrict_func(
                chat_id=chat_id,
                user_id=target_user["user_id"],
                permissions=ChatPermissions(can_send_messages=False),
                until_date=mute_until
            )
            await reply_func(
                "💥 <b>ПРОВАЛ СПЕЦОПЕРАЦИИ!</b>\n"
                "Ваша вылазка признана несогласованной авантюрой. Оппозиция требует суда.\n"
                "Штраф: -40 ₪. Вы отправлены на допрос в подвал ШАБАК (мут на 15 минут).",
                parse_mode="HTML"
            )
        except Exception:
            await reply_func("💥 Провал операции! Юридический советник спас от мута, но 40 ₪ списаны.")

async def logic_threat(message: Message, args_str: str):
    topic = args_str or "активность левых активистов"
    enrichment = random.randint(75, 99)
    await message.reply(
        f"🔴 <b>КРАСНАЯ ЛИНИЯ ОЧЕРЧЕНА</b>\n"
        f"Объект угрозы: <b>{topic}</b>\n"
        f"Уровень обогащения: <b>{enrichment}%</b>\n\n"
        f"<i>«Мы не позволим пересечь эту черту. Все варианты действий лежат на столе премьера.»</i>",
        parse_mode="HTML"
    )

async def logic_pager(message: Message):
    if not message.reply_to_message:
        await message.reply("Директива применяется только ответом (реплаем) на сообщение подозреваемого.")
        return
    target = message.reply_to_message.from_user.full_name
    await message.reply(
        f"📟 <b>Служба внешней безопасности:</b>\n"
        f"На пейджер абонента <b>{target}</b> отправлен зашифрованный импульс.\n"
        f"<tg-spoiler>⚠️ Внимание: Зафиксирован критический перегрев литиевого аккумулятора устройства!</tg-spoiler>",
        parse_mode="HTML"
    )

# --- ОБРАБОТЧИКИ СЛЭШ-КОМАНД (/command) ---
@dp.message(Command("menu"))
@dp.message(CommandStart())
async def cmd_menu(message: Message):
    await logic_menu(message)

@dp.message(Command("dossier", "profile"))
async def cmd_dossier(message: Message):
    parts = message.text.split(maxsplit=1)
    args = parts[1] if len(parts) > 1 else None
    await logic_dossier(message, args)

@dp.message(Command("gesheft"))
async def cmd_gesheft(message: Message):
    await logic_gesheft(message)

@dp.message(Command("bribe"))
async def cmd_bribe(message: Message):
    await logic_bribe(message)

@dp.message(Command("pay"))
async def cmd_pay(message: Message):
    parts = message.text.split(maxsplit=1)
    await logic_pay(message, parts[1] if len(parts) > 1 else "")

@dp.message(Command("protest"))
async def cmd_protest(message: Message):
    await logic_protest(message)

@dp.message(Command("shakedown"))
async def cmd_shakedown(message: Message):
    await logic_shakedown(message)

@dp.message(Command("audit"))
async def cmd_audit(message: Message):
    await logic_audit(message)

@dp.message(Command("kompromat"))
async def cmd_kompromat(message: Message):
    await logic_kompromat(message)

@dp.message(Command("martial_law"))
async def cmd_martial_law(message: Message):
    await logic_martial_law(message)

@dp.message(Command("veto"))
async def cmd_veto(message: Message):
    await logic_veto(message)

@dp.message(Command("found_bloc"))
async def cmd_found_bloc(message: Message):
    parts = message.text.split(maxsplit=1)
    await logic_found_bloc(message, parts[1] if len(parts) > 1 else "")

@dp.message(Command("bloc_sponsor"))
async def cmd_bloc_sponsor(message: Message):
    parts = message.text.split(maxsplit=1)
    await logic_bloc_sponsor(message, parts[1] if len(parts) > 1 else "")

@dp.message(Command("slots"))
async def cmd_slots(message: Message):
    parts = message.text.split(maxsplit=1)
    await logic_slots(message, parts[1] if len(parts) > 1 else "")

@dp.message(Command("thimbles"))
async def cmd_thimbles(message: Message):
    parts = message.text.split(maxsplit=1)
    await logic_thimbles(message, parts[1] if len(parts) > 1 else "")

@dp.message(Command("shop"))
async def cmd_shop(message: Message):
    await logic_shop(message)

@dp.message(Command("strike"))
async def cmd_strike(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await execute_strike(user, message.chat.id, message.reply, message.bot.restrict_chat_member)

@dp.message(Command("threat"))
async def cmd_threat(message: Message):
    parts = message.text.split(maxsplit=1)
    await logic_threat(message, parts[1] if len(parts) > 1 else "")

@dp.message(Command("pager"))
async def cmd_pager(message: Message):
    await logic_pager(message)

# --- ОБРАБОТКА ИНЛАЙН КНОПОК ---
@dp.callback_query()
async def process_callbacks(callback: CallbackQuery):
    data = callback.data
    user = await get_or_create_user(callback.from_user.id, callback.from_user.full_name, callback.from_user.username)

    if data == "btn_strike":
        await execute_strike(user, callback.message.chat.id, callback.message.reply, callback.bot.restrict_chat_member)
        await callback.answer()
    elif data == "btn_dossier":
        card = await build_dossier_text(user)
        await callback.message.reply(card, parse_mode="HTML")
        await callback.answer()
    elif data == "btn_gesheft":
        await logic_gesheft(callback.message, callback.from_user.id, callback.from_user.full_name, callback.from_user.username)
        await callback.answer()
    elif data == "btn_bribe":
        await logic_bribe(callback.message, callback.from_user.id, callback.from_user.full_name, callback.from_user.username)
        await callback.answer()
    elif data == "btn_shop":
        await logic_shop(callback.message)
        await callback.answer()
    elif data == "btn_slots_info":
        await callback.message.reply("Чтобы сыграть в слоты, напишите в чат: <code>Слоты 50</code> или <code>/slots 50</code>", parse_mode="HTML")
        await callback.answer()
    elif data == "btn_back":
        await callback.message.edit_text("Оперативный пульт коалиции активирован:", reply_markup=get_main_keyboard())
        await callback.answer()
    elif data.startswith("buy_"):
        item = data.split("_")[1]
        now = datetime.now()
        if item == "dome":
            if user["shekels"] < 250:
                await callback.answer("Недостаточно шекелей на «Железный купол»!", show_alert=True)
                return
            until = now + timedelta(hours=12)
            await update_user(user["user_id"], shekels=user["shekels"] - 250, immune_until=until.isoformat())
            await callback.message.reply("🛡 <b>«Железный купол» развернут!</b> Полная защита от штрафов на 12 часов.", parse_mode="HTML")
            await callback.answer()
        elif item == "bunker":
            if user["shekels"] < 500:
                await callback.answer("Недостаточно шекелей на бункер!", show_alert=True)
                return
            until = now + timedelta(hours=24)
            await update_user(user["user_id"], shekels=user["shekels"] - 500, bunker_until=until.isoformat())
            await callback.message.reply("🚪 <b>Пропуск в бункер Кирьи активирован!</b> Иммунитет к прослушке ШАБАК на 24 часа.", parse_mode="HTML")
            await callback.answer()
        elif item == "champagne":
            if user["shekels"] < 300:
                await callback.answer("Недостаточно шекелей!", show_alert=True)
                return
            await update_user(user["user_id"], shekels=user["shekels"] - 300, loyalty=min(100, user["loyalty"] + 25))
            await callback.message.reply("🍾 <b>Ящик Dom Pérignon доставлен!</b> Претензии суда сняты (+25% к лояльности).", parse_mode="HTML")
            await callback.answer()
        elif item == "pager":
            if user["shekels"] < 400:
                await callback.answer("Недостаточно шекелей!", show_alert=True)
                return
            await update_user(user["user_id"], shekels=user["shekels"] - 400)
            await callback.message.reply("📟 <b>Заминированный пейджер добавлен в ваш инвентарь.</b> Используйте реплаем слово «Пейджер» или /pager.", parse_mode="HTML")
            await callback.answer()

# --- ТЕКСТОВЫЕ КОМАНДЫ БЕЗ СЛЭША, ДИАЛОГ И ПРОСЛУШКА ЧАТА ---
TRIGGER_WORDS = {
    "цахал", "моссад", "шабак", "купол", "меркав", "пейджер", "бункер", "кирь",
    "биби", "нетаньяху", "ликуд", "коалици", "реформ", "побед", "сигар",
    "шампанск", "левиафан", "шекел", "трамп", "авраамов", "каплан", "протест",
    "митинг", "забастовк", "лапид", "ганц", "беннет", "оппозици", "перевыбор",
    "гааг", "мус", "дело 4000", "коррупци", "провал", "поражени", "отставк",
    "иран", "тегеран", "ксир", "хесболл", "хезболл", "хамас", "синвар"
}

@dp.message(F.text)
async def handle_chat_messages(message: Message):
    raw_text = message.text.strip()
    text_lower = raw_text.lower()
    words = raw_text.split(maxsplit=1)
    first_word = words[0].lower().rstrip(".,!?") if words else ""
    rest_args = words[1] if len(words) > 1 else ""

    # Автоматическая регистрация/обновление юзернейма при любом сообщении
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name, message.from_user.username)

    # 1. ПРОВЕРКА РУССКИХ ТЕКСТОВЫХ КОМАНД В СТИЛЕ «ИРИСА»
    # Досье / Анкета / Профиль (работает само по себе, реплаем или с @username)
    if first_word in ["досье", "анкета", "профиль", "кто"] and (not rest_args or rest_args.startswith("@") or len(rest_args.split()) == 1):
        await logic_dossier(message, rest_args if rest_args else None)
        return

    # Одиночные текстовые команды (срабатывают, когда написаны одним словом)
    if not rest_args:
        if first_word in ["пульт", "меню", "коалиция"]:
            await logic_menu(message)
            return
        elif first_word in ["гешефт", "распил", "казна"]:
            await logic_gesheft(message)
            return
        elif first_word in ["взятка", "сигары", "занести"]:
            await logic_bribe(message)
            return
        elif first_word in ["удар", "атака"]:
            await execute_strike(user, message.chat.id, message.reply, message.bot.restrict_chat_member)
            return
        elif first_word in ["магазин", "буфет", "спецбуфет"]:
            await logic_shop(message)
            return
        elif first_word in ["протест", "митинг", "каплан"]:
            await logic_protest(message)
            return
        elif first_word in ["ларек", "ларёк", "потрусить", "штраф"]:
            await logic_shakedown(message)
            return
        elif first_word in ["аудит", "проверка", "налоговая"]:
            await logic_audit(message)
            return
        elif first_word in ["компромат", "слив"]:
            await logic_kompromat(message)
            return
        elif first_word in ["вето"]:
            await logic_veto(message)
            return
        elif first_word in ["пейджер"]:
            await logic_pager(message)
            return

    # Текстовые команды с аргументами (ставка или сумма)
    if first_word in ["слоты", "казино", "рулетка"] and rest_args:
        await logic_slots(message, rest_args)
        return
    elif first_word in ["наперстки", "напёрстки"] and rest_args:
        await logic_thimbles(message, rest_args)
        return
    elif first_word in ["откат", "перевод", "дать"] and rest_args and message.reply_to_message:
        await logic_pay(message, rest_args)
        return

    # 2. ПРЯМОЙ ДИАЛОГ С БИБИ (Упоминание или реплай на сообщение бота)
    bot_info = await message.bot.get_me()
    is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_info.id
    is_mentioned = f"@{bot_info.username.lower()}" in text_lower if bot_info.username else False

    if is_reply_to_bot or is_mentioned:
        clean_text = raw_text.replace(f"@{bot_info.username}", "").strip() if bot_info.username else raw_text
        
        system_instruction = (
            "Ты — Биньямин Нетаньяху (Биби), премьер-министр Израиля. Ты циничный, непоколебимый, авторитарный "
            "политический гроссмейстер. Используй термины: коалиция, Ликуд, Моссад, стратегическая стойкость, красные линии. "
            f"Собеседник: {user['full_name']}, Ранг: [{user['rank']}] {user['role_title']}, Лояльность: {user['loyalty']}%. "
            "Если лояльность низкая — унижай и презирай как уличного крикуна. Если высокая — общайся как с ценным сообщником. "
            "Отвечай кратко, едко, держи образ великого правителя. Никаких извинений."
        )

        if gemini_client:
            try:
                response = gemini_client.models.generate_content(
                    model='gemini-3.8-flash',
                    contents=f"{system_instruction}\n\nРеплика: {clean_text}"
                )
                await message.reply(response.text)
                return
            except Exception as e:
                print(f"Ошибка Gemini API: {e}", flush=True)
                await message.reply("Премьер находится на закрытом совещании в бункере Кирьи. Ожидайте официальное коммюнике.")
                return
        else:
            await message.reply("Спецсвязь с бункером временно отключена.")
            return

    # 3. ФОНОВЫЙ СЕМАНТИЧЕСКИЙ ТРЕКЕР (ШАБАК)
    now_iso = datetime.now().isoformat()
    if user["bunker_until"] and user["bunker_until"] > now_iso:
        return

    has_triggers = any(w in text_lower for w in TRIGGER_WORDS)
    if not has_triggers:
        return

    if gemini_client:
        try:
            eval_prompt = (
                "Ты — закрытый аналитический модуль канцелярии премьера Израиля Биньямина Нетаньяху. "
                "Оцени реплику участника чата с точки зрения лояльности к премьеру, ЦАХАЛу и безопасности Израиля.\n"
                "- Если реплика хвалит премьера, армию, победы, или радуется разгрому/провалу врагов (ХАМАС, Иран, Хезболла) -> верни число от +1 до +3.\n"
                "- Если реплика критикует власть, коалицию, выражает пораженчество, сочувствует врагам или протестам -> верни число от -1 до -3.\n"
                "- Если нейтрально или контекст бытовой -> верни 0.\n"
                f"Реплика: \"{raw_text}\"\n"
                "Формат: строго одно целое число."
            )
            res = gemini_client.models.generate_content(model='gemini-3.8-flash', contents=eval_prompt)
            score_text = res.text.strip().replace("+", "")
            delta = int(score_text)
        except Exception:
            delta = 0

        if delta != 0:
            new_loyalty = max(0, min(100, user["loyalty"] + (delta * 3)))
            await update_user(user["user_id"], loyalty=new_loyalty)
            
            if delta <= -2:
                async with aiosqlite.connect(DB_FILE) as db:
                    cursor = await db.execute("SELECT value FROM system_state WHERE key = 'martial_law'")
                    row = await cursor.fetchone()
                    if row and row[0] > now_iso:
                        await apply_chat_mute(message, user["user_id"], 15, "Крамольные высказывания в период военного положения!")
                        return
                        
                await message.reply(
                    f"👁 <b>ШАБАК фиксирует крамолу:</b>\n"
                    f"Субъект: {user['full_name']} (Лояльность: {new_loyalty}%). Дело передано в военный трибунал.",
                    parse_mode="HTML"
                )
            elif delta >= 2:
                await message.reply(
                    f"📜 <b>Канцелярия премьера одобряет:</b>\n"
                    f"Государственная позиция {user['full_name']} отмечена. Лояльность выросла до {new_loyalty}%.",
                    parse_mode="HTML"
                )

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
async def health_check(request):
    return web.Response(text="Bibi coalition is strong and resilient. 200 OK")

async def main():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"--> Веб-сервер успешно привязан к 0.0.0.0:{port}", flush=True)
    
    await init_db()
    
    try:
        await set_bot_commands(bot)
    except Exception as e:
        print(f"Предупреждение при регистрации команд: {e}", flush=True)
    
    print("--> Запуск Telegram поллинга...", flush=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print("КРИТИЧЕСКАЯ ОШИБКА ПРИ ЗАПУСКЕ:", flush=True)
        traceback.print_exc()
        sys.exit(1)
