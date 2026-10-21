import os
import random
import asyncio
from datetime import datetime, timedelta
from aiohttp import web
import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatPermissions,
    BotCommand
)
from google import genai

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

gemini_client = None
if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"Ошибка инициализации Gemini: {e}")

DB_FILE = "bibi_state.db"

# Ранги и карикатурная табель о рангах
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

async def get_or_create_user(user_id: int, full_name: str):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row:
            await db.execute(
                "INSERT INTO users (user_id, full_name, loyalty, shekels, rank, role_title) VALUES (?, ?, 30, 120, 0, ?)",
                (user_id, full_name, RANKS[0]["name"])
            )
            await db.commit()
            return await get_or_create_user(user_id, full_name)
        
        cols = [d[0] for d in cursor.description]
        user = dict(zip(cols, row))
        
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
        BotCommand(command="menu", description="Оперативный пульт коалиции"),
        BotCommand(command="dossier", description="Анкета коалиции и досье ШАБАК"),
        BotCommand(command="gesheft", description="Распил бюджета (раз в 4 часа)"),
        BotCommand(command="strike", description="Превентивный удар (рулетка)"),
        BotCommand(command="slots", description="Казино «Мифаль ха-Блеф» (/slots 50)"),
        BotCommand(command="thimbles", description="Наперстки на шуке Кармель (/thimbles 30 1)"),
        BotCommand(command="shop", description="Спецбуфет канцелярии (магазин)"),
        BotCommand(command="bribe", description="Занести сигары (50 ₪ за лояльность)"),
        BotCommand(command="protest", description="Орать в мегафон (Ранг 0)"),
        BotCommand(command="shakedown", description="Реплаем: потрясти ларек на 25 ₪ (Ранг 1+)"),
        BotCommand(command="audit", description="Реплаем: налоговая проверка (Ранг 3+)"),
        BotCommand(command="kompromat", description="Реплаем: слить компромат Моссада (Ранг 4+)"),
        BotCommand(command="martial_law", description="Чрезвычайное положение на 20 мин (Ранг 4+)"),
        BotCommand(command="veto", description="Реплаем: право вето фаворита (Ранг 5)"),
        BotCommand(command="found_bloc", description="Основать фракцию (/found_bloc Название)"),
        BotCommand(command="bloc_sponsor", description="Внешний вектор фракции (/bloc_sponsor Умань)"),
        BotCommand(command="threat", description="Очертить красную линию (/threat тема)"),
        BotCommand(command="pager", description="Реплаем: проверка связи безопасности"),
        BotCommand(command="pay", description="Перевести шекели (/pay 50 @user)")
    ]
    await bot_instance.set_my_commands(commands)

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
        await message.reply(f"⛓ <b>ШАБАК изолировал нарушителя на {minutes} мин!</b>\nПричина: {reason}")
    except Exception:
        await message.reply("⚠️ Юридический советник заблокировал изоляцию (нет прав админа у бота), но факт нарушения занесен в реестр.")

# --- КОМАНДЫ ПУЛЬТА И ДОСЬЕ ---
@dp.message(Command("menu"))
@dp.message(CommandStart())
async def cmd_menu(message: Message):
    text = (
        "🏛 <b>Канцелярия премьер-министра Израиля</b>\n\n"
        "Оперативный пульт коалиции активирован. Нажмите директиву или введите задачу в чат:"
    )
    await message.reply(text, reply_markup=get_main_keyboard(), parse_mode="HTML")

@dp.message(Command("dossier"))
async def cmd_dossier(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name)
    bloc = await get_user_bloc(user["bloc_id"])
    bloc_title = f"{bloc['name']} (Куратор: {bloc['sponsor']})" if bloc else "Внефракционный одиночка"
    
    now_iso = datetime.now().isoformat()
    dome_status = "Не активен"
    if user["immune_until"] and user["immune_until"] > now_iso:
        dome_status = "🛡 Активен"
    bunker_status = "Не активен"
    if user["bunker_until"] and user["bunker_until"] > now_iso:
        bunker_status = "🚪 В укрытии (Иммунитет к ШАБАК)"

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
            print(f"Ошибка Gemini API: {e}")

    card = (
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
    await message.reply(card, parse_mode="HTML")

# --- ЭКОНОМИКА: ГЕШЕФТ, ВЗЯТКИ, ПЕРЕВОДЫ ---
@dp.message(Command("gesheft"))
async def cmd_gesheft(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name)
    now = datetime.now()
    
    if user["last_gesheft"]:
        last = datetime.fromisoformat(user["last_gesheft"])
        if now - last < timedelta(hours=4):
            rem = timedelta(hours=4) - (now - last)
            mins = int(rem.total_seconds() // 60)
            await message.reply(f"⏳ Казначейство опечатано. Следующий распил бюджета доступен через {mins} мин.")
            return

    reward = random.randint(35, 75) + (user["rank"] * 10)
    await update_user(message.from_user.id, shekels=user["shekels"] + reward, last_gesheft=now.isoformat())
    await message.reply(
        f"💰 <b>Гешефт успешно оформлен!</b>\n"
        f"Списано по статье: «Закупка противодиверсионных фиников для канцелярии».\n"
        f"Вам начислено: <b>+{reward} ₪</b>."
    )

@dp.message(Command("bribe"))
async def cmd_bribe(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name)
    if is_shabbat():
        await message.reply("🕯 <b>Шаббат шалом!</b> Брать и давать взятки в святой день строжайше запрещено раввинатом.")
        return
    if user["shekels"] < 50:
        await message.reply("🍾 <b>Канцелярия премьера:</b> В вашем бюджете нет 50 ₪. Коалиция не работает под честное слово.")
        return
    
    new_loyalty = min(100, user["loyalty"] + 15)
    await update_user(message.from_user.id, loyalty=new_loyalty, shekels=user["shekels"] - 50)
    await message.reply(
        "🍾 <b>Поставка дипломатической почтой оформлена.</b>\n"
        "Ящик кубинских сигар доставлен в резиденцию в Кейсарии. Ваша лояльность выросла на <b>+15%</b>."
    )

@dp.message(Command("pay"))
async def cmd_pay(message: Message, command: CommandObject):
    if not command.args or not message.reply_to_message:
        await message.reply("Использование: ответьте на сообщение получателя командой <code>/pay 50</code>")
        return
    try:
        amount = int(command.args.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.reply("Укажите корректную сумму шекелей цифрами.")
        return

    sender = await get_or_create_user(message.from_user.id, message.from_user.full_name)
    if sender["shekels"] < amount:
        await message.reply("У вас недостаточно шекелей для осуществления финансовой проводки.")
        return

    target_user = message.reply_to_message.from_user
    receiver = await get_or_create_user(target_user.id, target_user.full_name)

    tax = int(amount * 0.10)
    net_amount = amount - tax
    await update_user(sender["user_id"], shekels=sender["shekels"] - amount)
    await update_user(receiver["user_id"], shekels=receiver["shekels"] + net_amount)

    await message.reply(
        f"💸 <b>Откат переведён!</b>\n"
        f"Отправитель: {sender['full_name']}\n"
        f"Получатель: {receiver['full_name']}\n"
        f"Сумма: <b>{net_amount} ₪</b> (10% госпошлины ушли в фонд Ликуда)."
    )

# --- РАНГОВЫЕ ДЕЙСТВИЯ: ПРОТЕСТ, ПОБОРЫ, АУДИТ, КОМПРОМАТ, ВЕТО ---
@dp.message(Command("protest"))
async def cmd_protest(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name)
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
        await message.reply(f"📢 <b>Митинг устоял!</b> Зарубежный фонд выделил вам грант за защиту демократии: <b>+{grant} ₪</b> (лояльность -5%).")

@dp.message(Command("shakedown"))
async def cmd_shakedown(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name)
    if user["rank"] < 1:
        await message.reply("Вы ещё не доросли до права трясти ларьки. Достигните хотя бы Ранга 1.")
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение жертвы реплаем.")
        return
    
    target_obj = message.reply_to_message.from_user
    victim = await get_or_create_user(target_obj.id, target_obj.full_name)
    
    if victim["rank"] >= 1:
        await message.reply("Коллег по муниципальному аппарату трясти нельзя — жалуйтесь выше.")
        return
    
    steal = min(victim["shekels"], 25)
    await update_user(victim["user_id"], shekels=victim["shekels"] - steal)
    await update_user(user["user_id"], shekels=user["shekels"] + steal)
    await message.reply(
        f"🧆 <b>Инспекция по кошерности завершена!</b>\n"
        f"Инспектор {user['full_name']} оштрафовал {victim['full_name']} на <b>{steal} ₪</b> за подозрительную тхину."
    )

@dp.message(Command("audit"))
async def cmd_audit(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name)
    if user["rank"] < 3:
        await message.reply("Назначать налоговые проверки разрешено только с Ранга 3 (Глава комиссии).")
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение цели командой /audit.")
        return

    target_obj = message.reply_to_message.from_user
    victim = await get_or_create_user(target_obj.id, target_obj.full_name)
    
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
        f"Списано: <b>{fine} ₪</b> в фонд комиссии депутата {user['full_name']}."
    )

@dp.message(Command("kompromat"))
async def cmd_kompromat(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name)
    if user["rank"] < 4:
        await message.reply("Аппаратные войны компроматов доступны только министрам кабинета (Ранг 4+).")
        return
    if not message.reply_to_message:
        await message.reply("Укажите реплаем соперника для слива компромата.")
        return

    target_obj = message.reply_to_message.from_user
    victim = await get_or_create_user(target_obj.id, target_obj.full_name)

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
            f"На министра {victim['full_name']} заведено «Дело 5000». Лояльность сбита на <b>-30%</b>, позиции в коалиции пошатнулись!"
        )
    else:
        await apply_chat_mute(
            message,
            user["user_id"],
            20,
            "Клевета на коллег по коалиции и фальсификация документов канцелярии."
        )

@dp.message(Command("martial_law"))
async def cmd_martial_law(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.full_name)
    if user["rank"] < 4:
        await message.reply("Объявлять чрезвычайное положение могут только члены узкого кабинета (Ранг 4+).")
        return
    unti
