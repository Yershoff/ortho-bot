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

# Часовой пояс пациентов относительно UTC (3 = Москва).
# Сервер хостинга может жить по UTC, поэтому считаем время сами.
TZ_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "3"))
# Тихие часы: уведомления отправляем только с 10:00 до 20:00 местного времени
QUIET_FROM, QUIET_TO = 10, 20


def now_local() -> datetime:
    from datetime import timezone
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
        hours=TZ_OFFSET_HOURS
    )


def is_quiet_hours() -> bool:
    return not (QUIET_FROM <= now_local().hour < QUIET_TO)

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
        # миграция: график смены элайнеров
        for col, typ in [
            ("alg_start", "TEXT"), ("alg_interval", "INTEGER"),
            ("alg_total", "INTEGER"), ("alg_notified", "TEXT"),
            ("installed_at", "TEXT"), ("followup_stage", "INTEGER DEFAULT 0"),
            # ношение капп: сумма секунд за сегодня, отметка "надето с",
            #   дата, за которую копится счётчик, флаг предупреждения
            ("wear_today", "INTEGER DEFAULT 0"), ("wear_since", "TEXT"),
            ("wear_date", "TEXT"), ("wear_warned", "TEXT"),
            # стадия лечения: active | retainer ; напоминание про эластики
            ("stage", "TEXT DEFAULT 'active'"),
            ("elastics", "INTEGER DEFAULT 0"), ("elastics_date", "TEXT"),
            # фото прогресса: дата последнего запроса селфи
            ("last_progress", "TEXT"),
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE patients ADD COLUMN {col} {typ}")
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

# Базовое меню (для брекетов и по умолчанию)
MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["❓ Частые вопросы", "📋 Памятка"],
        ["🛒 Что купить", "📅 Мой следующий визит"],
        ["📸 Фото прогресса", "📷 Отправить фото врачу"],
    ],
    resize_keyboard=True,
)

# Меню для элайнеров — добавлена кнопка учёта ношения капп
MENU_ALIGNERS = ReplyKeyboardMarkup(
    [
        ["❓ Частые вопросы", "😬 Мои каппы"],
        ["🛒 Что купить", "📅 Мой следующий визит"],
        ["📸 Фото прогресса", "📷 Отправить фото врачу"],
    ],
    resize_keyboard=True,
)

# Меню для ретейнеров (после лечения)
MENU_RETAINER = ReplyKeyboardMarkup(
    [
        ["❓ Частые вопросы", "📅 Мой следующий визит"],
        ["📸 Фото прогресса", "📷 Отправить фото врачу"],
    ],
    resize_keyboard=True,
)


def menu_for(chat_id: int) -> ReplyKeyboardMarkup:
    """Подбирает клавиатуру под стадию и тип аппарата пациента."""
    try:
        with db() as conn:
            r = conn.execute(
                "SELECT appliance, stage FROM patients WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
        if r and r["stage"] == "retainer":
            return MENU_RETAINER
        if r and r["appliance"] == "aligners":
            return MENU_ALIGNERS
    except Exception:
        pass
    return MENU_KEYBOARD

# Картинки-иллюстрации к некоторым ответам.
# Если файла нет рядом с ботом — ответ придёт просто текстом (бот не упадёт).
FAQ_IMAGES: dict[str, str] = {
    "food": "art/food.png",
    "hygiene": "art/brushing.png",
    "pain": "art/wax.png",
}

# ─────────────────────────── Хэндлеры пациента ───────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    upsert_patient(update.effective_chat.id, user.full_name, user.username)
    # приветственный баннер, если картинка на месте
    if os.path.exists("art/banner.png"):
        try:
            with open("art/banner.png", "rb") as f:
                await update.message.reply_photo(f)
        except Exception:
            pass
    await update.message.reply_text(
        f"Здравствуйте, {user.first_name}! 🦷\n\n"
        "Я бот-помощник вашего ортодонта. Могу:\n"
        "• ответить на частые вопросы\n"
        "• подсказать, что купить для ухода за полостью рта\n"
        "• передать врачу фото, если вас что-то беспокоит\n"
        "• напомнить о визите",
        reply_markup=menu_for(update.effective_chat.id),
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
    if appliance == "aligners":
        await query.message.reply_text(
            "Для элайнеров я добавил кнопку «😬 Мои каппы» — отмечайте там, "
            "когда снимаете и надеваете, чтобы следить за нормой ношения.",
            reply_markup=menu_for(query.message.chat_id),
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
    # если к вопросу есть иллюстрация и файл на месте — шлём картинкой с подписью
    img_path = FAQ_IMAGES.get(key)
    if img_path and os.path.exists(img_path):
        caption = f"<b>{title}</b>\n\n{text}"
        # подпись к фото ограничена 1024 символами — если длиннее, шлём фото + текст
        try:
            await query.message.delete()
        except Exception:
            pass
        if len(caption) <= 1024:
            with open(img_path, "rb") as f:
                await context.bot.send_photo(
                    query.message.chat_id, f, caption=caption,
                    parse_mode="HTML", reply_markup=back,
                )
        else:
            with open(img_path, "rb") as f:
                await context.bot.send_photo(query.message.chat_id, f)
            await context.bot.send_message(
                query.message.chat_id, caption,
                parse_mode="HTML", reply_markup=back,
            )
        return
    await query.edit_message_text(f"<b>{title}</b>\n\n{text}",
                                  parse_mode="HTML", reply_markup=back)


async def faq_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    buttons = [
        [InlineKeyboardButton(title, callback_data=f"faq:{key}")]
        for key, (title, _) in FAQ.items()
    ]
    markup = InlineKeyboardMarkup(buttons)
    # если предыдущий ответ был картинкой — у сообщения нет текста, редактировать
    # нельзя; удаляем его и шлём список заново
    if query.message.photo:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            query.message.chat_id, "Выберите вопрос:", reply_markup=markup
        )
    else:
        await query.edit_message_text("Выберите вопрос:", reply_markup=markup)


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


# ─── Счётчик ношения капп ───

WEAR_GOAL_HOURS = 20  # цель ношения в сутки


def _fmt_hm(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h} ч {m:02d} мин"


def _roll_wear_day(conn, chat_id: int) -> None:
    """Если наступил новый день — обнуляем дневной счётчик."""
    today = now_local().date().isoformat()
    r = conn.execute(
        "SELECT wear_date FROM patients WHERE chat_id=?", (chat_id,)
    ).fetchone()
    if not r or r["wear_date"] != today:
        conn.execute(
            "UPDATE patients SET wear_today=0, wear_date=?, wear_warned=NULL "
            "WHERE chat_id=?",
            (today, chat_id),
        )


async def show_wear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Экран учёта ношения капп с кнопками надел/снял."""
    chat_id = update.effective_chat.id
    with db() as conn:
        _roll_wear_day(conn, chat_id)
        r = conn.execute(
            "SELECT wear_today, wear_since FROM patients WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
    worn = r["wear_today"] or 0
    if r["wear_since"]:
        # сейчас надеты — прибавим текущий незакрытый интервал для показа
        since = datetime.fromisoformat(r["wear_since"])
        worn += int((now_local() - since).total_seconds())
        status = "😬 Сейчас каппы <b>надеты</b>"
        btn = InlineKeyboardButton("Снял каппы", callback_data="wear:off")
    else:
        status = "Каппы сейчас сняты"
        btn = InlineKeyboardButton("Надел каппы", callback_data="wear:on")
    goal = WEAR_GOAL_HOURS * 3600
    pct = min(100, int(worn / goal * 100)) if goal else 0
    bar = "▰" * (pct // 10) + "▱" * (10 - pct // 10)
    await update.message.reply_text(
        f"{status}\n\n"
        f"Сегодня наношено: <b>{_fmt_hm(worn)}</b> из {WEAR_GOAL_HOURS} ч\n"
        f"{bar} {pct}%\n\n"
        "Отмечайте каждый раз, когда снимаете и надеваете каппы — "
        "так мы видим, набирается ли норма 20–22 часа.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[btn]]),
    )


async def wear_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    chat_id = query.message.chat_id
    with db() as conn:
        _roll_wear_day(conn, chat_id)
        r = conn.execute(
            "SELECT wear_today, wear_since FROM patients WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        if action == "on":
            conn.execute(
                "UPDATE patients SET wear_since=? WHERE chat_id=?",
                (now_local().isoformat(), chat_id),
            )
            msg = "Отметил: каппы надеты 😬 Счётчик пошёл."
        else:  # off
            worn = r["wear_today"] or 0
            if r["wear_since"]:
                since = datetime.fromisoformat(r["wear_since"])
                worn += int((now_local() - since).total_seconds())
            conn.execute(
                "UPDATE patients SET wear_today=?, wear_since=NULL WHERE chat_id=?",
                (worn, chat_id),
            )
            msg = f"Отметил: каппы сняты. Сегодня уже {_fmt_hm(worn)}."
    try:
        await query.edit_message_reply_markup(None)
    except Exception:
        pass
    await query.message.reply_text(msg, reply_markup=menu_for(chat_id))


# ─── Фото прогресса ───

async def ask_progress_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting_progress"] = True
    await update.message.reply_text(
        "📸 Сделайте селфи улыбки — покрупнее, чтобы были видны зубы.\n\n"
        "Я сохраню его с сегодняшней датой, и со временем вы сможете "
        "наблюдать, как меняется ваша улыбка. Это здорово мотивирует! 😊\n\n"
        "Просто пришлите фото следующим сообщением."
    )


async def relay_to_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фото или текстовый вопрос пациента -> в чат врача."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    upsert_patient(chat_id, user.full_name, user.username)

    # фото прогресса — отдельный поток: сохраняем у врача с меткой даты
    if update.message.photo and context.user_data.get("awaiting_progress"):
        context.user_data["awaiting_progress"] = False
        today = now_local().strftime("%d.%m.%Y")
        with db() as conn:
            conn.execute(
                "UPDATE patients SET last_progress=? WHERE chat_id=?",
                (now_local().date().isoformat(), chat_id),
            )
        try:
            await context.bot.send_photo(
                DOCTOR_CHAT_ID,
                update.message.photo[-1].file_id,
                caption=f"📸 Фото прогресса — {user.full_name}, {today}",
                parse_mode="HTML",
            )
        except Exception as e:
            log.warning("Фото прогресса врачу: %s", e)
        await update.message.reply_text(
            "Сохранил ваше фото! 📸 Продолжайте в том же духе — "
            "скоро будет с чем сравнить 😊",
            reply_markup=menu_for(chat_id),
        )
        return

    # тревожная подпись к фото — экстренный сценарий
    caption_text = update.message.caption or ""
    if caption_text and looks_emergency(caption_text):
        await handle_emergency_photo(update, context)
        return

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


# Тревожные слова — при них бот сразу даёт срочный контакт, а не заготовку.
EMERGENCY_WORDS = [
    "сильная боль", "невыносим", "нестерпим", "проглотил", "проглотила",
    "задыха", "отёк", "отек", "опухл", "температура", "кровотеч",
    "кровь не останав", "аллерг", "не могу дышать", "обморок",
    "проволока в горле", "застряла в горле",
]

# Телефон для экстренной связи (можно задать переменной окружения).
EMERGENCY_PHONE = os.environ.get("EMERGENCY_PHONE", "")


def looks_emergency(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in EMERGENCY_WORDS)


async def handle_emergency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пациент написал что-то тревожное — даём срочные инструкции и зовём врача."""
    phone_line = (
        f"\n\n📞 Срочная связь с клиникой: {EMERGENCY_PHONE}"
        if EMERGENCY_PHONE else ""
    )
    await update.message.reply_text(
        "⚠️ Похоже, ситуация серьёзная.\n\n"
        "Если есть сильная боль, затруднённое дыхание, отёк, высокая "
        "температура или вы что-то проглотили и вам плохо — не ждите ответа "
        "в чате, свяжитесь с врачом напрямую или позвоните в скорую (103)."
        + phone_line +
        "\n\nЯ уже передал ваше сообщение врачу с пометкой «срочно».",
    )
    # врачу — с явной пометкой
    user = update.effective_user
    try:
        await context.bot.send_message(
            DOCTOR_CHAT_ID,
            f"🚨 <b>СРОЧНО</b> — пациент <b>{user.full_name}</b>"
            + (f" (@{user.username})" if user.username else "")
            + f"\nID: <code>{update.effective_chat.id}</code>\n\n"
            f"💬 {update.message.text}\n\n"
            "↩️ Ответьте reply-ем, чтобы написать пациенту.",
            parse_mode="HTML",
        )
    except Exception as e:
        log.warning("Не удалось отправить срочное врачу: %s", e)


async def handle_emergency_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фото с тревожной подписью — срочно врачу + инструкции пациенту."""
    user = update.effective_user
    phone_line = (
        f"\n📞 Срочная связь: {EMERGENCY_PHONE}" if EMERGENCY_PHONE else ""
    )
    await update.message.reply_text(
        "⚠️ Вижу, что дело серьёзное. Если вам плохо — не ждите ответа в чате, "
        "свяжитесь с врачом напрямую или звоните 103." + phone_line +
        "\n\nФото и сообщение уже отправлены врачу с пометкой «срочно»."
    )
    try:
        sent = await context.bot.send_photo(
            DOCTOR_CHAT_ID,
            update.message.photo[-1].file_id,
            caption=f"🚨 <b>СРОЧНО</b> — {user.full_name}"
            + (f" (@{user.username})" if user.username else "")
            + f"\nID: <code>{update.effective_chat.id}</code>\n\n"
            f"{update.message.caption or ''}\n\n↩️ Ответьте reply-ем.",
            parse_mode="HTML",
        )
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO relay VALUES (?, ?)",
                         (sent.message_id, update.effective_chat.id))
    except Exception as e:
        log.warning("Экстренное фото врачу: %s", e)


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
    elif text == "😬 Мои каппы":
        await show_wear(update, context)
    elif text == "📸 Фото прогресса":
        await ask_progress_photo(update, context)
    elif looks_emergency(text):
        # экстренный сценарий имеет приоритет над обычной пересылкой
        await handle_emergency(update, context)
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
        "Команды (или нажмите «/» — появится меню с подсказками):\n"
        "/patients — список пациентов\n"
        "/setvisit ID ДД.ММ.ГГГГ ЧЧ:ММ — назначить визит\n"
        "/aligners ID 14 20 — график элайнеров (каждые 14 дн., 20 капп)\n"
        "/installed ID — брекеты установлены сегодня (вкл. сопровождение)\n"
        "/elastics ID on — напоминания про эластики (или off)\n"
        "/retainer ID — перевести в режим ретейнеров (лечение окончено)\n"
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


async def cmd_aligners(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/aligners ID ИНТЕРВАЛ ВСЕГО — график смены элайнеров.
    Пример: /aligners 123456789 14 20 (смена каждые 14 дней, всего 20 капп,
    капа №1 надета сегодня)."""
    try:
        chat_id = int(context.args[0])
        interval = int(context.args[1])
        total = int(context.args[2])
        assert interval > 0 and total > 0
    except (IndexError, ValueError, AssertionError):
        await update.message.reply_text(
            "Формат: /aligners ID ИНТЕРВАЛ_ДНЕЙ ВСЕГО_КАПП\n"
            "Пример: /aligners 123456789 14 20\n"
            "(капа №1 считается надетой сегодня)"
        )
        return
    today = now_local().date().isoformat()
    with db() as conn:
        cur = conn.execute(
            "UPDATE patients SET alg_start=?, alg_interval=?, alg_total=?, "
            "alg_notified=? WHERE chat_id=?",
            (today, interval, total, today, chat_id),
        )
    if cur.rowcount == 0:
        await update.message.reply_text("Пациент с таким ID не найден.")
        return
    await update.message.reply_text(
        f"✅ График задан: смена каждые {interval} дн., всего {total} капп. "
        "Бот будет напоминать пациенту о каждой смене."
    )
    await context.bot.send_message(
        chat_id,
        f"📅 Врач задал ваш график элайнеров:\n"
        f"• сегодня надеваем капу №1\n"
        f"• смена — каждые {interval} дней\n"
        f"• всего капп: {total}\n\n"
        "Я напомню о каждой смене — ничего считать не нужно 😉",
    )


async def cmd_installed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/installed ID — пациенту сегодня установили брекеты, включить сопровождение."""
    try:
        chat_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Формат: /installed ID\n"
            "Отмечает, что пациенту сегодня установили брекеты — бот будет "
            "сопровождать его сообщениями на 1-й, 3-й и 7-й день."
        )
        return
    with db() as conn:
        cur = conn.execute(
            "UPDATE patients SET installed_at=?, followup_stage=0 WHERE chat_id=?",
            (now_local().date().isoformat(), chat_id),
        )
    if cur.rowcount == 0:
        await update.message.reply_text("Пациент с таким ID не найден.")
        return
    await update.message.reply_text(
        "✅ Сопровождение включено: бот напишет пациенту на 1-й, 3-й и 7-й день."
    )


async def cmd_retainer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/retainer ID — перевести пациента в режим ретейнеров (лечение окончено)."""
    try:
        chat_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Формат: /retainer ID\n"
            "Переводит пациента в режим ретейнеров: другие напоминания и меню, "
            "график элайнеров и эластики отключаются."
        )
        return
    with db() as conn:
        cur = conn.execute(
            "UPDATE patients SET stage='retainer', alg_start=NULL, "
            "elastics=0 WHERE chat_id=?",
            (chat_id,),
        )
    if cur.rowcount == 0:
        await update.message.reply_text("Пациент с таким ID не найден.")
        return
    await update.message.reply_text(
        "✅ Пациент переведён в режим ретейнеров. Бот будет напоминать носить "
        "ретейнер и приходить на контроль."
    )
    try:
        await context.bot.send_message(
            chat_id,
            "🎉 Поздравляем — активное лечение завершено!\n\n"
            "Теперь начинается важный этап — ретейнеры. Носите их строго по "
            "рекомендации врача, иначе зубы могут вернуться в прежнее положение.\n\n"
            "Я буду периодически напоминать. Ваша улыбка это заслужила! 😁",
            reply_markup=MENU_RETAINER,
        )
    except Exception:
        pass


async def cmd_elastics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/elastics ID on|off — вкл/выкл ежедневные напоминания про эластики."""
    try:
        chat_id = int(context.args[0])
        mode = context.args[1].lower()
        assert mode in ("on", "off")
    except (IndexError, ValueError, AssertionError):
        await update.message.reply_text(
            "Формат: /elastics ID on  (включить)  или  /elastics ID off\n"
            "Включает ежедневное напоминание пациенту носить эластики (резинки)."
        )
        return
    val = 1 if mode == "on" else 0
    with db() as conn:
        cur = conn.execute(
            "UPDATE patients SET elastics=? WHERE chat_id=?", (val, chat_id)
        )
    if cur.rowcount == 0:
        await update.message.reply_text("Пациент с таким ID не найден.")
        return
    await update.message.reply_text(
        f"✅ Напоминания про эластики {'включены' if val else 'выключены'}."
    )
    if val:
        try:
            await context.bot.send_message(
                chat_id,
                "🎽 Врач подключил напоминания про эластики (резинки). "
                "Я буду напоминать надевать их и менять на свежие — они теряют "
                "упругость за день. Носим столько, сколько сказал врач!",
            )
        except Exception:
            pass


FOLLOWUP_MESSAGES = [
    # (день, текст)
    (1, "Добрый день! Вчера вам установили брекеты — как самочувствие? 🙂\n\n"
        "Небольшая ноющая боль и непривычность — это нормально, зубы начали "
        "двигаться. Ешьте мягкую пищу, а если что-то натирает — ортодонтический "
        "воск в помощь.\n\nЕсли что-то беспокоит — просто напишите мне, "
        "передам врачу."),
    (3, "Как дела? 🦷 К третьему дню болезненность обычно начинает стихать.\n\n"
        "Если боль наоборот усиливается или что-то колет щёку — отправьте фото "
        "через меню «📷 Отправить фото врачу», врач посмотрит."),
    (7, "Неделя с брекетами — поздравляю, самое непривычное позади! 🎉\n\n"
        "Самое время проверить гигиену: ёршики и монопучковая щётка после еды, "
        "ирригатор вечером. Загляните в «🛒 Что купить», если чего-то ещё нет.\n\n"
        "Дальше — по плану: приёмы раз в 6–8 недель, врач всё расскажет."),
]


async def aligner_and_followup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежечасно: смена элайнеров и сопровождение после установки."""
    if is_quiet_hours():
        return
    today = now_local().date()
    with db() as conn:
        # — элайнеры —
        rows = conn.execute(
            "SELECT chat_id, alg_start, alg_interval, alg_total, alg_notified "
            "FROM patients WHERE alg_start IS NOT NULL"
        ).fetchall()
        for r in rows:
            start = datetime.fromisoformat(r["alg_start"]).date()
            days = (today - start).days
            if days <= 0 or days % r["alg_interval"] != 0:
                continue
            if r["alg_notified"] == today.isoformat():
                continue  # сегодня уже напоминали
            cap = days // r["alg_interval"] + 1
            try:
                if cap < r["alg_total"]:
                    await context.bot.send_message(
                        r["chat_id"],
                        f"😬 Сегодня меняем капу: надеваем <b>№{cap} из "
                        f"{r['alg_total']}</b>. Так держать! 🎉",
                        parse_mode="HTML",
                    )
                    conn.execute(
                        "UPDATE patients SET alg_notified=? WHERE chat_id=?",
                        (today.isoformat(), r["chat_id"]),
                    )
                elif cap == r["alg_total"]:
                    await context.bot.send_message(
                        r["chat_id"],
                        f"😬 Сегодня надеваем <b>последнюю капу №{cap} из "
                        f"{r['alg_total']}</b> — финишная прямая! 🏁",
                        parse_mode="HTML",
                    )
                    conn.execute(
                        "UPDATE patients SET alg_notified=? WHERE chat_id=?",
                        (today.isoformat(), r["chat_id"]),
                    )
                else:  # график закончился
                    await context.bot.send_message(
                        r["chat_id"],
                        "🎉 Ваш график элайнеров завершён! Врач расскажет о "
                        "следующих шагах на приёме.",
                    )
                    conn.execute(
                        "UPDATE patients SET alg_start=NULL, alg_notified=NULL "
                        "WHERE chat_id=?",
                        (r["chat_id"],),
                    )
                    await context.bot.send_message(
                        DOCTOR_CHAT_ID,
                        f"ℹ️ У пациента ID {r['chat_id']} закончился график "
                        "элайнеров — пора планировать следующий этап.",
                    )
            except Exception as e:
                log.warning("Элайнер-напоминание %s: %s", r["chat_id"], e)

        # — сопровождение после установки —
        rows = conn.execute(
            "SELECT chat_id, installed_at, followup_stage FROM patients "
            "WHERE installed_at IS NOT NULL AND followup_stage < ?",
            (len(FOLLOWUP_MESSAGES),),
        ).fetchall()
        for r in rows:
            installed = datetime.fromisoformat(r["installed_at"]).date()
            days = (today - installed).days
            stage = r["followup_stage"]
            due_day, text = FOLLOWUP_MESSAGES[stage]
            if days >= due_day:
                try:
                    await context.bot.send_message(r["chat_id"], text)
                    conn.execute(
                        "UPDATE patients SET followup_stage=? WHERE chat_id=?",
                        (stage + 1, r["chat_id"]),
                    )
                except Exception as e:
                    log.warning("Сопровождение %s: %s", r["chat_id"], e)


async def daily_care_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежечасно: эластики (утро), ретейнер (раз в неделю), фото-прогресс
    (раз в 2 недели), вечерний контроль ношения капп. Всё с тихими часами."""
    if is_quiet_hours():
        return
    now = now_local()
    today = now.date()
    with db() as conn:
        # — эластики: одно напоминание в день, в первый «рабочий» час (10:00) —
        if now.hour == QUIET_FROM:
            rows = conn.execute(
                "SELECT chat_id FROM patients WHERE elastics=1 AND "
                "(elastics_date IS NULL OR elastics_date!=?)", (today.isoformat(),)
            ).fetchall()
            for r in rows:
                try:
                    await context.bot.send_message(
                        r["chat_id"],
                        "🎽 Доброе утро! Не забудьте про эластики (резинки): "
                        "наденьте свежие на сегодня. Носим столько часов, "
                        "сколько назначил врач 💪",
                    )
                    conn.execute("UPDATE patients SET elastics_date=? WHERE chat_id=?",
                                 (today.isoformat(), r["chat_id"]))
                except Exception as e:
                    log.warning("Эластики %s: %s", r["chat_id"], e)

        # — вечерний контроль ношения капп (в 20-1=19ч по умолчанию) —
        if now.hour == QUIET_TO - 1:
            rows = conn.execute(
                "SELECT chat_id, wear_today, wear_since, wear_date, wear_warned "
                "FROM patients WHERE appliance='aligners' AND stage='active'"
            ).fetchall()
            for r in rows:
                if r["wear_date"] != today.isoformat():
                    continue  # сегодня не отмечали — не пилим
                worn = r["wear_today"] or 0
                if r["wear_since"]:
                    since = datetime.fromisoformat(r["wear_since"])
                    worn += int((now - since).total_seconds())
                if worn < WEAR_GOAL_HOURS * 3600 and r["wear_warned"] != today.isoformat():
                    try:
                        await context.bot.send_message(
                            r["chat_id"],
                            f"⏰ Сегодня каппы наношены {_fmt_hm(worn)} — это меньше "
                            f"нормы {WEAR_GOAL_HOURS} ч. Постарайтесь носить их "
                            "почти всё время: от этого напрямую зависит срок лечения!",
                        )
                        conn.execute("UPDATE patients SET wear_warned=? WHERE chat_id=?",
                                     (today.isoformat(), r["chat_id"]))
                    except Exception as e:
                        log.warning("Контроль ношения %s: %s", r["chat_id"], e)

        # — ретейнер: напоминание раз в 7 дней (по понедельникам в 11:00) —
        if now.weekday() == 0 and now.hour == 11:
            rows = conn.execute(
                "SELECT chat_id FROM patients WHERE stage='retainer'"
            ).fetchall()
            for r in rows:
                try:
                    await context.bot.send_message(
                        r["chat_id"],
                        "🦷 Еженедельное напоминание: носите ретейнер по графику! "
                        "Это сохраняет результат лечения. Если ретейнер треснул "
                        "или потерялся — сразу напишите мне.",
                    )
                except Exception as e:
                    log.warning("Ретейнер %s: %s", r["chat_id"], e)

        # — фото прогресса: просим раз в 14 дней (в 12:00) —
        if now.hour == 12:
            rows = conn.execute(
                "SELECT chat_id, last_progress, stage FROM patients "
                "WHERE stage IN ('active','retainer')"
            ).fetchall()
            for r in rows:
                last = r["last_progress"]
                due = (last is None) or (
                    (today - datetime.fromisoformat(last).date()).days >= 14
                )
                if due:
                    try:
                        await context.bot.send_message(
                            r["chat_id"],
                            "📸 Пора для фото прогресса! Сделайте селфи улыбки "
                            "через кнопку «📸 Фото прогресса» — сравним с прошлым "
                            "разом и увидим, как продвигается лечение 😊",
                        )
                        # ставим метку, чтобы не спамить каждый час до присылки
                        conn.execute(
                            "UPDATE patients SET last_progress=? WHERE chat_id=?",
                            (today.isoformat(), r["chat_id"]),
                        )
                    except Exception as e:
                        log.warning("Фото-прогресс запрос %s: %s", r["chat_id"], e)


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
    if is_quiet_hours():
        return  # не беспокоим рано утром и поздно вечером
    now = now_local()
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

        # — оценка визита: через 3 ч после визита, если ещё не спрашивали —
        rows = conn.execute(
            "SELECT chat_id, next_visit FROM patients "
            "WHERE next_visit IS NOT NULL AND reminded=1"
        ).fetchall()
        for r in rows:
            visit = datetime.fromisoformat(r["next_visit"])
            if visit + timedelta(hours=3) <= now:
                try:
                    await context.bot.send_message(
                        r["chat_id"],
                        "Как прошёл сегодняшний приём? Оцените, пожалуйста 🙂",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("⭐", callback_data="rate:1"),
                            InlineKeyboardButton("⭐⭐", callback_data="rate:2"),
                            InlineKeyboardButton("⭐⭐⭐", callback_data="rate:3"),
                            InlineKeyboardButton("⭐⭐⭐⭐", callback_data="rate:4"),
                            InlineKeyboardButton("⭐⭐⭐⭐⭐", callback_data="rate:5"),
                        ]]),
                    )
                    # визит отработан: очищаем дату, сбрасываем флаг
                    conn.execute(
                        "UPDATE patients SET next_visit=NULL, reminded=0 "
                        "WHERE chat_id=?", (r["chat_id"],),
                    )
                except Exception as e:
                    log.warning("Запрос оценки %s: %s", r["chat_id"], e)


# Ссылка на отзыв (Яндекс/2ГИС и т.п.) — задаётся переменной окружения.
REVIEW_URL = os.environ.get("REVIEW_URL", "")


async def rate_visit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пациент оценил визит: 4–5 → просим отзыв, 1–3 → тихо врачу."""
    query = update.callback_query
    await query.answer()
    score = int(query.data.split(":", 1)[1])
    user = query.from_user
    try:
        await query.edit_message_reply_markup(None)
    except Exception:
        pass
    if score >= 4:
        text = "Спасибо большое! Очень рады 😊"
        if REVIEW_URL:
            text += (f"\n\nБудем признательны за отзыв — это очень помогает "
                     f"клинике:\n{REVIEW_URL}")
        await query.message.reply_text(text)
    else:
        await query.message.reply_text(
            "Спасибо за честность. Мне жаль, что визит оставил вопросы — "
            "я передал вашу оценку врачу, с вами свяжутся."
        )
    # врачу — всегда, для статистики и реакции на негатив
    try:
        await context.bot.send_message(
            DOCTOR_CHAT_ID,
            f"{'⭐'*score} Оценка визита от {user.full_name}"
            + (f" (@{user.username})" if user.username else "")
            + (f"\nID: <code>{query.message.chat_id}</code>"
               if score < 4 else ""),
            parse_mode="HTML",
        )
    except Exception as e:
        log.warning("Оценка врачу: %s", e)


# ─────────────────────────── Запуск ───────────────────────────


async def setup_commands(app: Application) -> None:
    """Выпадающее меню команд: у врача — своё, у пациентов — только /start."""
    from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
    await app.bot.set_my_commands(
        [BotCommand("start", "Запустить бота")],
        scope=BotCommandScopeDefault(),
    )
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("patients", "Список пациентов"),
                BotCommand("setvisit", "Назначить визит: ID ДД.ММ.ГГГГ ЧЧ:ММ"),
                BotCommand("aligners", "График элайнеров: ID интервал всего"),
                BotCommand("installed", "Брекеты установлены сегодня: ID"),
                BotCommand("elastics", "Эластики вкл/выкл: ID on|off"),
                BotCommand("retainer", "Перевести в режим ретейнеров: ID"),
                BotCommand("broadcast", "Рассылка всем пациентам: текст"),
                BotCommand("backup", "Скачать копию базы"),
            ],
            scope=BotCommandScopeChat(DOCTOR_CHAT_ID),
        )
    except Exception as e:
        log.warning("Не удалось настроить меню врача: %s", e)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "Задайте переменную окружения BOT_TOKEN (токен — у @BotFather)."
        )
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(setup_commands).build()

    doctor = filters.Chat(DOCTOR_CHAT_ID)
    patient = ~doctor

    # Врач
    app.add_handler(CommandHandler("patients", cmd_patients, filters=doctor))
    app.add_handler(CommandHandler("setvisit", cmd_setvisit, filters=doctor))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast, filters=doctor))
    app.add_handler(CommandHandler("backup", cmd_backup, filters=doctor))
    app.add_handler(CommandHandler("aligners", cmd_aligners, filters=doctor))
    app.add_handler(CommandHandler("installed", cmd_installed, filters=doctor))
    app.add_handler(CommandHandler("retainer", cmd_retainer, filters=doctor))
    app.add_handler(CommandHandler("elastics", cmd_elastics, filters=doctor))
    app.add_handler(CallbackQueryHandler(visit_response, pattern=r"^visit:"))
    app.add_handler(MessageHandler(doctor & filters.REPLY, doctor_reply))
    app.add_handler(MessageHandler(doctor & ~filters.COMMAND, doctor_hint))

    # Пациент
    app.add_handler(CommandHandler("start", cmd_start, filters=patient))
    app.add_handler(CallbackQueryHandler(appliance_chosen, pattern=r"^appl:"))
    app.add_handler(CallbackQueryHandler(faq_answer, pattern=r"^faq:"))
    app.add_handler(CallbackQueryHandler(faq_back, pattern=r"^faq_list$"))
    app.add_handler(CallbackQueryHandler(wear_toggle, pattern=r"^wear:"))
    app.add_handler(CallbackQueryHandler(rate_visit, pattern=r"^rate:"))
    app.add_handler(MessageHandler(patient & filters.PHOTO, relay_to_doctor))
    app.add_handler(MessageHandler(patient & filters.TEXT & ~filters.COMMAND, patient_text))

    # Ежечасная проверка напоминаний (визиты + оценка визита)
    app.job_queue.run_repeating(send_reminders, interval=3600, first=10)
    # Элайнеры и сопровождение после установки — тоже ежечасно, с тихими часами
    app.job_queue.run_repeating(aligner_and_followup_job, interval=3600, first=30)
    # Эластики, ретейнеры, фото-прогресс, контроль ношения капп
    app.job_queue.run_repeating(daily_care_job, interval=3600, first=45)
    # Еженедельный бэкап базы врачу в Telegram (раз в 7 дней)
    app.job_queue.run_repeating(weekly_backup, interval=7 * 24 * 3600, first=60)

    log.info("Бот запущен 🦷")
    app.run_polling()


if __name__ == "__main__":
    main()
