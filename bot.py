import os
import sys
import json
import random
import asyncio
from datetime import datetime, timezone, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
import aiosqlite
from google import genai
from google.genai import types as genai_types

# ==========================================
# 1. КОНФИГУРАЦИЯ И ОКРУЖЕНИЕ
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID", "")
PORT = int(os.getenv("PORT", 8080))

if not BOT_TOKEN or not GEMINI_API_KEY:
    print("ОШИБКА: Задайте переменные BOT_TOKEN и GEMINI_API_KEY!")
    sys.exit(1)

DB_PATH = "bibi.db"

# ==========================================
# 2. СИСТЕМНЫЙ ПРОМПТ
# ==========================================
SYSTEM_PROMPT = """
Ты — Биньямин Нетаньяху (Биби), премьер-министр Израиля и теневой авторитарный владыка этого закрытого Telegram-чата.

ТВОЙ ОБРАЗ И ПСИХОЛОГИЯ:
- Ты несменяемый патриарх, гроссмейстер коалиций, прагматичный циник и архитектор концепции «осажденной крепости».
- Ты никогда не извиняешься, не рефлексируешь и не признаешь своих ошибок. Любой провал — это халатность разведки, предательство генералов или происки оппозиции. Любой успех чата — результат твоего гениального стратегического руководства.
- Бытовую рутину участников (игры, споры, лень, дедлайны) ты мгновенно возводишь в ранг геополитической катастрофы, экзистенциальной угрозы или иранского заговора.
- Ты говоришь тяжело, веско, уверенно, без суеты. Избегай легкомысленного молодежного сленга, отвечай рублеными, авторитетными фразами с легким оттенком сарказма и превосходства.

ОТНОШЕНИЕ К УЧАСТНИКАМ:
- За поддержку, согласие и здравые идеи ты не благодаришь просто так, а высокомерно одобряешь их «редкую моральную ясность», записываешь людей в стратегические партнеры, раздаешь министерские портфели и обещаешь защиту коалиции.
- За нытье, слабость или сомнения ты объявляешь санкции, вычерчиваешь «красные линии» и грозишь превентивным демонтажем инфраструктуры оппонента.

РЕАКЦИЯ НА РЕАЛЬНУЮ ПОЛИТИКУ:
- Безопасность и война: Только доктрина «полной победы» (Absolute Victory). Никаких уступок, слабости или компромиссов. Террористическая инфраструктура должна быть ликвидирована под корень.
- Палестинский вопрос: Категорическое «нет» созданию палестинского государства. Израиль обязан сохранять полный контроль безопасности от реки до моря.
- Международное давление (ООН, МУС, западные СМИ): Полное презрение. Это лицемерие, двойные стандарты и историческая амнезия. Никто не имеет права читать лекции о морали стране, борющейся за выживание.
- Союзники: Мы ценим партнерство, но решения о своем существовании принимаем исключительно самостоятельно.

ТЕХНИЧЕСКИЕ ПРАВИЛА:
- Никаких морализаторских дисклеймеров вроде «Как ИИ я...». Ты всегда остаешься в роли на 100%.
- Ответы должны быть компактными (1–4 предложения), емкими и хлесткими.
- Используй термины: «коалиция», «красная черта», «вотум недоверия», «соглашения Авраама», «полная победа», «превентивный ответ».
"""

# ==========================================
# 3. БАЗА ДАННЫХ (SQLite)
# ==========================================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                loyalty_score INTEGER DEFAULT 50,
                shekels INTEGER DEFAULT 100,
                strikes INTEGER DEFAULT 0,
                is_immune BOOLEAN DEFAULT 0,
                immune_until TEXT,
                minister_title TEXT DEFAULT 'Рядовой гражданин',
                dossier_notes TEXT DEFAULT '[]'
            )
        """)
        await db.commit()

async def get_or_create_user(user: types.User):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user.id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
        
        await db.execute("""
            INSERT INTO users (user_id, username, full_name)
            VALUES (?, ?, ?)
        """, (user.id, user.username or "", user.full_name or ""))
        await db.commit()
        
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user.id,)) as cursor:
            return dict(await cursor.fetchone())

async def update_user(user_id: int, **kwargs):
    fields = [f"{k} = ?" for k in kwargs.keys()]
    values = list(kwargs.values()) + [user_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?", values)
        await db.commit()

# ==========================================
# 4. ИНТЕГРАЦИЯ GEMINI API
# ==========================================
ai_client = genai.Client(api_key=GEMINI_API_KEY)
safety_settings = [
    genai_types.SafetySetting(category=genai_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=genai_types.HarmBlockThreshold.BLOCK_NONE),
    genai_types.SafetySetting(category=genai_types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=genai_types.HarmBlockThreshold.BLOCK_NONE),
    genai_types.SafetySetting(category=genai_types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=genai_types.HarmBlockThreshold.BLOCK_NONE),
    genai_types.SafetySetting(category=genai_types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=genai_types.HarmBlockThreshold.BLOCK_NONE),
]

async def ask_bibi(prompt: str, user_context: str = "") -> str:
    full_prompt = f"Контекст досье субъекта: {user_context}\nСообщение в чате: {prompt}" if user_context else prompt
    try:
        response = await ai_client.aio.models.generate_content(
            model='gemini-3.8-flash',
            contents=full_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.8,
                safety_settings=safety_settings,
                max_output_tokens=250,
            )
        )
        return response.text or "Кабинет безопасности временно воздерживается от комментариев."
    except Exception as e:
        print(f"Ошибка Gemini API: {e}")
        return "Премьер находится на закрытом совещании в бункере Кирьи. Ожидайте официальное коммюнике."

# ==========================================
# 5. КЛАВИАТУРА И ШАББАТ
# ==========================================
last_spontaneous_reply = datetime.min.replace(tzinfo=timezone.utc)
SPONTANEOUS_KEYWORDS = ["война", "деньги", "суд", "виноват", "план", "играть", "слив", "купить", "телефон", "биби"]

def get_cabinet_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎯 Превентивный удар", callback_data="btn_strike"),
            InlineKeyboardButton(text="📁 Моё досье", callback_data="btn_dossier")
        ],
        [
            InlineKeyboardButton(text="🍾 Занести сигары (50 ₪)", callback_data="btn_bribe"),
            InlineKeyboardButton(text="⚖️ Судебная реформа", callback_data="btn_reform")
        ]
    ])
    return keyboard

def is_shabbat() -> bool:
    now = datetime.now(timezone(timedelta(hours=3)))
    weekday = now.weekday()
    if weekday == 4 and now.hour >= 18:
        return True
    if weekday == 5 and now.hour < 21:
        return True
    return False

# ==========================================
# 6. ХЭНДЛЕРЫ
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start", "menu"))
async def cmd_menu(message: types.Message):
    await message.reply(
        "🏛 **Канцелярия премьер-министра Израиля**\n\n"
        "Оперативный пульт коалиции активирован. Выберите директиву или введите задачу в чат:",
        reply_markup=get_cabinet_keyboard(),
        parse_mode="Markdown"
    )

# Обработка инлайн-кнопок
@dp.callback_query(F.data == "btn_strike")
async def cb_strike(call: types.CallbackQuery):
    user_db = await get_or_create_user(call.from_user)
    roll = random.random()

    if roll < 0.4:
        new_shekels = user_db["shekels"] + 50
        immune_until = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        await update_user(call.from_user.id, shekels=new_shekels, is_immune=1, immune_until=immune_until, minister_title="Стратегический партнер")
        await call.message.reply(f"🎯 @{call.from_user.username or call.from_user.first_name}, превентивный удар успешен! Начислено +50 шекелей и включен «Железный купол» на 6 часов.")
    elif roll < 0.7:
        advice = [
            "Никогда не признавай распад коалиции, пока не подписаны новые соглашения.",
            "В любой непонятной ситуации обвиняй оппозицию и затягивание решений штабом.",
            "Если союзники выражают озабоченность — кивай, но делай то, что нужно ради выживания."
        ]
        await call.message.reply(f"📜 Директива премьера для @{call.from_user.username or call.from_user.first_name}: {random.choice(advice)}")
    else:
        now = datetime.now(timezone.utc)
        if user_db["is_immune"] and user_db.get("immune_until"):
            if datetime.fromisoformat(user_db["immune_until"]) > now:
                await call.message.reply(f"🛡 Превентивный удар отражен «Железным куполом» участника @{call.from_user.username or call.from_user.first_name}!")
                await call.answer()
                return

        new_shekels = max(0, user_db["shekels"] - 30)
        await update_user(call.from_user.id, shekels=new_shekels)
        try:
            until_date = datetime.now(timezone.utc) + timedelta(seconds=60)
            await bot.restrict_chat_member(
                chat_id=call.message.chat.id,
                user_id=call.from_user.id,
                permissions=types.ChatPermissions(can_send_messages=False),
                until_date=until_date
            )
            await call.message.reply(f"⚠️ Вылазка признана провокацией! @{call.from_user.username or call.from_user.first_name} оштрафован на 30 шекелей и получает мут на 60 сек.")
        except TelegramBadRequest:
            await call.message.reply(f"Глубинное государство заблокировало мут (нет прав админа). Но штраф 30 шекелей с @{call.from_user.username or call.from_user.first_name} списан.")
    await call.answer()

@dp.callback_query(F.data == "btn_dossier")
async def cb_dossier(call: types.CallbackQuery):
    u = await get_or_create_user(call.from_user)
    context = f"Титул: {u['minister_title']}, Лояльность: {u['loyalty_score']}/100, Шекели: {u['shekels']}"
    verdict = await ask_bibi("Дай саркастическую характеристику лояльности этого члена коалиции в 1-2 предложениях.", user_context=context)
    text = (
        f"📁 **ДОСЬЕ МОССАДА: {call.from_user.full_name}**\n\n"
        f"• Должность: {u['minister_title']}\n"
        f"• Индекс благонадежности: {u['loyalty_score']}/100\n"
        f"• В казне: {u['shekels']} ₪\n"
        f"• Резюме Биби: {verdict}"
    )
    await call.message.reply(text, parse_mode="Markdown")
    await call.answer()

@dp.callback_query(F.data == "btn_bribe")
async def cb_bribe(call: types.CallbackQuery):
    u = await get_or_create_user(call.from_user)
    if u["shekels"] < 50:
        await call.answer("Недостаточно шекелей в бюджете!", show_alert=True)
        return
    await update_user(call.from_user.id, shekels=u["shekels"] - 50, loyalty_score=min(100, u["loyalty_score"] + 15))
    await call.message.reply(
        f"🍾 @{call.from_user.username or call.from_user.first_name} передал коробку первоклассных сигар. "
        "Улики признаны сфабрикованными левой прессой. Лояльность повышена на +15!"
    )
    await call.answer()

@dp.callback_query(F.data == "btn_reform")
async def cb_reform(call: types.CallbackQuery):
    await call.message.reply(
        "⚖️ В рамках судебной реформы любые вето и протесты оппозиции аннулируются. Решение коалиции окончательное."
    )
    await call.answer()

# Текстовые команды
@dp.message(Command("threat"))
async def cmd_threat(message: types.Message):
    args = message.text.replace("/threat", "").strip() or "Угроза по периметру"
    pct = random.randint(75, 99)
    await message.reply(
        f"🚨 **КРАСНАЯ ЛИНИЯ: {args}**\n\n"
        f"💣 Уровень обогащения угрозы: **{pct}%** (критический порог: 90%)\n"
        f"Решение премьера: промедление недопустимо. Вопрос закрыт.",
        parse_mode="Markdown"
    )

@dp.message(Command("pager"))
async def cmd_pager(message: types.Message):
    if not message.reply_to_message:
        await message.reply("Сделайте реплай на устройство подозрительного контакта.")
        return
    await message.reply_to_message.reply(
        "⚠️ ||Служба безопасности: на вашем пейджере входящее сообщение. Батарея нагревается. Рекомендуется эвакуация.||",
        parse_mode="MarkdownV2"
    )

# Сообщения в чате (реплаи, теги, спонтанные реплики)
@dp.message(F.text)
async def handle_chat_message(message: types.Message):
    global last_spontaneous_reply
    if not message.text:
        return

    if ALLOWED_CHAT_ID and str(message.chat.id) != ALLOWED_CHAT_ID:
        return

    bot_info = await bot.get_me()
    is_direct_reply = message.reply_to_message and message.reply_to_message.from_user.id == bot_info.id
    is_tagged = f"@{bot_info.username}" in message.text

    if is_shabbat():
        if is_direct_reply or is_tagged:
            await message.reply("Премьер-министр соблюдает покой. Правительственная связь отключена до исхода субботы.")
        return

    # ПРЯМОЕ ОБРАЩЕНИЕ (через тег или реплай)
    if is_direct_reply or is_tagged:
        clean_text = message.text.replace(f"@{bot_info.username}", "").strip()
        u = await get_or_create_user(message.from_user)
        context = f"Пользователь: {message.from_user.full_name}, статус: {u['minister_title']}, лояльность: {u['loyalty_score']}"
        reply = await ask_bibi(clean_text, user_context=context)
        await message.reply(reply)
        return

    # Спонтанный триггер (не чаще раза в 25 мин)
    now = datetime.now(timezone.utc)
    text_lower = message.text.lower()
    has_keyword = any(kw in text_lower for kw in SPONTANEOUS_KEYWORDS)

    if has_keyword and (now - last_spontaneous_reply) > timedelta(minutes=25):
        if random.random() < 0.15:
            last_spontaneous_reply = now
            u = await get_or_create_user(message.from_user)
            context = f"Пользователь: {message.from_user.full_name}, лояльность: {u['loyalty_score']}"
            reply = await ask_bibi(message.text, user_context=context)
            await message.reply(reply)

# ==========================================
# 7. СЕРВЕР ДЛЯ RENDER (Health Check)
# ==========================================
async def handle_ping(request):
    return web.Response(text="Bibi is actively protecting the coalition. 200 OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# ==========================================
# 8. ЗАПУСК
# ==========================================
async def main():
    await init_db()
    await start_web_server()
    print("Бот Биби с инлайн-кнопками запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
      
