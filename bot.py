"""
🦷 OrthoBot — Telegram-бот для пациентов врача-ортодонта

Возможности:
  • Ответы на частые вопросы (брекеты, элайнеры, боль, питание)
  • Памятки по уходу
  • «Отправить фото врачу» — фото проблемы пересылается доктору,
    доктор может ответить пациенту прямо из своего чата (через reply)
  • Напоминания о визитах (бот сам напомнит пациенту за день до приёма)

Команды врача (работают только из чата врача):
  /patients            — список пациентов
  /setvisit ID ДД.ММ.ГГГГ ЧЧ:ММ — назначить визит пациенту
  ответ (reply) на пересланное фото/вопрос — уходит пациенту

Запуск:
  1. Получите токен у @BotFather
  2. export BOT_TOKEN="ваш_токен"
  3. export DOCTOR_CHAT_ID="ваш_id"   (узнать свой id: @userinfobot)
  4. python bot.py
"""

import logging
import os
import sqlite3
from datetime import datetime, time, timedelta

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─────────────────────────── Настройки ───────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
# ID чата врача. Можно переопределить переменной окружения DOCTOR_CHAT_ID,
# а если её нет — используется значение ниже. Это не секрет, в отличие от токена.
DOCTOR_CHAT_ID = int(os.environ.get("DOCTOR_CHAT_ID", "659762090"))
DB_PATH = "ortho_bot.db"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("ortho_bot")

# ─────────────────────────── База данных ───────────────────────────


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS patients (
                   chat_id     INTEGER PRIMARY KEY,
                   name        TEXT,
                   username    TEXT,
                   next_visit  TEXT,          -- ISO datetime или NULL
                   reminded    INTEGER DEFAULT 0,
                   created_at  TEXT
               )"""
        )
        # миграция: колонка "брекеты/элайнеры" (для старых баз)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(patients)")]
        if "appliance" not in cols:
            conn.execute("ALTER TABLE patients ADD COLUMN appliance TEXT")
        # связь "сообщение в чате врача -> пациент", чтобы reply уходил адресату
        conn.execute(
            """CREATE TABLE IF NOT EXISTS relay (
                   doctor_msg_id INTEGER PRIMARY KEY,
                   patient_chat_id INTEGER
               )"""
        )


def upsert_patient(chat_id: int, name: str, username: str | None) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO patients (chat_id, name, username, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name,
                                                  username=excluded.username""",
            (chat_id, name, username, datetime.now().isoformat()),
        )


# ─────────────────────────── Контент ───────────────────────────

FAQ: dict[str, tuple[str, str]] = {
    "pain": (
        "😖 Болят зубы после активации",
        "Это нормально! После установки брекетов, смены дуги или плановой "
        "коррекции могут быть болезненные ощущения 3–5 дней — зубы двигаются, "
        "всё по плану 👍🏼\n\n"
        "Что поможет:\n"
        "• Мягкая пища (йогурты, пюре, супы)\n"
        "• Обезболивающее, которое вы обычно принимаете (по инструкции)\n"
        "• Ортодонтический воск, если что-то натирает\n\n"
        "⚠️ Если боль острая, не проходит больше 5–7 дней или что-то "
        "воткнулось в щёку — отправьте фото через меню, я посмотрю.",
    ),
    "bracket_off": (
        "💥 Отклеился брекет / замок",
        "Без паники — это не срочно и случается у многих.\n\n"
        "Что делать:\n"
        "1. Если брекет держится на дуге — оставьте как есть\n"
        "2. Если совсем отвалился — сохраните его и принесите на приём, подклеим\n"
        "3. Если конец дуги колет щёку — прикройте ортодонтическим воском\n\n"
        "📷 Отправьте фото через меню «Отправить фото врачу» — скажу, "
        "нужно ли приходить раньше планового визита.",
    ),
    "food": (
        "🍎 Что нельзя есть",
        "Ношение брекетов — отнюдь не повод отказываться от любимой еды, "
        "но всё же следует придерживаться некоторых рекомендаций.\n\n"
        "🦷 С брекетами исключаем всё, что может отклеить замки или погнуть дугу:\n\n"
        "❌ Твёрдое: орехи, сухари, леденцы, целые яблоки или морковь — "
        "продукты, требующие откусывания, режьте!\n"
        "❌ Липкое: ириски, жвачка, нуга, карамель, халва\n"
        "❌ Вязкое: чипсы «начос», попкорн (не дожаренные зёрна!)\n\n"
        "✅ Можно: почти всё остальное, если резать на кусочки и жевать аккуратно.\n\n"
        "С элайнерами проще: снимаете капы — едите что хотите. "
        "Но в капах можно пить только воду!",
    ),
    "hygiene": (
        "🪥 Как чистить зубы с брекетами",
        "Минимум дважды в день, а лучше после каждой еды:\n\n"
        "1. Ортодонтическая щётка с V-вырезом — чистим над и под брекетами\n"
        "2. Монопучковая щётка — вокруг каждого замка, прочищаем расстояние "
        "между брекетом и десной 💪🏼\n"
        "3. Ёршики — под дугой между брекетами\n"
        "4. Ирригатор — вечером, лучший друг брекетоносца\n"
        "5. Зубная нить\n\n"
        "Плохая гигиена = кариес. Не хотим этого! 🙏",
    ),
    "aligners": (
        "😬 Правила ношения элайнеров",
        "• Носим 20–22 часа в сутки — снимаем только поесть и почистить зубы\n"
        "• Меняем капы строго по графику, который назначил врач (~каждые 14 дней)\n"
        "• В капах пьём только воду (чай/кофе окрашивают элайнеры — они теряют "
        "прозрачность и могут деформироваться)\n"
        "• Храним в контейнере — не в салфетке! (главная причина потерь 😅)\n"
        "• Моем капы прохладной водой, НЕ горячей; используем растворы или "
        "таблетки для очистки элайнеров\n\n"
        "Потеряли капу? Наденьте предыдущую и отправьте мне сообщение.",
    ),
    "duration": (
        "⏳ Сколько длится лечение",
        "В среднем 1,5–2 года, но всё индивидуально:\n\n"
        "• Простые случаи — от 8–12 месяцев\n"
        "• Сложные — до 2,5–3 лет\n\n"
        "Что ускоряет лечение:\n"
        "1. Соблюдение рекомендаций\n"
        "2. Являться на все приёмы вовремя\n"
        "3. Бережно относиться к брекетам и элайнерам\n"
        "4. Хорошая гигиена полости рта\n\n"
        "После снятия брекетов носим ретейнеры — это обязательная часть, "
        "иначе зубы вернутся обратно!",
    ),
    "pro_hygiene": (
        "✨ Профгигиена с брекетами — можно?",
        "Не только можно — нужно! Профессиональную гигиену полости рта "
        "проходим 1 раз в 4–6 месяцев.",
    ),
    "damage": (
        "🤔 Брекеты портят зубы?",
        "Нет, брекеты не портят зубы. А вот плохая гигиена полости рта "
        "может приводить к появлению кариеса вокруг брекетов — поэтому "
        "тщательная чистка так важна.",
    ),
    "install_pain": (
        "😮 Больно ли ставить брекеты?",
        "Установка брекетов — это безболезненная процедура: брекет "
        "приклеивается к зубу на специальный материал и засвечивается лампой.",
    ),
    "one_jaw": (
        "☝️ Можно ли брекеты на одну челюсть?",
        "Иногда да, но это индивидуально. Для коррекции прикуса важно "
        "перемещать зубы и на верхней, и на нижней челюсти. Окончательно "
        "ответить на этот вопрос можно после консультации и проведения "
        "диагностики.",
    ),
    "frequency": (
        "📆 Как часто приходить на приём?",
        "В среднем 1 раз в 6–8 недель. Во время визитов ортодонт "
        "контролирует перемещение зубов и проводит коррекции.",
    ),
    "sport": (
        "⚽ Можно ли заниматься спортом?",
        "Да! При контактных видах спорта рекомендовано использовать защитную "
        "спортивную капу, чтобы снизить риск травмы губ, щёк, зубов и "
        "отклеивания брекетов.",
    ),
    "kids": (
        "🧒 Когда вести ребёнка к ортодонту?",
        "Не нужно ждать, пока сменятся все зубы! Первый визит ребёнка к "
        "ортодонту должен быть в момент начала смены зубов — в 5–6,5 лет.\n\n"
        "Формирование прикуса и положения зубов должно происходить под "
        "контролем ортодонта — это позволяет создать гармоничное строение "
        "лица и устранить скученность зубов в раннем возрасте.",
    ),
    "alternatives": (
        "💡 Есть ли альтернатива брекетам?",
        "Да — элайнеры: прозрачные индивидуальные капы, которые постепенно "
        "выравнивают зубы. Подходит ли вам этот вариант, определяется на "
        "консультации.",
    ),
}

MEMO_TEXT = (
    "📋 <b>Памятка после установки брекетов</b>\n\n"
    "<b>Первые дни:</b>\n"
    "• Дискомфорт и ноющая боль 3–5 дней — норма\n"
    "• Ешьте мягкую пищу\n"
    "• Натирает — используйте ортодонтический воск\n\n"
    "<b>Всегда:</b>\n"
    "• Чистка зубов после каждой еды\n"
    "• Ёршики и ирригатор — ежедневно\n"
    "• Режем твёрдую еду на кусочки\n"
    "• Никаких ирисок, жвачки и орехов\n\n"
    "<b>Срочно свяжитесь с врачом, если:</b>\n"
    "• Дуга сильно колет и воск не помогает\n"
    "• Проглотили элемент конструкции\n"
    "• Острая боль дольше недели\n\n"
    "Вопросы — прямо здесь в боте 💬"
)

# ─── Списки покупок (ссылки на Озон) ───
# Сейчас это ссылки на поиск — они не протухают.
# Можно заменить на ссылки конкретных проверенных товаров:
# просто вставьте URL товара вместо поисковой ссылки.

def ozon(query: str) -> str:
    from urllib.parse import quote_plus
    return f"https://www.ozon.ru/search/?text={quote_plus(query)}"


SHOP_BRACES: list[tuple[str, str]] = [
    ("Ортодонтическая щётка (V-вырез)", ozon("ортодонтическая зубная щетка V-образная")),
    ("Монопучковая щётка", ozon("монопучковая зубная щетка")),
    ("Ёршики межзубные для брекетов", ozon("ершики для брекетов межзубные")),
    ("Ортодонтический воск", ozon("ортодонтический воск для брекетов")),
    ("Ирригатор", ozon("ирригатор для полости рта")),
    ("Ополаскиватель с фтором", ozon("ополаскиватель для рта с фторидом")),
]

SHOP_ALIGNERS: list[tuple[str, str]] = [
    ("Контейнер для элайнеров", ozon("контейнер для элайнеров кейс")),
    ("Таблетки для очистки капп", ozon("таблетки для очистки элайнеров капп")),
    ("Зубная щётка мягкая", ozon("зубная щетка мягкая soft")),
    ("Зубная нить", ozon("зубная нить флосс")),
    ("Ирригатор", ozon("ирригатор для полости рта")),
    ("Дорожный набор гигиены", ozon("дорожный набор зубная щетка паста")),
]


def shop_text(appliance: str) -> str:
    items = SHOP_BRACES if appliance == "braces" else SHOP_ALIGNERS
    title = "брекетов" if appliance == "braces" else "элайнеров"
    lines = [f"🛒 <b>Что купить для ухода — набор для {title}:</b>\n"]
    for name, url in items:
        lines.append(f"• <a href='{url}'>{name}</a>")
    lines.append(
        "\n💡 Ссылки ведут на подборки Озона. Если сомневаетесь в выборе "
        "конкретной модели — спросите меня, передам вопрос врачу."
    )
    return "\n".join(lines)


APPLIANCE_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("😁 Брекеты", callback_data="appl:braces")],
        [InlineKeyboardButton("😬 Элайнеры", callback_data="appl:aligners")],
    ]
)

MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["❓ Частые вопросы", "📋 Памятка"],
        ["🛒 Что купить", "📅 Мой следующий визит"],
        ["📷 Отправить фото врачу"],
    ],
    resize_keyboard=True,
)

# ─────────────────────────── Хэндлеры пациента ───────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    upsert_patient(update.effective_chat.id, user.full_name, user.username)
    await update.message.reply_text(
        f"Здравствуйте, {user.first_name}! 🦷\n\n"
        "Я бот-помощник вашего ортодонта. Могу:\n"
        "• ответить на частые вопросы\n"
        "• подсказать, что купить для ухода за полостью рта\n"
        "• передать врачу фото, если вас что-то беспокоит\n"
        "• напомнить о визите",
        reply_markup=MENU_KEYBOARD,
    )
    await update.message.reply_text(
        "Что вам установили?", reply_markup=APPLIANCE_KEYBOARD
    )


async def appliance_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пациент выбрал брекеты/элайнеры -> сохраняем и шлём список покупок."""
    query = update.callback_query
    await query.answer()
    appliance = query.data.split(":", 1)[1]  # braces | aligners
    with db() as conn:
        conn.execute(
            "UPDATE patients SET appliance=? WHERE chat_id=?",
            (appliance, query.message.chat_id),
        )
    label = "Брекеты 😁" if appliance == "braces" else "Элайнеры 😬"
    await query.edit_message_text(f"Отлично, записал: {label}")
    await query.message.reply_text(
        shop_text(appliance),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def show_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка меню «Что купить» — с учётом сохранённого выбора."""
    with db() as conn:
        row = conn.execute(
            "SELECT appliance FROM patients WHERE chat_id=?",
            (update.effective_chat.id,),
        ).fetchone()
    if row and row["appliance"]:
        await update.message.reply_text(
            shop_text(row["appliance"]),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text(
            "Сначала подскажите — что вам установили?",
            reply_markup=APPLIANCE_KEYBOARD,
        )


async def show_faq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    buttons = [
        [InlineKeyboardButton(title, callback_data=f"faq:{key}")]
        for key, (title, _) in FAQ.items()
    ]
    await update.message.reply_text(
        "Выберите вопрос:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def faq_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]
    title, text = FAQ[key]
    back = InlineKeyboardMarkup(
        [[InlineKeyboardButton("← К списку вопросов", callback_data="faq_list")]]
    )
    await query.edit_message_text(f"<b>{title}</b>\n\n{text}",
                                  parse_mode="HTML", reply_markup=back)


async def faq_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    buttons = [
        [InlineKeyboardButton(title, callback_data=f"faq:{key}")]
        for key, (title, _) in FAQ.items()
    ]
    await query.edit_message_text(
        "Выберите вопрос:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_memo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(MEMO_TEXT, parse_mode="HTML")


async def ask_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting_photo"] = True
    await update.message.reply_text(
        "Пришлите фото проблемы (можно с подписью — опишите, что беспокоит). "
        "Я передам его врачу, и вам ответят здесь же. 📷"
    )


async def show_visit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with db() as conn:
        row = conn.execute(
            "SELECT next_visit FROM patients WHERE chat_id=?",
            (update.effective_chat.id,),
        ).fetchone()
    if row and row["next_visit"]:
        dt = datetime.fromisoformat(row["next_visit"])
        await update.message.reply_text(
            f"📅 Ваш следующий визит: <b>{dt.strftime('%d.%m.%Y в %H:%M')}</b>\n\n"
            "Я напомню вам за день до приёма.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "Пока визит не назначен. Как только врач запишет вас — "
            "дата появится здесь, и я пришлю напоминание. 🙂"
        )


async def relay_to_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фото или текстовый вопрос пациента -> в чат врача."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    upsert_patient(chat_id, user.full_name, user.username)

    header = (
        f"📨 От пациента: <b>{user.full_name}</b>"
        + (f" (@{user.username})" if user.username else "")
        + f"\nID: <code>{chat_id}</code>\n"
        "↩️ Ответьте (reply) на это сообщение — ответ уйдёт пациенту."
    )

    if update.message.photo:
        caption = update.message.caption or ""
        sent = await context.bot.send_photo(
            DOCTOR_CHAT_ID,
            update.message.photo[-1].file_id,
            caption=f"{header}\n\n{caption}",
            parse_mode="HTML",
        )
        context.user_data["awaiting_photo"] = False
        await update.message.reply_text(
            "Фото передано врачу ✅ Ответ придёт сюда, обычно в течение дня."
        )
    else:
        sent = await context.bot.send_message(
            DOCTOR_CHAT_ID,
            f"{header}\n\n💬 {update.message.text}",
            parse_mode="HTML",
        )
        await update.message.reply_text(
            "Передал ваш вопрос врачу ✅ Ответ придёт сюда."
        )

    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO relay VALUES (?, ?)",
            (sent.message_id, chat_id),
        )


async def patient_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Роутер текстовых сообщений пациента (кнопки меню / свободный вопрос)."""
    text = update.message.text
    if text == "❓ Частые вопросы":
        await show_faq(update, context)
    elif text == "📋 Памятка":
        await show_memo(update, context)
    elif text == "🛒 Что купить":
        await show_shop(update, context)
    elif text == "📷 Отправить фото врачу":
        await ask_photo(update, context)
    elif text == "📅 Мой следующий визит":
        await show_visit(update, context)
    else:
        # свободный вопрос — пересылаем врачу
        await relay_to_doctor(update, context)


# ─────────────────────────── Хэндлеры врача ───────────────────────────


async def doctor_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Врач отвечает reply-ем на пересланное сообщение -> уходит пациенту."""
    replied = update.message.reply_to_message
    if not replied:
        return
    with db() as conn:
        row = conn.execute(
            "SELECT patient_chat_id FROM relay WHERE doctor_msg_id=?",
            (replied.message_id,),
        ).fetchone()
    if not row:
        await update.message.reply_text(
            "Не нашёл, кому переслать 🤔 Отвечайте reply-ем на сообщение пациента."
        )
        return

    patient_id = row["patient_chat_id"]
    prefix = "👩‍⚕️ <b>Ответ врача:</b>\n\n"
    if update.message.photo:
        await context.bot.send_photo(
            patient_id,
            update.message.photo[-1].file_id,
            caption=prefix + (update.message.caption or ""),
            parse_mode="HTML",
        )
    elif update.message.text:
        await context.bot.send_message(
            patient_id, prefix + update.message.text, parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            "Такой тип сообщения переслать не могу — "
            "ответьте, пожалуйста, текстом или фото 🙏"
        )
        return
    await update.message.reply_text("Отправлено пациенту ✅")


async def doctor_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Врач написал без reply — подсказываем, как отвечать пациентам."""
    await update.message.reply_text(
        "Чтобы ответить пациенту, сделайте «Ответ» (reply) на его сообщение: "
        "свайп влево по сообщению пациента → написать текст → отправить.\n\n"
        "Команды:\n"
        "/patients — список пациентов\n"
        "/setvisit ID ДД.ММ.ГГГГ ЧЧ:ММ — назначить визит\n"
        "/broadcast текст — рассылка всем пациентам\n"
        "/backup — получить копию базы данных"
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/broadcast текст — рассылка всем пациентам."""
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text(
            "Напишите текст после команды:\n"
            "/broadcast Уважаемые пациенты! С 1 по 14 августа клиника не работает."
        )
        return
    with db() as conn:
        rows = conn.execute("SELECT chat_id FROM patients").fetchall()
    ok, fail = 0, 0
    for r in rows:
        try:
            await context.bot.send_message(
                r["chat_id"],
                f"📢 <b>Сообщение от врача:</b>\n\n{text}",
                parse_mode="HTML",
            )
            ok += 1
        except Exception:
            fail += 1  # пациент заблокировал бота
    await update.message.reply_text(
        f"Рассылка завершена: доставлено {ok}, не доставлено {fail}."
    )


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backup — прислать файл базы данных врачу."""
    await send_backup(context)
    await update.message.reply_text("Готово 👆 Сохраните файл на всякий случай.")


async def send_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        with open(DB_PATH, "rb") as f:
            await context.bot.send_document(
                DOCTOR_CHAT_ID,
                f,
                filename=f"ortho_backup_{datetime.now().strftime('%Y-%m-%d')}.db",
                caption="💾 Резервная копия базы бота (пациенты, визиты). "
                "Просто сохраните этот файл — если что-то случится с ботом, "
                "по нему всё восстановим.",
            )
    except FileNotFoundError:
        log.warning("Файл базы для бэкапа не найден")


async def weekly_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_backup(context)


async def visit_response(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пациент нажал «Приду» / «Нужно перенести» под напоминанием."""
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    user = query.from_user
    if choice == "yes":
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("Отлично, ждём вас! 👍")
        await context.bot.send_message(
            DOCTOR_CHAT_ID,
            f"✅ {user.full_name} подтвердил(а) завтрашний визит.",
        )
    else:
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(
            "Понял, передал врачу — с вами свяжутся, чтобы подобрать новое время."
        )
        with db() as conn:
            conn.execute(
                "UPDATE patients SET next_visit=NULL, reminded=0 WHERE chat_id=?",
                (query.message.chat_id,),
            )
        await context.bot.send_message(
            DOCTOR_CHAT_ID,
            f"⚠️ <b>{user.full_name}</b> просит перенести завтрашний визит!\n"
            f"ID: <code>{query.message.chat_id}</code> — когда договоритесь, "
            "назначьте новую дату через /setvisit.",
            parse_mode="HTML",
        )


async def cmd_patients(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with db() as conn:
        rows = conn.execute(
            "SELECT chat_id, name, username, next_visit FROM patients ORDER BY name"
        ).fetchall()
    if not rows:
        await update.message.reply_text("Пациентов пока нет.")
        return
    lines = ["👥 <b>Пациенты:</b>\n"]
    for r in rows:
        visit = (
            datetime.fromisoformat(r["next_visit"]).strftime("%d.%m %H:%M")
            if r["next_visit"]
            else "—"
        )
        uname = f" @{r['username']}" if r["username"] else ""
        lines.append(f"• {r['name']}{uname}\n  ID: <code>{r['chat_id']}</code> | визит: {visit}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_setvisit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setvisit CHAT_ID ДД.ММ.ГГГГ ЧЧ:ММ"""
    try:
        chat_id = int(context.args[0])
        dt = datetime.strptime(f"{context.args[1]} {context.args[2]}", "%d.%m.%Y %H:%M")
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Формат: /setvisit ID ДД.ММ.ГГГГ ЧЧ:ММ\n"
            "Пример: /setvisit 123456789 25.07.2026 14:30\n"
            "ID пациента — в списке /patients"
        )
        return
    with db() as conn:
        cur = conn.execute(
            "UPDATE patients SET next_visit=?, reminded=0 WHERE chat_id=?",
            (dt.isoformat(), chat_id),
        )
    if cur.rowcount == 0:
        await update.message.reply_text("Пациент с таким ID не найден.")
        return
    await update.message.reply_text(
        f"✅ Визит назначен на {dt.strftime('%d.%m.%Y %H:%M')}. "
        "Пациент получит напоминание за день."
    )
    await context.bot.send_message(
        chat_id,
        f"📅 Вам назначен приём: <b>{dt.strftime('%d.%m.%Y в %H:%M')}</b>\n"
        "Я напомню за день до визита!",
        parse_mode="HTML",
    )


# ─────────────────────────── Напоминания ───────────────────────────


async def send_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневная задача: напомнить всем, у кого визит завтра."""
    now = datetime.now()
    tomorrow_end = now + timedelta(hours=36)
    with db() as conn:
        rows = conn.execute(
            "SELECT chat_id, next_visit FROM patients "
            "WHERE next_visit IS NOT NULL AND reminded=0"
        ).fetchall()
        for r in rows:
            visit = datetime.fromisoformat(r["next_visit"])
            if now < visit <= tomorrow_end:
                try:
                    await context.bot.send_message(
                        r["chat_id"],
                        f"🔔 Напоминание: завтра у вас приём у ортодонта — "
                        f"<b>{visit.strftime('%d.%m в %H:%M')}</b>\n\n"
                        "Не забудьте почистить зубы перед визитом 😉",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(
                            [[
                                InlineKeyboardButton("✅ Приду", callback_data="visit:yes"),
                                InlineKeyboardButton("❌ Нужно перенести", callback_data="visit:no"),
                            ]]
                        ),
                    )
                    conn.execute(
                        "UPDATE patients SET reminded=1 WHERE chat_id=?",
                        (r["chat_id"],),
                    )
                except Exception as e:  # пациент заблокировал бота и т.п.
                    log.warning("Не удалось напомнить %s: %s", r["chat_id"], e)


# ─────────────────────────── Запуск ───────────────────────────


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "Задайте переменную окружения BOT_TOKEN (токен — у @BotFather)."
        )
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    doctor = filters.Chat(DOCTOR_CHAT_ID)
    patient = ~doctor

    # Врач
    app.add_handler(CommandHandler("patients", cmd_patients, filters=doctor))
    app.add_handler(CommandHandler("setvisit", cmd_setvisit, filters=doctor))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast, filters=doctor))
    app.add_handler(CommandHandler("backup", cmd_backup, filters=doctor))
    app.add_handler(CallbackQueryHandler(visit_response, pattern=r"^visit:"))
    app.add_handler(MessageHandler(doctor & filters.REPLY, doctor_reply))
    app.add_handler(MessageHandler(doctor & ~filters.COMMAND, doctor_hint))

    # Пациент
    app.add_handler(CommandHandler("start", cmd_start, filters=patient))
    app.add_handler(CallbackQueryHandler(appliance_chosen, pattern=r"^appl:"))
    app.add_handler(CallbackQueryHandler(faq_answer, pattern=r"^faq:"))
    app.add_handler(CallbackQueryHandler(faq_back, pattern=r"^faq_list$"))
    app.add_handler(MessageHandler(patient & filters.PHOTO, relay_to_doctor))
    app.add_handler(MessageHandler(patient & filters.TEXT & ~filters.COMMAND, patient_text))

    # Ежечасная проверка напоминаний (простая и надёжная схема)
    app.job_queue.run_repeating(send_reminders, interval=3600, first=10)
    # Еженедельный бэкап базы врачу в Telegram (раз в 7 дней)
    app.job_queue.run_repeating(weekly_backup, interval=7 * 24 * 3600, first=60)

    log.info("Бот запущен 🦷")
    app.run_polling()


if __name__ == "__main__":
    main()
