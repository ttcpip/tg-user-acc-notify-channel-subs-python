import asyncio
import os
import logging
import aiosqlite
from datetime import datetime, timezone
import signal
import sys

from telethon import TelegramClient, events, errors
from dotenv import load_dotenv

# Загружаем .env
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

# Читаем переменные из .env
API_ID = int(os.getenv("TG_API_ID", "123456")
             )            # int, например 123456
# str, например "abcdef0123456789..."
API_HASH = os.getenv("TG_API_HASH", "ABC123...")
BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "123456:ABC-...")   # Токен бота
# Чат, из которого принимаем команды и куда слать уведомления
ADMIN_CHAT_ID = int(os.getenv("TG_ADMIN_CHAT_ID", "0"))

# Названия (или пути к) файлов/строк-сессий
SESSION_NAME_USER = os.getenv("TG_USER_SESSION_NAME", "user_account.session")
SESSION_NAME_BOT = os.getenv("TG_BOT_SESSION_NAME", "bot_account.session")

# Настройки polling
POLLING_INTERVAL_SECONDS = int(
    os.getenv("POLLING_INTERVAL_SECONDS", "60"))  # Интервал в секундах
# Путь к SQLite базе данных
DATABASE_PATH = os.getenv("DATABASE_PATH", "telegram_bot.db")

# Глобальные переменные
user_client = None
bot = None
current_channel_id = None
db = None  # Экземпляр базы данных aiosqlite

# ------------------------------------------------------------------------------
# DATABASE HANDLING
# ------------------------------------------------------------------------------


async def init_db():
    """Инициализирует базу данных и создаёт необходимые таблицы."""
    global db
    db = await aiosqlite.connect(DATABASE_PATH)
    await db.execute("""
    CREATE TABLE IF NOT EXISTS channel (
        id INTEGER PRIMARY KEY,
        tg_id INTEGER UNIQUE,
        name TEXT,
        username TEXT
    )
    """)

    await db.execute("""
    CREATE TABLE IF NOT EXISTS subscribers (
        user_tg_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT
    )
    """)

    await db.execute("""
    CREATE TABLE IF NOT EXISTS actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_tg_id INTEGER,
        user_tg_username TEXT,
        user_tg_name TEXT,
        user_tg_surname TEXT,
        action TEXT CHECK(action IN ('SUBSCRIBED', 'UNSUBSCRIBED')),
        time_utc TEXT,
        channel_tg_id INTEGER
    )
    """)

    await db.commit()


async def get_tracked_channel():
    """Получает информацию о текущем отслеживаемом канале."""
    async with db.execute("SELECT tg_id, name, username FROM channel LIMIT 1") as cursor:
        row = await cursor.fetchone()
    if row:
        return {'tg_id': row[0], 'name': row[1], 'username': row[2]}
    return None


async def set_tracked_channel(tg_id, name, username):
    """Устанавливает отслеживаемый канал, очищает предыдущих подписчиков."""
    await db.execute("DELETE FROM channel")
    await db.execute("INSERT INTO channel (tg_id, name, username) VALUES (?, ?, ?)", (tg_id, name, username))
    # Очистка предыдущих подписчиков
    await db.execute("DELETE FROM subscribers")
    await db.commit()


async def get_stored_subscribers():
    """Возвращает список всех подписчиков из базы данных."""
    async with db.execute("SELECT user_tg_id, username, first_name, last_name FROM subscribers") as cursor:
        rows = await cursor.fetchall()
    return {row[0]: {'username': row[1], 'first_name': row[2], 'last_name': row[3]} for row in rows}


async def add_subscriber(user):
    """Добавляет нового подписчика в базу данных."""
    await db.execute("""
    INSERT OR IGNORE INTO subscribers (user_tg_id, username, first_name, last_name)
    VALUES (?, ?, ?, ?)
    """, (user.id, user.username, user.first_name, user.last_name))
    await db.commit()


async def remove_subscriber(user_tg_id):
    """Удаляет подписчика из базы данных."""
    await db.execute("DELETE FROM subscribers WHERE user_tg_id = ?", (user_tg_id,))
    await db.commit()


async def log_action(user_tg_id, username, first_name, last_name, action, channel_tg_id):
    """Логирует действие подписчика в таблицу actions."""
    time_utc = datetime.now(timezone.utc).isoformat()
    await db.execute("""
    INSERT INTO actions (user_tg_id, user_tg_username, user_tg_name, user_tg_surname, action, time_utc, channel_tg_id)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_tg_id, username, first_name, last_name, action, time_utc, channel_tg_id))
    await db.commit()

# ------------------------------------------------------------------------------
# ИНИЦИАЛИЗАЦИЯ
# ------------------------------------------------------------------------------


async def init_clients():
    """
    Инициализирует бота и клиентский аккаунт (пользовательский).
    """
    global user_client, bot, current_channel_id

    # Инициализация базы данных
    await init_db()

    # Бот
    bot = TelegramClient(
        SESSION_NAME_BOT,
        API_ID,
        API_HASH
    )
    await bot.start(bot_token=BOT_TOKEN)
    logging.info("Бот успешно запущен.")

    # Пользовательский клиент
    user_client = TelegramClient(
        SESSION_NAME_USER,
        API_ID,
        API_HASH
    )
    # user_client не запускаем сразу через start(phone=...),
    # потому что авторизация будет через /login
    await user_client.connect()
    logging.info("Пользовательский клиент подключён.")

    # Получаем текущий канал из базы данных
    tracked_channel = await get_tracked_channel()
    if tracked_channel:
        current_channel_id = tracked_channel['tg_id']
        logging.info(
            f"Отслеживаемый канал: {tracked_channel['name']} (@{tracked_channel['username']}) id:{tracked_channel['tg_id']}")
    else:
        logging.info(
            "Нет отслеживаемого канала. Используйте /setchannel для установки.")

# ------------------------------------------------------------------------------
# ПОЛЛИНГ
# ------------------------------------------------------------------------------


async def polling_task():
    """
    Фоновая задача, которая периодически проверяет подписчиков канала.
    """
    while True:
        try:
            if current_channel_id is None:
                await asyncio.sleep(POLLING_INTERVAL_SECONDS)
                continue

            if not user_client.is_connected() or not (await user_client.is_user_authorized()):
                logging.warning(
                    "Пользовательский клиент не авторизован. Пропуск polling.")
                await asyncio.sleep(POLLING_INTERVAL_SECONDS)
                continue

            # Получаем текущий список подписчиков
            participants = await user_client.get_participants(current_channel_id)
            current_subscribers = {user.id: user for user in participants}
            total_subscribers = len(current_subscribers)  # Get total count

            # Получаем предыдущий список подписчиков из базы данных
            stored_subscribers = await get_stored_subscribers()

            # Вычисляем новых подписчиков и ушедших
            new_subscribers = {uid: user for uid, user in current_subscribers.items(
            ) if uid not in stored_subscribers}
            unsubscribed = {uid: info for uid, info in stored_subscribers.items(
            ) if uid not in current_subscribers}

            # Отправляем уведомления о новых подписках
            for uid, user in new_subscribers.items():
                username = user.username or 'no_username'
                first_name = user.first_name or ''
                last_name = user.last_name or 'no_surname'
                channel_info = await get_tracked_channel()
                channel_username = channel_info['username'] if channel_info and channel_info['username'] else 'no_username'
                channel_name = channel_info['name'] if channel_info else 'unknown_channel'

                msg = (f"Зафиксирована ПОДПИСКА на канал @{channel_username}, "
                       f"пользователь: @{username} {first_name} {last_name} (id{uid})\n"
                       f"Всего подписчиков: {total_subscribers}")

                if ADMIN_CHAT_ID != 0:
                    try:
                        await bot.send_message(ADMIN_CHAT_ID, msg)
                        logging.info(
                            f"Отправлено уведомление о подписке пользователя id{uid}")
                    except Exception as e:
                        logging.error(
                            f"Не удалось отправить уведомление о подписке: {e}")

                # Добавляем нового подписчика в базу данных
                await add_subscriber(user)

                # Логируем действие
                await log_action(uid, username, first_name, last_name, "SUBSCRIBED", current_channel_id)

            # Отправляем уведомления об отписках
            for uid, info in unsubscribed.items():
                username = info['username'] or 'no_username'
                first_name = info['first_name'] or ''
                last_name = info['last_name'] or 'no_surname'
                channel_info = await get_tracked_channel()
                channel_username = channel_info['username'] if channel_info and channel_info['username'] else 'no_username'
                channel_name = channel_info['name'] if channel_info else 'unknown_channel'

                msg = (f"Зафиксирована ОТПИСКА от канала @{channel_username}, "
                       f"пользователь: @{username} {first_name} {last_name} (id{uid})\n"
                       f"Всего подписчиков: {total_subscribers}")

                if ADMIN_CHAT_ID != 0:
                    try:
                        await bot.send_message(ADMIN_CHAT_ID, msg)
                        logging.info(
                            f"Отправлено уведомление об отписке пользователя id{uid}")
                    except Exception as e:
                        logging.error(
                            f"Не удалось отправить уведомление об отписке: {e}")

                # Удаляем подписчика из базы данных
                await remove_subscriber(uid)

                # Логируем действие
                await log_action(uid, username, first_name, last_name, "UNSUBSCRIBED", current_channel_id)

        except Exception as e:
            logging.error(f"Ошибка в polling_task: {e}")

        await asyncio.sleep(POLLING_INTERVAL_SECONDS)
# ------------------------------------------------------------------------------
# ПРОВЕРКА ДОСТУПА К КОМАНДАМ
# ------------------------------------------------------------------------------


def admin_only(func):
    """
    Декоратор, который проверяет, что команда пришла из ADMIN_CHAT_ID.
    Если нет, то игнорируем.
    """
    async def wrapper(event):
        if event.chat_id != ADMIN_CHAT_ID:
            # Игнорируем любые команды, пришедшие не из ADMIN_CHAT_ID
            logging.warning(
                f"Команда от неавторизованного чата: {event.chat_id}")
            return
        return await func(event)
    return wrapper

# ------------------------------------------------------------------------------
# КОМАНДЫ БОТА
# ------------------------------------------------------------------------------


@events.register(events.NewMessage(pattern=r'^/start$'))
@admin_only
async def cmd_start(event):
    text = (
        "Привет! Я бот для отслеживания подписок/отписок канала.\n\n"
        "Доступные команды:\n"
        "/login – Войти в аккаунт (интерактивно)\n"
        "/logout – Выйти из аккаунта\n"
        "/status – Проверить, авторизован ли аккаунт\n"
        "/setchannel <ID> – Установить ID канала\n"
        "/getchannelid <@username> – Получить numeric ID канала по его username\n"
        "/subcount – Узнать, сколько подписчиков\n"
        "/viewchannel – Просмотреть текущий отслеживаемый канал\n"
        "/id – Узнать текущий chat_id (или user_id)\n"
    )
    await event.respond(text)


@events.register(events.NewMessage(pattern=r'^/login$'))
@admin_only
async def cmd_login(event):
    """
    /login — Запрашиваем номер телефона и код для авторизации.
    Используем conversation с ручной проверкой.
    """
    global user_client

    # Если уже авторизованы
    if user_client.is_connected() and (await user_client.is_user_authorized()):
        await event.respond("Аккаунт уже авторизован.")
        return

    # Начинаем "conversation"
    async with bot.conversation(event.chat_id, exclusive=False, timeout=300) as conv:
        await conv.send_message(
            "Окей, давайте авторизуемся. Введите номер телефона, начиная с '+':"
        )

        # 1) Получаем телефон
        while True:
            try:
                phone_event = await conv.get_response()
            except asyncio.TimeoutError:
                await conv.send_message("Время ожидания истекло. Попробуйте заново /login.")
                return

            phone_number = phone_event.raw_text.strip()
            if phone_number.startswith('+'):
                break
            else:
                await conv.send_message("Номер должен начинаться с '+'. Попробуйте ещё раз.")

        try:
            # Подключаемся к клиенту
            await user_client.connect()

            # Если не авторизованы — запрашиваем код
            if not await user_client.is_user_authorized():
                await user_client.send_code_request(phone_number)
                await conv.send_message(
                    f"Код отправлен на номер {phone_number}. Введите код (только цифры):"
                )

                # 2) Получаем код (проверяем, что введены только цифры)
                while True:
                    try:
                        code_event = await conv.get_response()
                    except asyncio.TimeoutError:
                        await conv.send_message("Время ожидания истекло. Попробуйте заново /login.")
                        return

                    code = code_event.raw_text.strip()
                    if code.isdigit():
                        break
                    else:
                        await conv.send_message("Код должен состоять только из цифр. Повторите ввод.")

                # Пытаемся войти
                try:
                    await user_client.sign_in(phone_number, code)
                    await conv.send_message("Успешно авторизовались!")
                except errors.SessionPasswordNeededError:
                    # Если включена 2FA, просим пароль
                    await conv.send_message("Введите пароль (2FA):")
                    try:
                        pass_event = await conv.get_response()
                    except asyncio.TimeoutError:
                        await conv.send_message("Время ожидания истекло. Попробуйте заново /login.")
                        return

                    password_2fa = pass_event.raw_text.strip()
                    await user_client.sign_in(password=password_2fa)
                    await conv.send_message("Успешно авторизовались (с 2FA)!")
            else:
                await conv.send_message("Уже авторизовано.")

        except Exception as ex:
            await conv.send_message(f"Ошибка при авторизации: {ex}")


@events.register(events.NewMessage(pattern=r'^/logout$'))
@admin_only
async def cmd_logout(event):
    """
    /logout — Разлогинивает аккаунт и удаляет сохранённую сессию.
    """
    global user_client

    if user_client.is_connected() and await user_client.is_user_authorized():
        await user_client.log_out()
        await user_client.disconnect()
        await event.respond("Аккаунт разлогинен. Сессионный файл останется на сервере (при необходимости удалите вручную).")
    else:
        await event.respond("Аккаунт не авторизован (или уже разлогинен).")


@events.register(events.NewMessage(pattern=r'^/status$'))
@admin_only
async def cmd_status(event):
    """
    /status — Узнать, авторизован ли аккаунт
    """
    global user_client

    if user_client.is_connected() and (await user_client.is_user_authorized()):
        me = await user_client.get_me()
        await event.respond(f"Сейчас вошли под аккаунтом: {me.first_name} (id: {me.id})")
    else:
        await event.respond("Сейчас аккаунт не авторизован.")


@events.register(events.NewMessage(pattern=r'^/setchannel\s+(\-?\d+)$'))
@admin_only
async def cmd_setchannel(event):
    """
    /setchannel <ID> — Устанавливаем канал, за которым следим
    """
    global current_channel_id
    channel_id = event.pattern_match.group(1)
    try:
        channel_id = int(channel_id)
    except ValueError:
        await event.respond("Неверный формат ID канала. Убедитесь, что вы ввели числовой ID.")
        return

    try:
        # Получаем информацию о канале
        channel_entity = await user_client.get_entity(channel_id)
        channel_name = channel_entity.title or 'no_title'
        channel_username = channel_entity.username or 'no_username'
    except Exception as e:
        await event.respond(f"Не удалось получить информацию о канале: {e}")
        return

    # Устанавливаем канал для отслеживания
    await set_tracked_channel(channel_id, channel_name, channel_username)
    current_channel_id = channel_id
    logging.info(
        f"Установлен канал для отслеживания: {channel_name} (@{channel_username}) id:{channel_id}")

    # Получаем текущих подписчиков и сохраняем их в базе
    try:
        participants = await user_client.get_participants(channel_id)
        for user in participants:
            await add_subscriber(user)
        await event.respond(f"Установлен канал для отслеживания: {channel_name} (@{channel_username}) id:{channel_id}\n"
                            f"Текущее количество подписчиков: {len(participants)}")
    except Exception as e:
        await event.respond(f"Не удалось получить подписчиков канала: {e}")


@events.register(events.NewMessage(pattern=r'^/getchannelid\s+(@\S+)$'))
@admin_only
async def cmd_getchannelid(event):
    """
    /getchannelid <@username> — Получить numeric ID канала по его @username
    """
    global user_client

    if not user_client.is_connected() or not (await user_client.is_user_authorized()):
        await event.respond("Сначала нужно авторизоваться (команда /login).")
        return

    username = event.pattern_match.group(1)
    try:
        entity = await user_client.get_entity(username)
        await event.respond(f"ID для {username} = {entity.id}")
    except Exception as e:
        await event.respond(f"Не удалось получить ID: {e}")


@events.register(events.NewMessage(pattern=r'^/subcount$'))
@admin_only
async def cmd_subcount(event):
    """
    /subcount — Узнать количество подписчиков (через аккаунт)
    """
    global user_client, current_channel_id

    if current_channel_id is None:
        await event.respond("Сначала установите канал /setchannel <ID>.")
        return

    if not user_client.is_connected() or not (await user_client.is_user_authorized()):
        await event.respond("Сначала нужно авторизоваться (команда /login).")
        return

    try:
        participants = await user_client.get_participants(current_channel_id)
        count = len(participants)
        await event.respond(f"Сейчас в канале {count} подписчиков.")
    except Exception as e:
        await event.respond(f"Ошибка при получении количества подписчиков: {e}")


@events.register(events.NewMessage(pattern=r'^/viewchannel$'))
@admin_only
async def cmd_viewchannel(event):
    """
    /viewchannel — Просмотреть текущий отслеживаемый канал
    """
    channel_info = await get_tracked_channel()
    if channel_info:
        channel_username = channel_info['username'] or 'no_channel_username'
        channel_name = channel_info['name'] or 'no_title'
        channel_id = channel_info['tg_id']
        msg = f"{channel_name} @{channel_username} id:{channel_id}"
        await event.respond(msg)
    else:
        await event.respond("Нет установленного канала для отслеживания. Используйте /setchannel <ID>.")


@events.register(events.NewMessage(pattern=r'^/id$'))
async def cmd_id(event):
    """
    /id — Узнать, какой chat_id (или user_id) у текущего диалога
    """
    chat_id = event.chat_id
    sender = await event.get_sender()
    sender_id = sender.id if sender else "unknown_sender"

    await event.respond(f"Эта команда пришла из chat_id={chat_id}, ваш user_id={sender_id}")

# ------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------


async def shutdown():
    """Корректное завершение работы бота."""
    logging.info("Начинается процесс завершения работы...")
    if bot:
        await bot.disconnect()
    if user_client:
        await user_client.disconnect()
    if db:
        await db.close()
    logging.info("Работа завершена.")
    sys.exit(0)  # Завершаем процесс


async def main():
    """
    Запускаем инициализацию, фоновую задачу polling и «вечно» ждём событий.
    """
    await init_clients()

    # Регистрируем все хэндлеры
    bot.add_event_handler(cmd_start)
    bot.add_event_handler(cmd_login)
    bot.add_event_handler(cmd_logout)
    bot.add_event_handler(cmd_status)
    bot.add_event_handler(cmd_setchannel)
    bot.add_event_handler(cmd_getchannelid)
    bot.add_event_handler(cmd_subcount)
    bot.add_event_handler(cmd_viewchannel)
    bot.add_event_handler(cmd_id)

    # Запускаем фоновую задачу polling
    polling = asyncio.create_task(polling_task())

    # Обработка сигналов для корректного завершения
    loop = asyncio.get_running_loop()
    for signame in {'SIGINT', 'SIGTERM'}:
        loop.add_signal_handler(getattr(signal, signame),
                                lambda: asyncio.create_task(shutdown()))

    # Работаем вечно
    logging.info("Бот и пользовательский клиент запущены и ждут событий...")
    await asyncio.gather(polling)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен пользователем.")
