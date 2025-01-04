import asyncio
import os
import logging

from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
from dotenv import load_dotenv

# Загружаем .env
load_dotenv()

logging.basicConfig(level=logging.INFO)

# Читаем переменные из .env
# int, например 123456
API_ID = int(os.getenv("TG_API_ID", "123456"))
# str, например "abcdef0123456789..."
API_HASH = os.getenv("TG_API_HASH", "ABC123...")
BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "123456:ABC-...")    # Токен бота
# Чат, из которого принимаем команды и куда слать уведомления
ADMIN_CHAT_ID = int(os.getenv("TG_ADMIN_CHAT_ID", "0"))

# Названия (или пути к) файловых/строковых сессий
SESSION_NAME_USER = os.getenv("TG_USER_SESSION_NAME", "user_account.session")
SESSION_NAME_BOT = os.getenv("TG_BOT_SESSION_NAME", "bot_account.session")

# Глобальные переменные
user_client = None
bot = None
current_channel_id = None

# ------------------------------------------------------------------------------
# ИНИЦИАЛИЗАЦИЯ
# ------------------------------------------------------------------------------


async def init_clients():
    """
    Инициализирует бота и клиентский аккаунт (пользовательский).
    """
    global user_client, bot

    # Бот
    bot = TelegramClient(
        SESSION_NAME_BOT,
        API_ID,
        API_HASH
    )
    await bot.start(bot_token=BOT_TOKEN)

    # Пользовательский клиент
    user_client = TelegramClient(
        SESSION_NAME_USER,
        API_ID,
        API_HASH
    )
    # user_client не запускаем сразу через start(phone=...),
    # потому что авторизация будет по команде /login
    await user_client.connect()


# ------------------------------------------------------------------------------
# ОБРАБОТЧИКИ СОБЫТИЙ ДЛЯ ПОДПИСКИ/ОТПИСКИ
# ------------------------------------------------------------------------------
@events.register(events.ChatAction)
async def on_chat_action(event):
    """
    Срабатывает, когда пользователь:
     - вступил (user_joined)
     - вышел (user_left)
     - был добавлен (user_added)
     - был удалён (user_kicked)
    и т.д.

    Мы проверяем, что событие относится к каналу, за которым следим (current_channel_id).
    Получаем подробную информацию о канале и пользователе, отправляем уведомление в ADMIN_CHAT_ID.
    """
    global current_channel_id

    if current_channel_id is None:
        return  # Канал для отслеживания не задан

    if event.chat_id != current_channel_id:
        return  # Событие не в том канале

    # Если у нас нет связи с user_client (он не авторизован),
    # не сможем получить детали о канале/пользователе.
    if not user_client.is_connected() or not (await user_client.is_user_authorized()):
        return

    # Получаем entity канала (чтобы узнать username канала)
    try:
        channel_entity = await user_client.get_entity(current_channel_id)
        channel_username = getattr(channel_entity, "username", None)
        if not channel_username:
            channel_username = "no_username"
    except:
        channel_username = "no_username"

    # Выясняем, кто вступил/вышел
    user_id = None
    action_text = None
    if event.user_joined or event.user_added:
        user_id = event.user_id
        action_text = "ПОДПИСКА"
    elif event.user_left or event.user_kicked:
        user_id = event.user_id
        action_text = "ОТПИСКА"
    else:
        # Нас интересуют только подписка/отписка
        return

    # Получаем информацию о пользователе
    user_username = "no_username"
    user_first_name = ""
    user_last_name = "no_surname"
    try:
        user_entity = await user_client.get_entity(user_id)
        if getattr(user_entity, "username", None):
            user_username = user_entity.username

        if getattr(user_entity, "first_name", None):
            user_first_name = user_entity.first_name

        if getattr(user_entity, "last_name", None):
            user_last_name = user_entity.last_name

    except Exception:
        pass

    # Формируем текст уведомления
    msg = (f"Зафиксирована {action_text} от канала @{channel_username}, "
           f"пользователь: @{user_username} {user_first_name} {user_last_name} (id{user_id})")

    # Отправляем в ADMIN_CHAT_ID
    # (при условии, что ADMIN_CHAT_ID != 0)
    if ADMIN_CHAT_ID != 0:
        try:
            await bot.send_message(ADMIN_CHAT_ID, msg)
        except Exception as e:
            logging.error(
                f"Не удалось отправить уведомление в ADMIN_CHAT_ID: {e}")


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
        "/login – Войти в аккаунт\n"
        "/logout – Выйти из аккаунта\n"
        "/status – Проверить, авторизован ли аккаунт\n"
        "/setchannel <ID> – Установить ID канала\n"
        "/getchannelid <@username> – Получить numeric ID канала по его username\n"
        "/subcount – Узнать, сколько подписчиков\n"
    )
    await event.respond(text)


@events.register(events.NewMessage(pattern=r'^/login$'))
@admin_only
async def cmd_login(event):
    """
    /login — Запрос номера телефона и кода для авторизации 
    (если уже авторизованы, скажет об этом)
    """
    global user_client

    if user_client.is_connected() and (await user_client.is_user_authorized()):
        await event.respond("Аккаунт уже авторизован.")
        return

    await event.respond("Окей, давайте авторизуемся. Вышлите свой номер телефона в формате +71234567890:")

    # Ждём ответа с номером телефона
    phone_event = await bot.wait_for_event(
        events.NewMessage(chats=event.chat_id,
                          func=lambda e: e.raw_text.startswith('+'))
    )
    phone_number = phone_event.raw_text.strip()

    # Начинаем авторизацию
    try:
        await user_client.connect()

        if not await user_client.is_user_authorized():
            await user_client.send_code_request(phone_number)
            await event.respond(f"Код отправлен на номер {phone_number}. Введите код:")

            code_event = await bot.wait_for_event(
                events.NewMessage(chats=event.chat_id,
                                  func=lambda e: e.raw_text.isdigit())
            )
            code = code_event.raw_text.strip()

            try:
                # Если включена 2FA, Telethon попросит пароль отдельно
                await user_client.sign_in(phone_number, code)
                await event.respond("Успешно авторизовались!")
            except errors.SessionPasswordNeededError:
                await event.respond("Введите пароль от аккаунта (2FA):")
                pass_event = await bot.wait_for_event(events.NewMessage(chats=event.chat_id))
                password_2fa = pass_event.raw_text.strip()

                await user_client.sign_in(password=password_2fa)
                await event.respond("Успешно авторизовались (с 2FA)!")
        else:
            await event.respond("Уже авторизовано.")

    except Exception as ex:
        await event.respond(f"Ошибка при авторизации: {ex}")


@events.register(events.NewMessage(pattern=r'^/logout$'))
@admin_only
async def cmd_logout(event):
    """
    /logout — Разлогинивает аккаунт и удаляет сохранённую сессию
    """
    global user_client

    if user_client.is_connected() and await user_client.is_user_authorized():
        await user_client.log_out()
        await user_client.disconnect()
        await event.respond("Аккаунт разлогинен. Сессионный файл останется на сервере, можете удалить вручную.")
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
    current_channel_id = int(channel_id)
    await event.respond(f"Установлен канал для отслеживания: {current_channel_id}")


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
        # Получаем список участников канала
        participants = await user_client.get_participants(current_channel_id)
        count = len(participants)
        await event.respond(f"Сейчас в канале {count} подписчиков.")
    except Exception as e:
        await event.respond(f"Ошибка при получении количества подписчиков: {e}")


# ------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------
async def main():
    """
    Запускаем инициализацию и «вечно» ждём событий.
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

    user_client.add_event_handler(on_chat_action)

    # Работаем вечно
    logging.info("Бот и пользовательский клиент запущены и ждут событий...")
    await asyncio.Future()  # блокировка (вечное ожидание)


if __name__ == "__main__":
    # Запуск для продакшена (24/7)
    asyncio.run(main())
