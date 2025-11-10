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
import asyncio
import signal
import sys

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
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/tmp/warehouse_bot.log") if os.environ.get('RENDER') else logging.FileHandler("warehouse_bot.log")
    ]
)
logger = logging.getLogger(__name__)

# Глобальная переменная для управления состоянием бота
bot_application = None

def init_db():
    """Инициализация базы данных с обработкой ошибок"""
    max_retries = 3
    retry_delay = 2
    
    for attempt in range(max_retries):
        try:
            conn = sqlite3.connect(DB_NAME, timeout=30)
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (category_id) REFERENCES categories (id)
                )
            """)
            
            # Таблица бронирований
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (item_id) REFERENCES items (id)
                )
            """)
            
            # Индексы для улучшения производительности
            cur.execute("CREATE INDEX IF NOT EXISTS idx_items_category ON items(category_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_items_name ON items(name)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_reservations_item ON reservations(item_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_reservations_dates ON reservations(start_date, end_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_reservations_user ON reservations(user_id)")
            
            # Стандартные категории
            default_categories = [
                'Ткань (и изделия из ткани)',
                'Стекло', 
                'Искусственные цветы и зелень',
                'Крупные конструкции',
                'Сезонное',
                'Фурнитура',
                'Деревянные изделия'
            ]
            
            for category in default_categories:
                cur.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category,))
            
            conn.commit()
            conn.close()
            logger.info("База данных успешно инициализирована")
            return True
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при инициализации БД (попытка {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                logger.info(f"Повторная попытка через {retry_delay} секунд...")
                time.sleep(retry_delay)
            else:
                logger.error("Не удалось инициализировать базу данных после нескольких попыток")
                return False

def migrate_database():
    """Миграция базы данных для добавления новых колонок"""
    try:
        conn = sqlite3.connect(DB_NAME, timeout=30)
        cur = conn.cursor()
        
        # Проверяем существование колонок
        cur.execute("PRAGMA table_info(reservations)")
        columns = [column[1] for column in cur.fetchall()]
        
        # Добавляем недостающие колонки
        new_columns = [
            ('user_id', 'INTEGER'),
            ('username', 'TEXT'),
            ('first_name', 'TEXT'),
            ('event_name', 'TEXT')
        ]
        
        for column_name, column_type in new_columns:
            if column_name not in columns:
                cur.execute(f"ALTER TABLE reservations ADD COLUMN {column_name} {column_type}")
                logger.info(f"Добавлена колонка {column_name}")
        
        # Добавляем timestamp колонки в items если их нет
        cur.execute("PRAGMA table_info(items)")
        item_columns = [column[1] for column in cur.fetchall()]
        
        if 'created_at' not in item_columns:
            cur.execute("ALTER TABLE items ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
            logger.info("Добавлена колонка created_at в items")
            
        if 'updated_at' not in item_columns:
            cur.execute("ALTER TABLE items ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
            logger.info("Добавлена колонка updated_at в items")
        
        conn.commit()
        logger.info("Миграция базы данных завершена успешно")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка при миграции базы данных: {e}")
        return False
    finally:
        if 'conn' in locals():
            conn.close()

def get_db_connection():
    """Создание соединения с базой данных с обработкой ошибок"""
    try:
        conn = sqlite3.connect(DB_NAME, timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")  # 30 секунд timeout
        return conn
    except sqlite3.Error as e:
        logger.error(f"Ошибка подключения к БД: {e}")
        raise

async def start(update: Update, context: CallbackContext) -> None:
    """Обработчик команды start"""
    try:
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
    except Exception as e:
        logger.error(f"Ошибка в команде start: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте еще раз.")

async def add_item_start(update: Update, context: CallbackContext) -> int:
    """Начало процесса добавления товара"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM categories ORDER BY name")
        categories = cur.fetchall()
        conn.close()
        
        if not categories:
            await update.message.reply_text("❌ Нет доступных категорий!")
            return ConversationHandler.END
        
        buttons = [
            [InlineKeyboardButton(cat[1], callback_data=f"cat_{cat[0]}")] for cat in categories
        ]
        await update.message.reply_text(
            "📁 Выберите категорию:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return CATEGORY_SELECTION
    except Exception as e:
        logger.error(f"Ошибка в add_item_start: {e}")
        await update.message.reply_text("❌ Произошла ошибка при загрузке категорий.")
        return ConversationHandler.END

async def category_selection(update: Update, context: CallbackContext) -> int:
    """Обработчик выбора категории"""
    try:
        query = update.callback_query
        await query.answer()
        category_id = int(query.data.split("_")[1])
        context.user_data["category_id"] = category_id
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT name FROM categories WHERE id = ?", (category_id,))
        result = cur.fetchone()
        conn.close()
        
        if not result:
            await query.edit_message_text("❌ Категория не найдена!")
            return ConversationHandler.END
            
        category_name = result[0]
        await query.edit_message_text(f"📁 Категория: {category_name}\n\nВведите название позиции:")
        return ITEM_NAME
    except Exception as e:
        logger.error(f"Ошибка в category_selection: {e}")
        await update.callback_query.edit_message_text("❌ Произошла ошибка при выборе категории.")
        return ConversationHandler.END

async def item_name_input(update: Update, context: CallbackContext) -> int:
    """Обработчик ввода названия товара"""
    try:
        item_name = update.message.text.strip()
        if not item_name:
            await update.message.reply_text("❌ Название не может быть пустым! Введите название:")
            return ITEM_NAME
            
        context.user_data["item_name"] = item_name
        category_id = context.user_data["category_id"]
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, quantity, image_path, comment FROM items WHERE category_id = ? AND name = ?",
            (category_id, item_name)
        )
        existing_item = cur.fetchone()
        conn.close()
        
        if existing_item:
            context.user_data["existing_item"] = existing_item
            await update.message.reply_text(
                f"✅ Позиция '{item_name}' уже существует!\n"
                f"Текущее количество: {existing_item[1]} шт.\n\n"
                "🔢 Введите количество для добавления:"
            )
            return ITEM_QUANTITY
        else:
            await update.message.reply_text("🔢 Введите количество:")
            return ITEM_QUANTITY
    except Exception as e:
        logger.error(f"Ошибка в item_name_input: {e}")
        await update.message.reply_text("❌ Произошла ошибка при обработке названия.")
        return ConversationHandler.END

async def item_quantity_input(update: Update, context: CallbackContext) -> int:
    """Обработчик ввода количества товара"""
    try:
        quantity_text = update.message.text.strip()
        if not quantity_text.isdigit():
            await update.message.reply_text("❌ Введите целое число! Введите количество:")
            return ITEM_QUANTITY
            
        quantity = int(quantity_text)
        if quantity <= 0:
            await update.message.reply_text("❌ Количество должно быть больше 0! Введите корректное количество:")
            return ITEM_QUANTITY
            
        if "existing_item" in context.user_data:
            existing_item = context.user_data["existing_item"]
            item_id, old_quantity, image_path, comment = existing_item
            new_quantity = old_quantity + quantity
            
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "UPDATE items SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_quantity, item_id)
            )
            conn.commit()
            conn.close()
            
            await update.message.reply_text(
                f"✅ Позиция обновлена!\n"
                f"📦 {context.user_data['item_name']}\n"
                f"📊 Новое количество: {new_quantity} шт."
            )
            if "existing_item" in context.user_data:
                del context.user_data["existing_item"]
            return ConversationHandler.END
        else:
            context.user_data["quantity"] = quantity
            await update.message.reply_text("📸 Загрузите фото товара (или отправьте 'пропустить' чтобы продолжить без фото):")
            return ITEM_IMAGE
            
    except Exception as e:
        logger.error(f"Ошибка в item_quantity_input: {e}")
        await update.message.reply_text("❌ Произошла ошибка при обработке количества.")
        return ConversationHandler.END

async def item_image_input(update: Update, context: CallbackContext) -> int:
    """Обработчик загрузки фото товара"""
    try:
        if update.message.text and update.message.text.lower() == 'пропустить':
            context.user_data["image_path"] = None
            await update.message.reply_text("📝 Введите комментарий к товару:")
            return ITEM_COMMENT
            
        if not update.message.photo:
            await update.message.reply_text("❌ Пожалуйста, загрузите фото или отправьте 'пропустить'!")
            return ITEM_IMAGE
        
        os.makedirs(IMAGES_DIR, exist_ok=True)
        photo_file = await update.message.photo[-1].get_file()
        timestamp = int(datetime.now().timestamp())
        image_path = os.path.join(IMAGES_DIR, f"{timestamp}.jpg")
        
        await photo_file.download_to_drive(image_path)
        context.user_data["image_path"] = image_path
        
        await update.message.reply_text("📝 Введите комментарий к товару:")
        return ITEM_COMMENT
    except Exception as e:
        logger.error(f"Ошибка в item_image_input: {e}")
        await update.message.reply_text("❌ Произошла ошибка при загрузке фото.")
        return ConversationHandler.END

async def item_comment_input(update: Update, context: CallbackContext) -> int:
    """Обработчик ввода комментария к товару"""
    try:
        comment = update.message.text
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO items (category_id, name, quantity, image_path, comment) VALUES (?, ?, ?, ?, ?)",
            (
                context.user_data["category_id"],
                context.user_data["item_name"],
                context.user_data["quantity"],
                context.user_data.get("image_path"),
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
    except Exception as e:
        logger.error(f"Ошибка в item_comment_input: {e}")
        await update.message.reply_text("❌ Произошла ошибка при сохранении позиции.")
        return ConversationHandler.END

# Функции для календаря
def generate_calendar(year=None, month=None, selection_type="start"):
    """Генерирует inline-клавиатуру с календарем"""
    try:
        now = datetime.now()
        if year is None:
            year = now.year
        if month is None:
            month = now.month
        
        cal = calendar.monthcalendar(year, month)
        month_name = calendar.month_name[month]
        
        keyboard = []
        
        header = [InlineKeyboardButton(f"{month_name} {year}", callback_data="ignore")]
        keyboard.append(header)
        
        week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        keyboard.append([InlineKeyboardButton(day, callback_data="ignore") for day in week_days])
        
        for week in cal:
            week_buttons = []
            for day in week:
                if day == 0:
                    week_buttons.append(InlineKeyboardButton(" ", callback_data="ignore"))
                else:
                    date_str = f"{year}-{month:02d}-{day:02d}"
                    week_buttons.append(InlineKeyboardButton(str(day), callback_data=f"date_{selection_type}_{date_str}"))
            keyboard.append(week_buttons)
        
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
    except Exception as e:
        logger.error(f"Ошибка в generate_calendar: {e}")
        return InlineKeyboardMarkup([])

# Функции для бронирования
async def reserve_item_start(update: Update, context: CallbackContext) -> int:
    """Начало процесса бронирования"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT i.id, i.name, c.name, i.quantity
            FROM items i 
            JOIN categories c ON i.category_id = c.id
            WHERE i.quantity > 0
            ORDER BY c.name, i.name
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
    except Exception as e:
        logger.error(f"Ошибка в reserve_item_start: {e}")
        await update.message.reply_text("❌ Произошла ошибка при загрузке позиций.")
        return ConversationHandler.END

async def reserve_item_selection(update: Update, context: CallbackContext) -> int:
    """Обработчик выбора товара для бронирования"""
    try:
        query = update.callback_query
        await query.answer()
        item_id = int(query.data.split("_")[1])
        context.user_data["reserve_item_id"] = item_id
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT i.name, c.name, i.quantity 
            FROM items i 
            JOIN categories c ON i.category_id = c.id 
            WHERE i.id = ?
        """, (item_id,))
        result = cur.fetchone()
        conn.close()
        
        if not result:
            await query.edit_message_text("❌ Товар не найден!")
            return ConversationHandler.END
            
        item_name, category_name, quantity = result
        context.user_data["current_quantity"] = quantity
        await query.edit_message_text(
            f"📦 Товар: {category_name} - {item_name}\n"
            f"📊 Доступно: {quantity} шт.\n\n"
            "Введите количество для бронирования:"
        )
        return RESERVE_QUANTITY
    except Exception as e:
        logger.error(f"Ошибка в reserve_item_selection: {e}")
        await update.callback_query.edit_message_text("❌ Произошла ошибка при выборе товара.")
        return ConversationHandler.END

async def reserve_quantity_input(update: Update, context: CallbackContext) -> int:
    """Обработчик ввода количества для бронирования"""
    try:
        quantity_text = update.message.text.strip()
        if not quantity_text.isdigit():
            await update.message.reply_text("❌ Введите целое число! Введите количество:")
            return RESERVE_QUANTITY
            
        reserve_quantity = int(quantity_text)
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
        
        await update.message.reply_text(
            "📅 Выберите дату НАЧАЛА бронирования:",
            reply_markup=generate_calendar(selection_type="start")
        )
        return RESERVE_START_DATE
    except Exception as e:
        logger.error(f"Ошибка в reserve_quantity_input: {e}")
        await update.message.reply_text("❌ Произошла ошибка при обработке количества.")
        return ConversationHandler.END

async def reserve_start_date_input(update: Update, context: CallbackContext) -> int:
    """Обработчик выбора даты начала бронирования"""
    try:
        query = update.callback_query
        await query.answer()
        
        if query.data.startswith("nav_start"):
            _, _, year, month = query.data.split("_")
            await query.edit_message_text(
                "📅 Выберите дату НАЧАЛА бронирования:",
                reply_markup=generate_calendar(int(year), int(month), "start")
            )
            return RESERVE_START_DATE
        
        elif query.data.startswith("date_start"):
            _, _, date_str = query.data.split("_")
            start_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            today = datetime.now().date()
            
            if start_date < today:
                await query.answer("❌ Дата начала не может быть в прошлом!", show_alert=True)
                return RESERVE_START_DATE
            
            context.user_data["reserve_start_date"] = start_date.isoformat()
            
            await query.edit_message_text(
                "📅 Выберите дату ОКОНЧАНИЯ бронирования:",
                reply_markup=generate_calendar(selection_type="end")
            )
            return RESERVE_END_DATE
        
        return RESERVE_START_DATE
    except Exception as e:
        logger.error(f"Ошибка в reserve_start_date_input: {e}")
        await update.callback_query.edit_message_text("❌ Произошла ошибка при выборе даты.")
        return ConversationHandler.END

async def reserve_end_date_input(update: Update, context: CallbackContext) -> int:
    """Обработчик выбора даты окончания бронирования"""
    try:
        query = update.callback_query
        await query.answer()
        
        if query.data.startswith("nav_end"):
            _, _, year, month = query.data.split("_")
            await query.edit_message_text(
                "📅 Выберите дату ОКОНЧАНИЯ бронирования:",
                reply_markup=generate_calendar(int(year), int(month), "end")
            )
            return RESERVE_END_DATE
        
        elif query.data.startswith("date_end"):
            _, _, date_str = query.data.split("_")
            end_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            start_date = datetime.fromisoformat(context.user_data["reserve_start_date"]).date()
            
            if end_date <= start_date:
                await query.answer("❌ Дата окончания должна быть после даты начала!", show_alert=True)
                return RESERVE_END_DATE
            
            context.user_data["reserve_end_date"] = end_date.isoformat()
            
            await query.edit_message_text(
                "🎯 Введите название мероприятия или комментарий к бронированию:"
            )
            return RESERVE_EVENT
        
        return RESERVE_END_DATE
    except Exception as e:
        logger.error(f"Ошибка в reserve_end_date_input: {e}")
        await update.callback_query.edit_message_text("❌ Произошла ошибка при выборе даты.")
        return ConversationHandler.END

async def reserve_event_input(update: Update, context: CallbackContext) -> int:
    """Обработчик ввода названия мероприятия"""
    try:
        event_name = update.message.text.strip()
        if not event_name:
            event_name = "Без названия"
            
        context.user_data["reserve_event"] = event_name
        
        item_id = context.user_data["reserve_item_id"]
        reserve_quantity = context.user_data["reserve_quantity"]
        start_date = datetime.fromisoformat(context.user_data["reserve_start_date"]).date()
        end_date = datetime.fromisoformat(context.user_data["reserve_end_date"]).date()
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("SELECT quantity, name FROM items WHERE id = ?", (item_id,))
        result = cur.fetchone()
        if not result:
            await update.message.reply_text("❌ Товар не найден!")
            return ConversationHandler.END
            
        total_quantity, item_name = result
        
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
            return RESERVE_QUANTITY
        
        user = update.effective_user
        user_id = user.id
        username = f"@{user.username}" if user.username else user.first_name or "Пользователь"
        first_name = user.first_name or ""
        
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

# Функции для возврата брони
async def return_reservation(update: Update, context: CallbackContext) -> None:
    """Показ активных бронирований для возврата"""
    try:
        conn = get_db_connection()
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
            display_text = f"{cat_name} - {item_name} ({quantity}шт) {start_date} - {end_date}"
            if len(display_text) > 60:
                display_text = display_text[:57] + "..."
            buttons.append([InlineKeyboardButton(
                display_text, 
                callback_data=f"ret_{res_id}"
            )])
        
        await update.message.reply_text(
            "📦 Выберите бронь для возврата:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception as e:
        logger.error(f"Ошибка в return_reservation: {e}")
        await update.message.reply_text("❌ Произошла ошибка при загрузке бронирований.")

async def return_selection(update: Update, context: CallbackContext) -> None:
    """Обработчик возврата брони"""
    try:
        query = update.callback_query
        await query.answer()
        reserve_id = int(query.data.split("_")[1])
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT i.name, r.username, r.event_name 
            FROM reservations r 
            JOIN items i ON r.item_id = i.id 
            WHERE r.id = ?
        """, (reserve_id,))
        result = cur.fetchone()
        
        if not result:
            await query.edit_message_text("❌ Бронь не найдена!")
            conn.close()
            return
            
        item_name, username, event_name = result
        
        cur.execute("DELETE FROM reservations WHERE id = ?", (reserve_id,))
        conn.commit()
        conn.close()
        
        event_text = f" для мероприятия '{event_name}'" if event_name else ""
        await query.edit_message_text(f"✅ Бронь '{item_name}'{event_text} от {username} успешно возвращена!")
    except Exception as e:
        logger.error(f"Ошибка в return_selection: {e}")
        await update.callback_query.edit_message_text("❌ Произошла ошибка при возврате брони.")

# Функции для удаления позиции
async def delete_item(update: Update, context: CallbackContext) -> None:
    """Показ позиций для удаления"""
    try:
        conn = get_db_connection()
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
            display_text = f"{cat_name} - {item_name} ({quantity}шт)"
            if len(display_text) > 60:
                display_text = display_text[:57] + "..."
            buttons.append([InlineKeyboardButton(
                display_text, 
                callback_data=f"del_{item_id}"
            )])
        
        await update.message.reply_text(
            "🗑️ Выберите позицию для удаления:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception as e:
        logger.error(f"Ошибка в delete_item: {e}")
        await update.message.reply_text("❌ Произошла ошибка при загрузке позиций.")

async def delete_selection(update: Update, context: CallbackContext) -> None:
    """Обработчик удаления позиции"""
    try:
        query = update.callback_query
        await query.answer()
        item_id = int(query.data.split("_")[1])
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("SELECT name, image_path FROM items WHERE id = ?", (item_id,))
        result = cur.fetchone()
        
        if not result:
            await query.edit_message_text("❌ Позиция не найдена!")
            conn.close()
            return
            
        item_name, image_path = result
        
        if image_path and os.path.exists(image_path):
            try:
                os.remove(image_path)
            except Exception as e:
                logger.error(f"Ошибка при удалении изображения: {e}")
        
        cur.execute("DELETE FROM reservations WHERE item_id = ?", (item_id,))
        cur.execute("DELETE FROM items WHERE id = ?", (item_id,))
        conn.commit()
        conn.close()
        
        await query.edit_message_text(f"✅ Позиция '{item_name}' успешно удалена!")
    except Exception as e:
        logger.error(f"Ошибка в delete_selection: {e}")
        await update.callback_query.edit_message_text("❌ Произошла ошибка при удалении позиции.")

# Функции для просмотра остатков
async def current_stock(update: Update, context: CallbackContext) -> None:
    """Показ текущих остатков"""
    try:
        conn = get_db_connection()
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
        
        # Разбиваем длинные сообщения на части
        if len(response) > 4096:
            parts = [response[i:i+4096] for i in range(0, len(response), 4096)]
            for part in parts:
                await update.message.reply_text(part)
        else:
            await update.message.reply_text(response)
    except Exception as e:
        logger.error(f"Ошибка в current_stock: {e}")
        await update.message.reply_text("❌ Произошла ошибка при загрузке остатков.")

async def date_stock_start(update: Update, context: CallbackContext) -> int:
    """Начало проверки остатков на дату"""
    try:
        await update.message.reply_text(
            "📅 Выберите дату для проверки остатков:",
            reply_markup=generate_calendar(selection_type="check")
        )
        return CHECK_DATE
    except Exception as e:
        logger.error(f"Ошибка в date_stock_start: {e}")
        await update.message.reply_text("❌ Произошла ошибка.")
        return ConversationHandler.END

async def date_stock_check(update: Update, context: CallbackContext) -> int:
    """Проверка остатков на выбранную дату"""
    try:
        query = update.callback_query
        await query.answer()
        
        if query.data.startswith("nav_check"):
            _, _, year, month = query.data.split("_")
            await query.edit_message_text(
                "📅 Выберите дату для проверки остатков:",
                reply_markup=generate_calendar(int(year), int(month), "check")
            )
            return CHECK_DATE
        
        elif query.data.startswith("date_check"):
            _, _, date_str = query.data.split("_")
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            current_date = datetime.now().date()
            
            if target_date < current_date:
                await query.answer("❌ Дата не может быть в прошлом!", show_alert=True)
                return CHECK_DATE
                
            conn = get_db_connection()
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
                available = max(0, qty)
                response += f"  • {name}: {available}шт\n"
            
            await query.edit_message_text(response)
            return ConversationHandler.END
        
        return CHECK_DATE
    except Exception as e:
        logger.error(f"Ошибка в date_stock_check: {e}")
        await update.callback_query.edit_message_text("❌ Произошла ошибка при проверке остатков.")
        return ConversationHandler.END

# Улучшенные функции для просмотра позиции
async def view_item_start(update: Update, context: CallbackContext) -> int:
    """Начало просмотра позиции"""
    try:
        buttons = [
            [InlineKeyboardButton("📁 Поиск по категориям", callback_data="view_categories")],
            [InlineKeyboardButton("🔍 Поиск по названию", callback_data="view_search")],
        ]
        
        await update.message.reply_text(
            "🔍 Выберите способ поиска позиции:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return VIEW_CATEGORY_SELECTION
    except Exception as e:
        logger.error(f"Ошибка в view_item_start: {e}")
        await update.message.reply_text("❌ Произошла ошибка.")
        return ConversationHandler.END

async def view_category_method(update: Update, context: CallbackContext) -> int:
    """Обработчик выбора метода просмотра"""
    try:
        query = update.callback_query
        await query.answer()
        
        if query.data == "view_categories":
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT id, name FROM categories ORDER BY name")
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
            
    except Exception as e:
        logger.error(f"Ошибка в view_category_method: {e}")
        await update.callback_query.edit_message_text("❌ Произошла ошибка.")
        return ConversationHandler.END

async def view_category_selection(update: Update, context: CallbackContext) -> int:
    """Обработчик выбора категории для просмотра"""
    try:
        query = update.callback_query
        await query.answer()
        category_id = int(query.data.split("_")[1])
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT i.id, i.name, i.quantity 
            FROM items i 
            WHERE i.category_id = ?
            ORDER BY i.name
        """, (category_id,))
        items = cur.fetchall()
        
        cur.execute("SELECT name FROM categories WHERE id = ?", (category_id,))
        result = cur.fetchone()
        conn.close()
        
        if not result:
            await query.edit_message_text("❌ Категория не найдена!")
            return ConversationHandler.END
            
        category_name = result[0]
        
        if not items:
            await query.edit_message_text(f"❌ В категории '{category_name}' нет позиций!")
            return ConversationHandler.END
        
        buttons = []
        for item_id, item_name, quantity in items:
            display_text = f"{item_name} ({quantity}шт)"
            if len(display_text) > 60:
                display_text = display_text[:57] + "..."
            buttons.append([InlineKeyboardButton(
                display_text, 
                callback_data=f"viewitem_{item_id}"
            )])
        
        await query.edit_message_text(
            f"📁 Категория: {category_name}\n\n"
            "📦 Выберите позицию для просмотра:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return VIEW_ITEM_SELECTION
    except Exception as e:
        logger.error(f"Ошибка в view_category_selection: {e}")
        await update.callback_query.edit_message_text("❌ Произошла ошибка.")
        return ConversationHandler.END

async def search_item_input(update: Update, context: CallbackContext) -> int:
    """Обработчик поиска товара по названию"""
    try:
        search_term = update.message.text.strip()
        if not search_term:
            await update.message.reply_text("❌ Введите поисковый запрос!")
            return SEARCH_ITEM
        
        conn = get_db_connection()
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
            display_text = f"{category_name} - {item_name} ({quantity}шт)"
            if len(display_text) > 60:
                display_text = display_text[:57] + "..."
            buttons.append([InlineKeyboardButton(
                display_text, 
                callback_data=f"viewitem_{item_id}"
            )])
        
        await update.message.reply_text(
            f"🔍 Результаты поиска по '{search_term}':\n\n"
            "📦 Выберите позицию для просмотра:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return VIEW_ITEM_SELECTION
    except Exception as e:
        logger.error(f"Ошибка в search_item_input: {e}")
        await update.message.reply_text("❌ Произошла ошибка при поиске.")
        return ConversationHandler.END

async def view_item_selection(update: Update, context: CallbackContext) -> int:
    """Просмотр детальной информации о позиции"""
    try:
        query = update.callback_query
        await query.answer()
        
        if not query.data.startswith("viewitem_"):
            await query.edit_message_text("❌ Ошибка при выборе позиции!")
            return ConversationHandler.END
            
        item_id = int(query.data.split("_")[1])
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT i.name, c.name, i.quantity, i.comment, i.image_path
            FROM items i 
            JOIN categories c ON i.category_id = c.id 
            WHERE i.id = ?
        """, (item_id,))
        item_info = cur.fetchone()
        
        if not item_info:
            await query.edit_message_text("❌ Позиция не найдена!")
            conn.close()
            return ConversationHandler.END
        
        item_name, category_name, quantity, comment, image_path = item_info
        
        cur.execute("""
            SELECT start_date, end_date, quantity, username, event_name
            FROM reservations 
            WHERE item_id = ? AND end_date >= date('now')
            ORDER BY start_date
        """, (item_id,))
        reservations = cur.fetchall()
        
        conn.close()
        
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
    except Exception as e:
        logger.error(f"Ошибка в view_item_selection: {e}")
        await update.callback_query.edit_message_text("❌ Произошла ошибка при загрузке информации.")
        return ConversationHandler.END

# Функция для просмотра своих бронирований
async def my_reservations(update: Update, context: CallbackContext) -> None:
    """Показ бронирований текущего пользователя"""
    try:
        user = update.effective_user
        user_id = user.id
        
        conn = get_db_connection()
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
        
        if len(response) > 4096:
            parts = [response[i:i+4096] for i in range(0, len(response), 4096)]
            for part in parts:
                await update.message.reply_text(part)
        else:
            await update.message.reply_text(response)
    except Exception as e:
        logger.error(f"Ошибка в my_reservations: {e}")
        await update.message.reply_text("❌ Произошла ошибка при загрузке бронирований.")

# Функция для отправки напоминаний
async def send_reminders(update: Update, context: CallbackContext) -> None:
    """Отправка напоминаний о бронированиях"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT r.id, i.name, r.end_date, r.user_id, r.username, r.event_name
            FROM reservations r 
            JOIN items i ON r.item_id = i.id
            WHERE r.end_date <= date('now', '+3 days') AND r.end_date >= date('now')
            ORDER BY r.end_date
        """)
        ending_reservations = cur.fetchall()
        
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
    except Exception as e:
        logger.error(f"Ошибка в send_reminders: {e}")
        await update.message.reply_text("❌ Произошла ошибка при загрузке напоминаний.")

# Функция для отправки уведомлений всем пользователям
async def notify_all_users(update: Update, context: CallbackContext) -> None:
    """Отправка уведомлений всем пользователям"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT DISTINCT user_id, username
            FROM reservations 
            WHERE end_date >= date('now') AND user_id IS NOT NULL
        """)
        users = cur.fetchall()
        
        notified_count = 0
        for user_id, username in users:
            try:
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
                    
                    await context.bot.send_message(chat_id=user_id, text=message)
                    notified_count += 1
                    await asyncio.sleep(0.1)  # Задержка чтобы не превысить лимиты Telegram
                    
            except Exception as e:
                logger.error(f"Ошибка при отправке уведомления пользователю {username}: {e}")
        
        conn.close()
        
        await update.message.reply_text(f"✅ Уведомления отправлены {notified_count} пользователям!")
    except Exception as e:
        logger.error(f"Ошибка в notify_all_users: {e}")
        await update.message.reply_text("❌ Произошла ошибка при отправке уведомлений.")

async def cancel(update: Update, context: CallbackContext) -> int:
    """Отмена текущей операции"""
    await update.message.reply_text("❌ Операция отменена.")
    return ConversationHandler.END

async def help_command(update: Update, context: CallbackContext) -> None:
    """Справка по командам бота"""
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

async def error_handler(update: Update, context: CallbackContext) -> None:
    """Обработчик ошибок"""
    logger.error(f"Ошибка при обработке обновления: {context.error}", exc_info=context.error)
    
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ Произошла непредвиденная ошибка. Пожалуйста, попробуйте еще раз или обратитесь к администратору."
            )
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения об ошибке: {e}")

def setup_application():
    """Настройка и создание приложения"""
    application = Application.builder().token(TOKEN).build()

    # Обработчики диалогов
    add_item_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Добавить позицию$"), add_item_start)],
        states={
            CATEGORY_SELECTION: [CallbackQueryHandler(category_selection, pattern="^cat_")],
            ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, item_name_input)],
            ITEM_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, item_quantity_input)],
            ITEM_IMAGE: [
                MessageHandler(filters.PHOTO, item_image_input),
                MessageHandler(filters.TEXT & ~filters.COMMAND, item_image_input)
            ],
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
    
    # Обработчики callback-запросов
    application.add_handler(CallbackQueryHandler(return_selection, pattern="^ret_"))
    application.add_handler(CallbackQueryHandler(delete_selection, pattern="^del_"))
    
    # Обработчики кнопок
    application.add_handler(MessageHandler(filters.Regex("^Вернуть бронь$"), return_reservation))
    application.add_handler(MessageHandler(filters.Regex("^Удалить позицию$"), delete_item))
    application.add_handler(MessageHandler(filters.Regex("^Текущие остатки$"), current_stock))
    application.add_handler(MessageHandler(filters.Regex("^Мои бронирования$"), my_reservations))

    # Обработчик ошибок
    application.add_error_handler(error_handler)
    
    return application

def signal_handler(signum, frame):
    """Обработчик сигналов для graceful shutdown"""
    logger.info(f"Получен сигнал {signum}. Завершение работы...")
    if bot_application:
        bot_application.stop()
    sys.exit(0)

def main() -> None:
    """Основная функция запуска бота"""
    global bot_application
    
    # Регистрация обработчиков сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Создаем папку для изображений
    os.makedirs(IMAGES_DIR, exist_ok=True)
    
    logger.info("Инициализация базы данных...")
    if not init_db():
        logger.error("Не удалось инициализировать базу данных. Завершение работы.")
        return
    
    logger.info("Выполнение миграции базы данных...")
    migrate_database()
    
    logger.info("Настройка приложения...")
    try:
        bot_application = setup_application()
        
        logger.info("Бот запущен...")
        bot_application.run_polling(
            poll_interval=1.0,
            timeout=30,
            drop_pending_updates=True
        )
    except Exception as e:
        logger.error(f"Критическая ошибка при запуске бота: {e}")
    finally:
        logger.info("Бот остановлен.")

if __name__ == "__main__":
    main()
