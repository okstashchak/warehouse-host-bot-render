import warnings
warnings.filterwarnings("ignore", category=UserWarning, message=".*per_message=False.*")
import sqlite3
import logging
import os
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackContext,
    ConversationHandler,
    CallbackQueryHandler,
    filters
)
from datetime import datetime, timedelta
import calendar

# Настройки - получаем токен из переменных окружения
TOKEN = os.environ.get('BOT_TOKEN', '7576912897:AAGdkGgBYLrh1jjIUwvskqh6Ptqk-fcCqPM')
DB_NAME = "warehouse.db"
IMAGES_DIR = "images"

# Для Render используем абсолютные пути
if os.environ.get('RENDER'):
    IMAGES_DIR = "/tmp/images"
    DB_NAME = "/tmp/warehouse.db"

# Состояния для ConversationHandler
(
    CATEGORY_SELECTION,
    ITEM_NAME,
    ITEM_QUANTITY,
    ITEM_IMAGE,
    ITEM_COMMENT,
    RESERVE_ITEM_SELECTION,
    RESERVE_QUANTITY,
    RESERVE_START_DATE,
    RESERVE_END_DATE,
    RESERVE_EVENT,
    RETURN_SELECTION,
    DELETE_SELECTION,
    CHECK_DATE,
    VIEW_CATEGORY_SELECTION,
    VIEW_ITEM_SELECTION,
    SEARCH_ITEM,
) = range(16)

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    
    # Таблица категорий
    cur.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE
        )
    """)
    
    # Таблица товаров
    cur.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER,
            name TEXT,
            quantity INTEGER,
            image_path TEXT,
            comment TEXT,
            FOREIGN KEY (category_id) REFERENCES categories (id)
        )
    """)
    
    # Таблица бронирований - ОБНОВЛЕННАЯ ВЕРСИЯ
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER,
            quantity INTEGER,
            start_date TEXT,
            end_date TEXT,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            event_name TEXT,
            FOREIGN KEY (item_id) REFERENCES items (id)
        )
    """)
    
    # Стандартные категории
    cur.execute("INSERT OR IGNORE INTO categories (name) VALUES ('Ткань (и изделия из ткани)')")
    cur.execute("INSERT OR IGNORE INTO categories (name) VALUES ('Стекло')")
    cur.execute("INSERT OR IGNORE INTO categories (name) VALUES ('Искусственные цветы и зелень')")
    cur.execute("INSERT OR IGNORE INTO categories (name) VALUES ('Крупные конструкции')")
    cur.execute("INSERT OR IGNORE INTO categories (name) VALUES ('Сезонное')")
    cur.execute("INSERT OR IGNORE INTO categories (name) VALUES ('Фурнитура')")
    cur.execute("INSERT OR IGNORE INTO categories (name) VALUES ('Деревянные изделия')")
    conn.commit()
    conn.close()

def migrate_database():
    """Миграция базы данных для добавления новых колонок"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    
    try:
        # Проверяем существование колонок
        cur.execute("PRAGMA table_info(reservations)")
        columns = [column[1] for column in cur.fetchall()]
        
        # Добавляем недостающие колонки
        if 'user_id' not in columns:
            cur.execute("ALTER TABLE reservations ADD COLUMN user_id INTEGER")
            logger.info("Добавлена колонка user_id")
        
        if 'username' not in columns:
            cur.execute("ALTER TABLE reservations ADD COLUMN username TEXT")
            logger.info("Добавлена колонка username")
        
        if 'first_name' not in columns:
            cur.execute("ALTER TABLE reservations ADD COLUMN first_name TEXT")
            logger.info("Добавлена колонка first_name")
        
        if 'event_name' not in columns:
            cur.execute("ALTER TABLE reservations ADD COLUMN event_name TEXT")
            logger.info("Добавлена колонка event_name")
            
        conn.commit()
        logger.info("Миграция базы данных завершена успешно")
        
    except Exception as e:
        logger.error(f"Ошибка при миграции базы данных: {e}")
    finally:
        conn.close()

async def start(update: Update, context: CallbackContext) -> None:
    buttons = [
        ["Добавить позицию", "Забронировать"],
        ["Вернуть бронь", "Удалить позицию"],
        ["Текущие остатки", "Остатки на дату"],
        ["Просмотр позиции", "Мои бронирования"],
    ]
    await update.message.reply_text(
        "🏭 Бот управления складом\n\nВыберите действие:",
        reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True),
    )

async def add_item_start(update: Update, context: CallbackContext) -> int:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM categories")
    categories = cur.fetchall()
    conn.close()
    
    buttons = [
        [InlineKeyboardButton(cat[1], callback_data=f"cat_{cat[0]}")] for cat in categories
    ]
    await update.message.reply_text(
        "📁 Выберите категорию:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return CATEGORY_SELECTION

async def category_selection(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    category_id = int(query.data.split("_")[1])
    context.user_data["category_id"] = category_id
    
    # Получаем название категории для красивого отображения
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT name FROM categories WHERE id = ?", (category_id,))
    category_name = cur.fetchone()[0]
    conn.close()
    
    await query.edit_message_text(f"📁 Категория: {category_name}\n\nВведите название позиции:")
    return ITEM_NAME

async def item_name_input(update: Update, context: CallbackContext) -> int:
    item_name = update.message.text
    context.user_data["item_name"] = item_name
    
    # Проверяем, существует ли уже позиция с таким названием в этой категории
    category_id = context.user_data["category_id"]
    
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, quantity, image_path, comment FROM items WHERE category_id = ? AND name = ?",
        (category_id, item_name)
    )
    existing_item = cur.fetchone()
    conn.close()
    
    if existing_item:
        # Если позиция существует, сохраняем ее данные и пропускаем запрос фото
        context.user_data["existing_item"] = existing_item
        await update.message.reply_text(
            f"✅ Позиция '{item_name}' уже существует!\n"
            f"Текущее количество: {existing_item[1]} шт.\n\n"
            "🔢 Введите количество для добавления:"
        )
        return ITEM_QUANTITY
    else:
        # Новая позиция - запрашиваем количество как обычно
        await update.message.reply_text("🔢 Введите количество:")
        return ITEM_QUANTITY

async def item_quantity_input(update: Update, context: CallbackContext) -> int:
    try:
        quantity = int(update.message.text)
        if quantity <= 0:
            await update.message.reply_text("❌ Количество должно быть больше 0! Введите корректное количество:")
            return ITEM_QUANTITY
            
        # Проверяем, обновляем ли существующую позицию или создаем новую
        if "existing_item" in context.user_data:
            # Обновляем существующую позицию
            existing_item = context.user_data["existing_item"]
            item_id, old_quantity, image_path, comment = existing_item
            new_quantity = old_quantity + quantity
            
            conn = sqlite3.connect(DB_NAME)
            cur = conn.cursor()
            cur.execute(
                "UPDATE items SET quantity = ? WHERE id = ?",
                (new_quantity, item_id)
            )
            conn.commit()
            conn.close()
            
            await update.message.reply_text(
                f"✅ Позиция обновлена!\n"
                f"📦 {context.user_data['item_name']}\n"
                f"📊 Новое количество: {new_quantity} шт."
            )
            # Очищаем временные данные
            if "existing_item" in context.user_data:
                del context.user_data["existing_item"]
            return ConversationHandler.END
        else:
            # Создаем новую позицию - запрашиваем фото
            context.user_data["quantity"] = quantity
            await update.message.reply_text("📸 Загрузите фото товара:")
            return ITEM_IMAGE
            
    except ValueError:
        await update.message.reply_text("❌ Введите целое число!")
        return ITEM_QUANTITY

async def item_image_input(update: Update, context: CallbackContext) -> int:
    if not update.message.photo:
        await update.message.reply_text("❌ Пожалуйста, загрузите фото!")
        return ITEM_IMAGE
    
    os.makedirs(IMAGES_DIR, exist_ok=True)
    photo_file = await update.message.photo[-1].get_file()
    image_path = os.path.join(IMAGES_DIR, f"{datetime.now().timestamp()}.jpg")
    await photo_file.download_to_drive(image_path)
    context.user_data["image_path"] = image_path
    await update.message.reply_text("📝 Введите комментарий к товару:")
    return ITEM_COMMENT

async def item_comment_input(update: Update, context: CallbackContext) -> int:
    comment = update.message.text
    
    # Сохранение новой позиции в БД
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO items (category_id, name, quantity, image_path, comment) VALUES (?, ?, ?, ?, ?)",
        (
            context.user_data["category_id"],
            context.user_data["item_name"],
            context.user_data["quantity"],
            context.user_data["image_path"],
            comment,
        ),
    )
    conn.commit()
    conn.close()
    
    await update.message.reply_text(
        f"✅ Позиция успешно добавлена на склад!\n"
        f"📦 {context.user_data['item_name']}\n"
        f"📊 Количество: {context.user_data['quantity']} шт."
    )
    return ConversationHandler.END

# Функции для календаря
def generate_calendar(year=None, month=None, selection_type="start"):
    """Генерирует inline-клавиатуру с календарем"""
    now = datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month
    
    # Создаем календарь
    cal = calendar.monthcalendar(year, month)
    month_name = calendar.month_name[month]
    
    # Создаем кнопки для дней
    keyboard = []
    
    # Заголовок с месяцем и годом
    header = [InlineKeyboardButton(f"{month_name} {year}", callback_data="ignore")]
    keyboard.append(header)
    
    # Дни недели
    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    keyboard.append([InlineKeyboardButton(day, callback_data="ignore") for day in week_days])
    
    # Дни месяца
    for week in cal:
        week_buttons = []
        for day in week:
            if day == 0:
                week_buttons.append(InlineKeyboardButton(" ", callback_data="ignore"))
            else:
                date_str = f"{year}-{month:02d}-{day:02d}"
                week_buttons.append(InlineKeyboardButton(str(day), callback_data=f"date_{selection_type}_{date_str}"))
        keyboard.append(week_buttons)
    
    # Кнопки навигации
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1
    
    nav_buttons = [
        InlineKeyboardButton("◀️", callback_data=f"nav_{selection_type}_{prev_year}_{prev_month}"),
        InlineKeyboardButton("Сегодня", callback_data=f"date_{selection_type}_{now.year}-{now.month:02d}-{now.day:02d}"),
        InlineKeyboardButton("▶️", callback_data=f"nav_{selection_type}_{next_year}_{next_month}")
    ]
    keyboard.append(nav_buttons)
    
    return InlineKeyboardMarkup(keyboard)

# Функции для бронирования с добавлением информации о пользователе и мероприятии
async def reserve_item_start(update: Update, context: CallbackContext) -> int:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT i.id, i.name, c.name, i.quantity
        FROM items i 
        JOIN categories c ON i.category_id = c.id
        WHERE i.quantity > 0
    """)
    items = cur.fetchall()
    conn.close()
    
    if not items:
        await update.message.reply_text("❌ На складе нет доступных позиций!")
        return ConversationHandler.END
    
    buttons = []
    for item_id, item_name, category_name, quantity in items:
        buttons.append([InlineKeyboardButton(
            f"{category_name} - {item_name} ({quantity}шт)", 
            callback_data=f"ritem_{item_id}"
        )])
    
    await update.message.reply_text(
        "📦 Выберите позицию для бронирования:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return RESERVE_ITEM_SELECTION

async def reserve_item_selection(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    item_id = int(query.data.split("_")[1])
    context.user_data["reserve_item_id"] = item_id
    
    # Получаем информацию о товаре
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT i.name, c.name, i.quantity 
        FROM items i 
        JOIN categories c ON i.category_id = c.id 
        WHERE i.id = ?
    """, (item_id,))
    item_name, category_name, quantity = cur.fetchone()
    conn.close()
    
    context.user_data["current_quantity"] = quantity
    await query.edit_message_text(
        f"📦 Товар: {category_name} - {item_name}\n"
        f"📊 Доступно: {quantity} шт.\n\n"
        "Введите количество для бронирования:"
    )
    return RESERVE_QUANTITY

async def reserve_quantity_input(update: Update, context: CallbackContext) -> int:
    try:
        reserve_quantity = int(update.message.text)
        current_quantity = context.user_data["current_quantity"]
        
        if reserve_quantity <= 0:
            await update.message.reply_text("❌ Количество должно быть больше 0! Повторите ввод:")
            return RESERVE_QUANTITY
        
        if reserve_quantity > current_quantity:
            await update.message.reply_text(
                f"❌ Недостаточно товара! Доступно только {current_quantity} шт.\n"
                "Введите новое количество:"
            )
            return RESERVE_QUANTITY
            
        context.user_data["reserve_quantity"] = reserve_quantity
        
        # Показываем календарь для выбора даты начала
        await update.message.reply_text(
            "📅 Выберите дату НАЧАЛА бронирования:",
            reply_markup=generate_calendar(selection_type="start")
        )
        return RESERVE_START_DATE
    except ValueError:
        await update.message.reply_text("❌ Введите целое число!")
        return RESERVE_QUANTITY

async def reserve_start_date_input(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("nav_start"):
        # Навигация по календарю
        _, _, year, month = query.data.split("_")
        await query.edit_message_text(
            "📅 Выберите дату НАЧАЛА бронирования:",
            reply_markup=generate_calendar(int(year), int(month), "start")
        )
        return RESERVE_START_DATE
    
    elif query.data.startswith("date_start"):
        # Дата выбрана
        _, _, date_str = query.data.split("_")
        start_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = datetime.now().date()
        
        if start_date < today:
            await query.answer("❌ Дата начала не может быть в прошлом!", show_alert=True)
            return RESERVE_START_DATE
        
        context.user_data["reserve_start_date"] = start_date.isoformat()
        
        # Показываем календарь для выбора даты окончания
        await query.edit_message_text(
            "📅 Выберите дату ОКОНЧАНИЯ бронирования:",
            reply_markup=generate_calendar(selection_type="end")
        )
        return RESERVE_END_DATE
    
    return RESERVE_START_DATE

async def reserve_end_date_input(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("nav_end"):
        # Навигация по календарю
        _, _, year, month = query.data.split("_")
        await query.edit_message_text(
            "📅 Выберите дату ОКОНЧАНИЯ бронирования:",
            reply_markup=generate_calendar(int(year), int(month), "end")
        )
        return RESERVE_END_DATE
    
    elif query.data.startswith("date_end"):
        # Дата выбрана
        _, _, date_str = query.data.split("_")
        end_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        start_date = datetime.fromisoformat(context.user_data["reserve_start_date"]).date()
        
        if end_date <= start_date:
            await query.answer("❌ Дата окончания должна быть после даты начала!", show_alert=True)
            return RESERVE_END_DATE
        
        # Сохраняем дату окончания
        context.user_data["reserve_end_date"] = end_date.isoformat()
        
        # Запрашиваем название мероприятия
        await query.edit_message_text(
            "🎯 Введите название мероприятия или комментарий к бронированию:"
        )
        return RESERVE_EVENT
    
    return RESERVE_END_DATE

async def reserve_event_input(update: Update, context: CallbackContext) -> int:
    try:
        event_name = update.message.text
        context.user_data["reserve_event"] = event_name
        
        # Проверяем доступность товара в указанный период
        item_id = context.user_data["reserve_item_id"]
        reserve_quantity = context.user_data["reserve_quantity"]
        start_date = datetime.fromisoformat(context.user_data["reserve_start_date"]).date()
        end_date = datetime.fromisoformat(context.user_data["reserve_end_date"]).date()
        
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        
        # Получаем общее количество товара
        cur.execute("SELECT quantity FROM items WHERE id = ?", (item_id,))
        result = cur.fetchone()
        if not result:
            await update.message.reply_text("❌ Товар не найден!")
            return ConversationHandler.END
        total_quantity = result[0]
        
        # Получаем сумму забронированных quantity в пересекающиеся периоды
        cur.execute("""
            SELECT SUM(quantity) FROM reservations 
            WHERE item_id = ? 
            AND ((start_date <= ? AND end_date >= ?) 
                 OR (start_date <= ? AND end_date >= ?)
                 OR (start_date >= ? AND end_date <= ?))
        """, (item_id, start_date, start_date, end_date, end_date, start_date, end_date))
        
        result = cur.fetchone()
        reserved_quantity = result[0] or 0 if result else 0
        
        available_quantity = total_quantity - reserved_quantity
        
        if reserve_quantity > available_quantity:
            await update.message.reply_text(
                f"❌ Недостаточно товара в указанный период! Доступно только {available_quantity} шт.\n\n"
                "Введите новое количество для бронирования:"
            )
            # Возвращаем к вводу количества
            return RESERVE_QUANTITY
        
        # Получаем информацию о пользователе
        user = update.effective_user
        user_id = user.id
        username = f"@{user.username}" if user.username else user.first_name or "Пользователь"
        first_name = user.first_name or ""
        
        # Создание брони
        cur.execute(
            "INSERT INTO reservations (item_id, quantity, start_date, end_date, user_id, username, first_name, event_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                reserve_quantity,
                context.user_data["reserve_start_date"],
                context.user_data["reserve_end_date"],
                user_id,
                username,
                first_name,
                event_name,
            ),
        )
        conn.commit()
        
        # Получаем название товара для сообщения
        cur.execute("SELECT name FROM items WHERE id = ?", (item_id,))
        result = cur.fetchone()
        if not result:
            await update.message.reply_text("❌ Ошибка при получении информации о товаре!")
            return ConversationHandler.END
        item_name = result[0]
        conn.close()
        
        await update.message.reply_text(
            f"✅ Бронь успешно создана!\n\n"
            f"📦 Товар: {item_name}\n"
            f"📊 Количество: {reserve_quantity} шт.\n"
            f"📅 Период: {start_date} - {end_date}\n"
            f"🎯 Мероприятие: {event_name}\n"
            f"👤 Забронировал: {username}"
        )
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Ошибка при создании брони: {e}")
        await update.message.reply_text("❌ Произошла ошибка при создании брони. Попробуйте еще раз.")
        return ConversationHandler.END

# Функции для возврата брони с информацией о пользователе
async def return_reservation(update: Update, context: CallbackContext) -> None:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT r.id, i.name, c.name, r.quantity, r.start_date, r.end_date, r.username, r.event_name
        FROM reservations r 
        JOIN items i ON r.item_id = i.id
        JOIN categories c ON i.category_id = c.id
        WHERE r.end_date >= date('now')
        ORDER BY r.start_date
    """)
    reservations = cur.fetchall()
    conn.close()
    
    if not reservations:
        await update.message.reply_text("❌ Нет активных бронирований!")
        return
    
    buttons = []
    for res_id, item_name, cat_name, quantity, start_date, end_date, username, event_name in reservations:
        event_text = f" - {event_name}" if event_name else ""
        buttons.append([InlineKeyboardButton(
            f"{cat_name} - {item_name} ({quantity}шт) {start_date} - {end_date} ({username}{event_text})", 
            callback_data=f"ret_{res_id}"
        )])
    
    await update.message.reply_text(
        "📦 Выберите бронь для возврата:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def return_selection(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    reserve_id = int(query.data.split("_")[1])
    
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    
    # Получаем информацию о брони перед удалением
    cur.execute("""
        SELECT i.name, r.username, r.event_name, r.user_id 
        FROM reservations r 
        JOIN items i ON r.item_id = i.id 
        WHERE r.id = ?
    """, (reserve_id,))
    item_name, username, event_name, user_id = cur.fetchone()
    
    cur.execute("DELETE FROM reservations WHERE id = ?", (reserve_id,))
    conn.commit()
    conn.close()
    
    event_text = f" для мероприятия '{event_name}'" if event_name else ""
    await query.edit_message_text(f"✅ Бронь '{item_name}'{event_text} от {username} успешно возвращена!")

# Функции для удаления позиции
async def delete_item(update: Update, context: CallbackContext) -> None:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT i.id, i.name, c.name, i.quantity
        FROM items i 
        JOIN categories c ON i.category_id = c.id
        ORDER BY c.name, i.name
    """)
    items = cur.fetchall()
    conn.close()
    
    if not items:
        await update.message.reply_text("❌ Нет позиций для удаления!")
        return
    
    buttons = []
    for item_id, item_name, cat_name, quantity in items:
        buttons.append([InlineKeyboardButton(
            f"{cat_name} - {item_name} ({quantity}шт)", 
            callback_data=f"del_{item_id}"
        )])
    
    await update.message.reply_text(
        "🗑️ Выберите позицию для удаления:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def delete_selection(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    item_id = int(query.data.split("_")[1])
    
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    
    # Получаем информацию о товаре перед удалением
    cur.execute("SELECT name, image_path FROM items WHERE id = ?", (item_id,))
    item_name, image_path = cur.fetchone()
    
    # Удаление изображения
    if image_path and os.path.exists(image_path):
        os.remove(image_path)
    
    # Удаление связанных бронирований
    cur.execute("DELETE FROM reservations WHERE item_id = ?", (item_id,))
    # Удаление товара
    cur.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"✅ Позиция '{item_name}' успешно удалена!")

# Функции для просмотра остатков
async def current_stock(update: Update, context: CallbackContext) -> None:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT c.name, i.name, i.quantity, i.comment
        FROM items i 
        JOIN categories c ON i.category_id = c.id
        ORDER BY c.name, i.name
    """)
    items = cur.fetchall()
    conn.close()
    
    if not items:
        await update.message.reply_text("📭 Склад пуст!")
        return
    
    response = "📦 Текущие остатки на складе:\n\n"
    current_category = ""
    
    for cat, name, qty, comment in items:
        if cat != current_category:
            response += f"📁 {cat}:\n"
            current_category = cat
        response += f"  • {name}: {qty}шт"
        if comment:
            response += f" ({comment})"
        response += "\n"
    
    await update.message.reply_text(response)

async def date_stock_start(update: Update, context: CallbackContext) -> int:
    # Показываем календарь для выбора даты проверки остатков
    await update.message.reply_text(
        "📅 Выберите дату для проверки остатков:",
        reply_markup=generate_calendar(selection_type="check")
    )
    return CHECK_DATE

async def date_stock_check(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("nav_check"):
        # Навигация по календарю
        _, _, year, month = query.data.split("_")
        await query.edit_message_text(
            "📅 Выберите дату для проверки остатков:",
            reply_markup=generate_calendar(int(year), int(month), "check")
        )
        return CHECK_DATE
    
    elif query.data.startswith("date_check"):
        # Дата выбрана
        _, _, date_str = query.data.split("_")
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        current_date = datetime.now().date()
        
        if target_date < current_date:
            await query.answer("❌ Дата не может быть в прошлом!", show_alert=True)
            return CHECK_DATE
            
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        
        cur.execute("""
            SELECT 
                c.name,
                i.name,
                i.quantity - IFNULL(SUM(r.quantity), 0) as available
            FROM items i
            JOIN categories c ON i.category_id = c.id
            LEFT JOIN reservations r ON i.id = r.item_id 
                AND r.start_date <= ? 
                AND r.end_date >= ?
            GROUP BY i.id
            ORDER BY c.name, i.name
        """, (target_date.isoformat(), target_date.isoformat()))
        
        items = cur.fetchall()
        conn.close()
        
        if not items:
            await query.edit_message_text(f"📭 На {target_date} нет позиций на складе!")
            return ConversationHandler.END
        
        response = f"📅 Остатки на {target_date}:\n\n"
        current_category = ""
        
        for cat, name, qty in items:
            if cat != current_category:
                response += f"📁 {cat}:\n"
                current_category = cat
            available = max(0, qty)  # Не показываем отрицательные значения
            response += f"  • {name}: {available}шт\n"
        
        await query.edit_message_text(response)
        return ConversationHandler.END
    
    return CHECK_DATE

# Улучшенные функции для просмотра позиции
async def view_item_start(update: Update, context: CallbackContext) -> int:
    buttons = [
        [InlineKeyboardButton("📁 Поиск по категориям", callback_data="view_categories")],
        [InlineKeyboardButton("🔍 Поиск по названию", callback_data="view_search")],
    ]
    
    await update.message.reply_text(
        "🔍 Выберите способ поиска позиции:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return VIEW_CATEGORY_SELECTION

async def view_category_method(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data == "view_categories":
        # Показываем список категорий
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM categories")
        categories = cur.fetchall()
        conn.close()
        
        if not categories:
            await query.edit_message_text("❌ В базе нет категорий!")
            return ConversationHandler.END
        
        buttons = []
        for cat_id, cat_name in categories:
            buttons.append([InlineKeyboardButton(cat_name, callback_data=f"viewcat_{cat_id}")])
        
        await query.edit_message_text(
            "📁 Выберите категорию:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return VIEW_CATEGORY_SELECTION
    
    elif query.data == "view_search":
        await query.edit_message_text(
            "🔍 Введите название позиции для поиска (можно часть названия):"
        )
        return SEARCH_ITEM

async def view_category_selection(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    category_id = int(query.data.split("_")[1])
    
    # Получаем позиции в выбранной категории
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT i.id, i.name, i.quantity 
        FROM items i 
        WHERE i.category_id = ?
        ORDER BY i.name
    """, (category_id,))
    items = cur.fetchall()
    
    # Получаем название категории
    cur.execute("SELECT name FROM categories WHERE id = ?", (category_id,))
    category_name = cur.fetchone()[0]
    conn.close()
    
    if not items:
        await query.edit_message_text(f"❌ В категории '{category_name}' нет позиций!")
        return ConversationHandler.END
    
    buttons = []
    for item_id, item_name, quantity in items:
        buttons.append([InlineKeyboardButton(
            f"{item_name} ({quantity}шт)", 
            callback_data=f"viewitem_{item_id}"
        )])
    
    await query.edit_message_text(
        f"📁 Категория: {category_name}\n\n"
        "📦 Выберите позицию для просмотра:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return VIEW_ITEM_SELECTION

async def search_item_input(update: Update, context: CallbackContext) -> int:
    search_term = update.message.text
    
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT i.id, i.name, c.name, i.quantity 
        FROM items i 
        JOIN categories c ON i.category_id = c.id
        WHERE i.name LIKE ?
        ORDER BY c.name, i.name
    """, (f"%{search_term}%",))
    items = cur.fetchall()
    conn.close()
    
    if not items:
        await update.message.reply_text(f"❌ Не найдено позиций по запросу '{search_term}'!")
        return ConversationHandler.END
    
    buttons = []
    for item_id, item_name, category_name, quantity in items:
        buttons.append([InlineKeyboardButton(
            f"{category_name} - {item_name} ({quantity}шт)", 
            callback_data=f"viewitem_{item_id}"
        )])
    
    await update.message.reply_text(
        f"🔍 Результаты поиска по '{search_term}':\n\n"
        "📦 Выберите позицию для просмотра:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return VIEW_ITEM_SELECTION

async def view_item_selection(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    
    # Получаем ID товара из callback_data
    if not query.data.startswith("viewitem_"):
        await query.edit_message_text("❌ Ошибка при выборе позиции!")
        return ConversationHandler.END
        
    item_id = int(query.data.split("_")[1])
    
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    
    # Получаем информацию о товаре
    cur.execute("""
        SELECT i.name, c.name, i.quantity, i.comment, i.image_path
        FROM items i 
        JOIN categories c ON i.category_id = c.id 
        WHERE i.id = ?
    """, (item_id,))
    item_info = cur.fetchone()
    
    if not item_info:
        await query.edit_message_text("❌ Позиция не найдена!")
        return ConversationHandler.END
    
    item_name, category_name, quantity, comment, image_path = item_info
    
    # Получаем активные брони для этой позиции
    cur.execute("""
        SELECT start_date, end_date, quantity, username, event_name
        FROM reservations 
        WHERE item_id = ? AND end_date >= date('now')
        ORDER BY start_date
    """, (item_id,))
    reservations = cur.fetchall()
    
    conn.close()
    
    # Формируем сообщение с информацией о позиции
    message = f"📦 Карточка позиции\n\n"
    message += f"📁 Категория: {category_name}\n"
    message += f"📋 Название: {item_name}\n"
    message += f"📊 Количество: {quantity} шт.\n"
    
    if comment:
        message += f"📝 Комментарий: {comment}\n"
    
    if reservations:
        message += f"\n📅 Активные брони:\n"
        for start_date, end_date, res_quantity, username, event_name in reservations:
            event_text = f" - {event_name}" if event_name else ""
            message += f"  • {start_date} - {end_date}: {res_quantity} шт. ({username}{event_text})\n"
    else:
        message += f"\n✅ Нет активных броней"
    
    # Если есть фото, отправляем его с подписью
    if image_path and os.path.exists(image_path):
        try:
            with open(image_path, 'rb') as photo:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=photo,
                    caption=message
                )
            await query.edit_message_text("✅ Вот информация о позиции:")
        except Exception as e:
            logger.error(f"Ошибка при отправке фото: {e}")
            await query.edit_message_text(f"{message}\n\n❌ Не удалось загрузить фото")
    else:
        await query.edit_message_text(message)
    
    return ConversationHandler.END

# Новая функция для просмотра своих бронирований
async def my_reservations(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    user_id = user.id
    
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT r.id, i.name, c.name, r.quantity, r.start_date, r.end_date, r.event_name
        FROM reservations r 
        JOIN items i ON r.item_id = i.id
        JOIN categories c ON i.category_id = c.id
        WHERE r.user_id = ? AND r.end_date >= date('now')
        ORDER BY r.end_date
    """, (user_id,))
    reservations = cur.fetchall()
    conn.close()
    
    if not reservations:
        await update.message.reply_text("📭 У вас нет активных бронирований!")
        return
    
    response = "📋 Ваши активные бронирования:\n\n"
    
    today = datetime.now().date()
    for res_id, item_name, cat_name, quantity, start_date, end_date, event_name in reservations:
        end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
        days_left = (end_date_obj - today).days
        
        status = "🟢" if days_left > 2 else "🟡" if days_left > 0 else "🔴"
        event_text = f" - {event_name}" if event_name else ""
        
        response += f"{status} {cat_name} - {item_name} ({quantity}шт)\n"
        response += f"   📅 {start_date} - {end_date}{event_text}\n"
        response += f"   ⏳ Осталось дней: {days_left}\n\n"
    
    await update.message.reply_text(response)

# Функция для отправки напоминаний (для администраторов)
async def send_reminders(update: Update, context: CallbackContext) -> None:
    # Проверяем, является ли пользователь администратором
    # Здесь можно добавить проверку на ID администратора
    # ADMIN_IDS = [123456789, 987654321]  # Замените на реальные ID
    
    # if update.effective_user.id not in ADMIN_IDS:
    #     await update.message.reply_text("❌ У вас нет прав для этой команды!")
    #     return
    
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    
    # Находим брони, которые заканчиваются сегодня или уже просрочены
    cur.execute("""
        SELECT r.id, i.name, r.end_date, r.user_id, r.username, r.event_name
        FROM reservations r 
        JOIN items i ON r.item_id = i.id
        WHERE r.end_date <= date('now', '+3 days') AND r.end_date >= date('now')
        ORDER BY r.end_date
    """)
    ending_reservations = cur.fetchall()
    
    # Находим просроченные брони
    cur.execute("""
        SELECT r.id, i.name, r.end_date, r.user_id, r.username, r.event_name
        FROM reservations r 
        JOIN items i ON r.item_id = i.id
        WHERE r.end_date < date('now')
        ORDER BY r.end_date
    """)
    overdue_reservations = cur.fetchall()
    
    conn.close()
    
    if not ending_reservations and not overdue_reservations:
        await update.message.reply_text("✅ Нет бронирований для напоминаний!")
        return
    
    response = "🔔 Напоминания о бронированиях:\n\n"
    
    if ending_reservations:
        response += "📋 Бронирования, которые скоро заканчиваются:\n"
        for res_id, item_name, end_date, user_id, username, event_name in ending_reservations:
            end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
            days_left = (end_date_obj - datetime.now().date()).days
            event_text = f" ({event_name})" if event_name else ""
            
            response += f"• {item_name}{event_text} - заканчивается через {days_left} дн. (@{username if username.startswith('@') else username})\n"
        
        response += "\n"
    
    if overdue_reservations:
        response += "🚨 Просроченные бронирования:\n"
        for res_id, item_name, end_date, user_id, username, event_name in overdue_reservations:
            end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
            days_overdue = (datetime.now().date() - end_date_obj).days
            event_text = f" ({event_name})" if event_name else ""
            
            response += f"• {item_name}{event_text} - просрочено на {days_overdue} дн. (@{username if username.startswith('@') else username})\n"
    
    response += "\n💡 Используйте команду /notify_all для отправки уведомлений всем пользователям."
    
    await update.message.reply_text(response)

# Функция для отправки уведомлений всем пользователям
async def notify_all_users(update: Update, context: CallbackContext) -> None:
    # Проверка прав администратора (раскомментируйте и настройте при необходимости)
    # ADMIN_IDS = [123456789, 987654321]
    # if update.effective_user.id not in ADMIN_IDS:
    #     await update.message.reply_text("❌ У вас нет прав для этой команды!")
    #     return
    
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    
    # Находим все активные брони
    cur.execute("""
        SELECT DISTINCT user_id, username
        FROM reservations 
        WHERE end_date >= date('now')
    """)
    users = cur.fetchall()
    
    notified_count = 0
    for user_id, username in users:
        try:
            # Получаем брони пользователя
            cur.execute("""
                SELECT i.name, r.end_date, r.event_name
                FROM reservations r 
                JOIN items i ON r.item_id = i.id
                WHERE r.user_id = ? AND r.end_date >= date('now')
                ORDER BY r.end_date
            """, (user_id,))
            user_reservations = cur.fetchall()
            
            if user_reservations:
                message = "🔔 Напоминание о ваших бронированиях:\n\n"
                
                today = datetime.now().date()
                for item_name, end_date, event_name in user_reservations:
                    end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
                    days_left = (end_date_obj - today).days
                    event_text = f" ({event_name})" if event_name else ""
                    
                    status = "🟢" if days_left > 2 else "🟡" if days_left > 0 else "🔴"
                    message += f"{status} {item_name}{event_text}\n"
                    message += f"   📅 До {end_date} (осталось {days_left} дн.)\n\n"
                
                message += "⚠️ Пожалуйста, не забудьте вернуть позиции вовремя!"
                
                # Отправляем сообщение пользователю
                await context.bot.send_message(chat_id=user_id, text=message)
                notified_count += 1
                
        except Exception as e:
            logger.error(f"Ошибка при отправке уведомления пользователю {username}: {e}")
    
    conn.close()
    
    await update.message.reply_text(f"✅ Уведомления отправлены {notified_count} пользователям!")

async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("❌ Операция отменена.")
    return ConversationHandler.END

async def help_command(update: Update, context: CallbackContext) -> None:
    help_text = """
🤖 Бот управления складом

Доступные команды:

📥 Добавить позицию - добавить новый товар на склад
📦 Забронировать - забронировать товар на период
↩️ Вернуть бронь - досрочно вернуть забронированный товар
🗑️ Удалить позицию - удалить товар со склада
📊 Текущие остатки - посмотреть текущее наличие
📅 Остатки на дату - посчитать остатки на будущую дату
👀 Просмотр позиции - посмотреть детальную информацию о позиции
📋 Мои бронирования - посмотреть свои активные брони

Административные команды:
/reminders - показать бронирования, требующие внимания
/notify_all - отправить уведомления всем пользователям

Для начала работы нажмите /start
    """
    await update.message.reply_text(help_text)

def main() -> None:
    # Создаем папку для изображений
    os.makedirs(IMAGES_DIR, exist_ok=True)
    
    # Инициализируем базу данных
    init_db()
    
    # ВЫПОЛНЯЕМ МИГРАЦИЮ БАЗЫ ДАННЫХ
    migrate_database()
    
    # Создаем Application с обработкой ошибок
    try:
        application = Application.builder().token(TOKEN).build()
    except Exception as e:
        logger.error(f"Ошибка при создании Application: {e}")
        # Альтернативный способ для старых версий
        from telegram.ext import Updater
        application = Application.builder().token(TOKEN).build()

    # Обработчики диалогов
    add_item_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Добавить позицию$"), add_item_start)],
        states={
            CATEGORY_SELECTION: [CallbackQueryHandler(category_selection, pattern="^cat_")],
            ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, item_name_input)],
            ITEM_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, item_quantity_input)],
            ITEM_IMAGE: [MessageHandler(filters.PHOTO, item_image_input)],
            ITEM_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, item_comment_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    reserve_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Забронировать$"), reserve_item_start)],
        states={
            RESERVE_ITEM_SELECTION: [CallbackQueryHandler(reserve_item_selection, pattern="^ritem_")],
            RESERVE_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, reserve_quantity_input)],
            RESERVE_START_DATE: [CallbackQueryHandler(reserve_start_date_input, pattern="^(date_start|nav_start)")],
            RESERVE_END_DATE: [CallbackQueryHandler(reserve_end_date_input, pattern="^(date_end|nav_end)")],
            RESERVE_EVENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reserve_event_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    date_check_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Остатки на дату$"), date_stock_start)],
        states={
            CHECK_DATE: [CallbackQueryHandler(date_stock_check, pattern="^(date_check|nav_check)")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    view_item_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Просмотр позиции$"), view_item_start)],
        states={
            VIEW_CATEGORY_SELECTION: [
                CallbackQueryHandler(view_category_method, pattern="^view_categories$"),
                CallbackQueryHandler(view_category_method, pattern="^view_search$"),
                CallbackQueryHandler(view_category_selection, pattern="^viewcat_")
            ],
            VIEW_ITEM_SELECTION: [CallbackQueryHandler(view_item_selection, pattern="^viewitem_")],
            SEARCH_ITEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_item_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("reminders", send_reminders))
    application.add_handler(CommandHandler("notify_all", notify_all_users))
    
    application.add_handler(add_item_conv)
    application.add_handler(reserve_conv)
    application.add_handler(date_check_conv)
    application.add_handler(view_item_conv)
    
    # Обработчики callback-запросов (регистрируем отдельно)
    application.add_handler(CallbackQueryHandler(return_selection, pattern="^ret_"))
    application.add_handler(CallbackQueryHandler(delete_selection, pattern="^del_"))
    
    # Обработчики кнопок
    application.add_handler(MessageHandler(filters.Regex("^Вернуть бронь$"), return_reservation))
    application.add_handler(MessageHandler(filters.Regex("^Удалить позицию$"), delete_item))
    application.add_handler(MessageHandler(filters.Regex("^Текущие остатки$"), current_stock))
    application.add_handler(MessageHandler(filters.Regex("^Мои бронирования$"), my_reservations))

    # Добавляем обработчик ошибок
    async def error_handler(update: Update, context: CallbackContext) -> None:
        logger.error(f"Ошибка при обработке обновления: {context.error}")
        
    application.add_error_handler(error_handler)

    # Запуск бота
    logger.info("Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()
