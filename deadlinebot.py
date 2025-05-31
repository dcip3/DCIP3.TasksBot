from datetime import datetime, timezone, timedelta
import time
import json
from pathlib import Path
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram import Router
from aiogram import F
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
import aiohttp
import asyncio

notified_jobs = set()

API_TOKEN = "REDACTED_TELEGRAM_BOT_TOKEN"  # Замените на ваш токен
BASE_API_URL = "https://renderfarm.local:4434/api"

bot = Bot(token=API_TOKEN)
dp_router = Router()

class AuthStates(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()

CREDENTIALS_FILE = Path("credentials.json")

"""
Загружает сохранённые учётные данные пользователей из файла.
"""
def load_credentials():
    if CREDENTIALS_FILE.exists():
        with CREDENTIALS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}

"""
Сохраняет учётные данные пользователей в файл.
"""
def save_credentials(data):
    with CREDENTIALS_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f)

"""
Получает логин, пароль и статус уведомлений для указанного пользователя.
"""
def get_auth_credentials(user_id):
    creds = user_credentials.get(str(user_id))
    if not creds or len(creds) < 2:
        return None, None, False
    login, password = creds[0], creds[1]
    notifications_enabled = creds[2] if len(creds) > 2 else False
    return login, password, notifications_enabled

"""
Возвращает основную клавиатуру для управления ботом.
"""
def get_main_keyboard():
    kb = [
        [KeyboardButton(text="Jobs"), KeyboardButton(text="Realtime"), KeyboardButton(text="Workers")],
        [KeyboardButton(text="🔔 Уведомления"), KeyboardButton(text="🚪 Выход")],
        [KeyboardButton(text="🧹 Очистить")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

"""
Форматирует строку прогресса задачи.
"""
def format_progress(completed, total):
    return f"{int((completed / total) * 100) if total else 0}% {completed}/{total}"

user_credentials = load_credentials()

active_realtime_tasks = {}

"""
Следит за завершением задач и уведомляет пользователей, если задача завершена.
"""
async def job_progress_watcher():
    global notified_jobs
    while True:
        await asyncio.sleep(60)
        for user_id, creds in user_credentials.items():
            login = creds[0]
            password = creds[1]
            notifications_enabled = creds[2] if len(creds) > 2 else False
            if not notifications_enabled:
                continue
            try:
                async with aiohttp.ClientSession() as session:
                    headers = aiohttp.BasicAuth(login, password)
                    async with session.get(f"{BASE_API_URL}/jobs", auth=headers, ssl=False) as resp:
                        if resp.status == 200:
                            jobs = await resp.json()
                            for job in jobs:
                                job_id = job.get("JobId") or job.get("_id") or job.get("Props", {}).get("JobId")
                                if not job_id:
                                    continue
                                total_tasks = job.get("Props", {}).get("Tasks", 0)
                                completed_chunks = job.get("CompletedChunks", 0)
                                progress = 0
                                if total_tasks:
                                    progress = int((completed_chunks / total_tasks) * 100)
                                if progress == 100 and job_id not in notified_jobs:
                                    date_comp_str = job.get("DateComp") or job.get("Props", {}).get("DateComp")
                                    if not date_comp_str or date_comp_str == "0001-01-01T00:00:00Z":
                                        continue
                                    try:
                                        date_comp = datetime.fromisoformat(date_comp_str.replace("Z", "+00:00"))
                                        now = datetime.now(timezone.utc)
                                        diff = now - date_comp
                                        if diff > timedelta(minutes=10):
                                            continue
                                    except Exception:
                                        continue
                                    batch = job.get("Props", {}).get("Batch", "Без имени")
                                    message_text = f"✅ Задача '{batch}' завершена (100%)."
                                    await bot.send_message(int(user_id), message_text)
                                    notified_jobs.add(job_id)
                        else:
                            print(f"Watcher: Ошибка при запросе jobs для user {user_id}: {resp.status}")
            except Exception as e:
                print(f"Watcher: Ошибка при мониторинге задач для user {user_id}: {e}")


"""
Обрабатывает команду /start: приветствует пользователя или запрашивает логин.
"""
@dp_router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id)
    login, password, _ = get_auth_credentials(user_id)
    if login and password:
        keyboard = get_main_keyboard()
        await message.answer("Добро пожаловать обратно! Выберите действие:", reply_markup=keyboard)
    else:
        await message.answer("Введите логин:")
        await state.set_state(AuthStates.waiting_for_login)


"""
Обрабатывает ввод логина пользователя.
"""
@dp_router.message(AuthStates.waiting_for_login)
async def process_login(message: types.Message, state: FSMContext):
    await state.update_data(login=message.text)
    await message.answer("Введите пароль:")
    await state.set_state(AuthStates.waiting_for_password)


"""
Обрабатывает ввод пароля пользователя и сохраняет учётные данные.
"""
@dp_router.message(AuthStates.waiting_for_password)
async def process_password(message: types.Message, state: FSMContext):
    data = await state.get_data()
    login = data["login"]
    password = message.text
    user_credentials[str(message.from_user.id)] = [login, password]
    save_credentials(user_credentials)
    if len(user_credentials[str(message.from_user.id)]) == 2:
        user_credentials[str(message.from_user.id)].append(False)
        save_credentials(user_credentials)
    await state.clear()

    keyboard = get_main_keyboard()
    await message.answer("Выберите действие:", reply_markup=keyboard)

"""
Очищает чат от последних сообщений (до 1000) по кнопке "🧹 Очистить".
"""
@dp_router.message(lambda message: message.text == "🧹 Очистить")
async def clear_chat_handler(message: types.Message):
    chat_id = message.chat.id
    from_message_id = message.message_id
    for i in range(0, 1000):
        try:
            await bot.delete_message(chat_id, from_message_id - i)
        except:
            continue

    keyboard = get_main_keyboard()
    await bot.send_message(chat_id, reply_markup=keyboard, text="")


"""
Обрабатывает запрос пользователя на просмотр списка задач (Jobs).
"""
@dp_router.message(lambda message: message.text == "Jobs")
async def handle_jobs(message: types.Message, page: int = 0):
    if message.chat.id in active_realtime_tasks:
        active_realtime_tasks[message.chat.id].cancel()
        del active_realtime_tasks[message.chat.id]
    user_id = message.from_user.id
    login, password, _ = get_auth_credentials(user_id)
    if not login or not password:
        await message.answer("Сначала введите логин и пароль с помощью /start.")
        return
    msg = await message.answer("Загрузка...")
    async with aiohttp.ClientSession() as session:
        try:
            headers = aiohttp.BasicAuth(login, password)
            async with session.get(f"{BASE_API_URL}/jobs", auth=headers, ssl=False) as resp:
                if resp.status == 200:
                    json_data = await resp.json()

                    from collections import defaultdict

                    grouped_jobs = defaultdict(list)
                    for job in json_data:
                        batch = job.get("Props", {}).get("Batch", "Без имени")
                        grouped_jobs[batch].append(job)

                    combined_jobs = []
                    for batch, jobs in grouped_jobs.items():
                        total_tasks = sum(j.get("Props", {}).get("Tasks", 0) for j in jobs)
                        completed_chunks = sum(j.get("CompletedChunks", 0) for j in jobs)
                        stat = max(j.get("Stat", 0) for j in jobs)
                        combined_jobs.append({
                            "_id": jobs[0].get("_id"),
                            "Props": {"Batch": batch, "Tasks": total_tasks},
                            "CompletedChunks": completed_chunks,
                            "Stat": stat
                        })

                    json_data = combined_jobs
                    json_data.sort(key=lambda j: j.get("Stat", 0) == 2)
                    jobs_slice = json_data[page*4 : page*4+4]
                    normal_jobs = [job for job in jobs_slice if job.get("Stat", 0) != 2]
                    suspended_jobs = [job for job in jobs_slice if job.get("Stat", 0) == 2]
                    messages = []
                    buttons = []
                    for job in normal_jobs:
                        props = job.get("Props", {})
                        batch = props.get("Batch", "Без имени")
                        total_tasks = props.get("Tasks", 0)
                        completed_chunks = job.get("CompletedChunks", 0)
                        progress_str = format_progress(completed_chunks, total_tasks)
                        messages.append(f"{batch:<24} {progress_str:^16}\n{'-'*40}")
                    if suspended_jobs:
                        messages.append("")
                        title = " suspended "
                        line_length = 40
                        dashes_each_side = (line_length - len(title)) // 2
                        separator = "-" * dashes_each_side + title + "-" * (line_length - dashes_each_side - len(title))
                        messages.append(separator)
                        for job in suspended_jobs:
                            props = job.get("Props", {})
                            batch = props.get("Batch", "Без имени")
                            total_tasks = props.get("Tasks", 0)
                            completed_chunks = job.get("CompletedChunks", 0)
                            progress_str = format_progress(completed_chunks, total_tasks)
                            messages.append(f"{batch:<24} {progress_str:^16}\n{'-'*40}")
                    for job in jobs_slice:
                        props = job.get("Props", {})
                        batch = props.get("Batch", "Без имени")
                        job_id = job.get("_id")
                        if job_id:
                            buttons.append(InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}"))
                    header = f"{'Batch':<24} {'Progress':^16}"
                    header += f"\n{'-'*40}"
                    batch_text = "\n".join(messages) if messages else "Нет данных"
                    text = f"<pre>{header}\n{batch_text}</pre>"
                    if buttons and len(buttons) > 0:
                        inline_keyboard = []
                        row = []
                        for i, button in enumerate(buttons, 1):
                            row.append(button)
                            if i % 2 == 0:
                                inline_keyboard.append(row)
                                row = []
                        if row:
                            inline_keyboard.append(row)
                        if (page + 1)*4 < len(json_data):
                            inline_keyboard.append([InlineKeyboardButton(text="➡ Вперёд", callback_data=f"jobs_page:{page+1}")])
                        keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
                        await msg.edit_text(
                            f"<pre>{header}\n{batch_text}</pre>\n\nВыберите задачу для подробной информации:",
                            parse_mode="HTML",
                            reply_markup=keyboard
                        )
                    else:
                        await msg.edit_text(text, parse_mode="HTML")
                else:
                    await msg.edit_text(f"Ошибка: {resp.status}")
        except Exception as e:
            await msg.edit_text(f"Ошибка при запросе: {e}")

"""
Обрабатывает нажатие на кнопку задачи и выводит подробную информацию о задаче.
"""
@dp_router.callback_query(lambda c: c.data and c.data.startswith("job_info:"))
async def job_info_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    login, password, _ = get_auth_credentials(user_id)
    if not login or not password:
        await callback_query.answer("Сначала введите логин и пароль с помощью /start.", show_alert=True)
        return
    job_id = callback_query.data.split(":", 1)[1]
    async with aiohttp.ClientSession() as session:
        try:
            headers = aiohttp.BasicAuth(login, password)
            async with session.get(f"{BASE_API_URL}/jobs", auth=headers, ssl=False) as resp:
                if resp.status == 200:
                    jobs = await resp.json()
                    # Find the job by _id
                    matching_jobs = [j for j in jobs if j.get("_id") == job_id]
                    if not matching_jobs:
                        await callback_query.answer("Задача не найдена.", show_alert=True)
                        return
                    batch_name = matching_jobs[0].get("Props", {}).get("Batch")
                    matching_jobs = [j for j in jobs if j.get("Props", {}).get("Batch") == batch_name]

                    for job in matching_jobs:
                        props = job.get("Props", {})
                        batch = props.get("Batch", "Без имени")
                        total_tasks = props.get("Tasks", 0)
                        completed_chunks = job.get("CompletedChunks", 0)
                        progress_str = format_progress(completed_chunks, total_tasks)
                        user = props.get("User", "Неизвестно")
                        date_comp = props.get("DateComp", "Неизвестно")
                        full_name = props.get("Name", "Без имени")
                        name = full_name.split("/")[-1] if "/" in full_name else full_name
                        message_text = (
                            f"Информация о подзадаче:\n"
                            f"Name: {name}\n"
                            f"Batch: {batch}\n"
                            f"Прогресс: {progress_str}\n"
                            f"Пользователь: {user}\n"
                        )
                        await callback_query.message.answer(message_text)

                        stat = job.get("Stat", 0)
                        job_id_btn = job.get("_id")
                        requeue_button = InlineKeyboardButton(text="🔄 Requeue", callback_data=f"requeue_job:{job_id_btn}")
                        delete_button = InlineKeyboardButton(text="🗑️ Delete", callback_data=f"delete_job:{job_id_btn}")
                        if stat == 3:
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [requeue_button, delete_button]
                            ])
                        else:
                            if stat == 2:
                                action_button = InlineKeyboardButton(text="✅ Resume", callback_data=f"resume_job:{job_id_btn}")
                            else:
                                action_button = InlineKeyboardButton(text="❌ Suspend", callback_data=f"suspend_job:{job_id_btn}")
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [action_button, requeue_button],
                                [delete_button]
                            ])
                        await callback_query.message.answer("Действия:", reply_markup=keyboard)
                    await callback_query.answer()
                else:
                    await callback_query.answer(f"Ошибка при запросе: {resp.status}", show_alert=True)
        except Exception as e:
            await callback_query.answer(f"Ошибка при запросе: {e}", show_alert=True)


"""
Обрабатывает запрос пользователя на просмотр списка воркеров (Workers).
"""
@dp_router.message(lambda message: message.text == "Workers")
async def handle_workers(message: types.Message):
    if message.chat.id in active_realtime_tasks:
        active_realtime_tasks[message.chat.id].cancel()
        del active_realtime_tasks[message.chat.id]
    user_id = message.from_user.id
    login, password, _ = get_auth_credentials(user_id)
    if not login or not password:
        await message.answer("Сначала введите логин и пароль с помощью /start.")
        return
    api_url = f"{BASE_API_URL}/slaves?Data=infosettings"
    msg = await message.answer("Загрузка...")
    async with aiohttp.ClientSession() as session:
        try:
            headers = aiohttp.BasicAuth(login, password)
            async with session.get(api_url, auth=headers, ssl=False) as resp:
                if resp.status == 200:
                    json_data = await resp.json()
                    messages = []
                    for worker in json_data:
                        info = worker.get("Info", {})
                        name = info.get("Name", "Unknown")
                        stat_num = info.get("Stat", 0)
                        if stat_num == 0:
                            stat = "Unknown"
                        elif stat_num == 1:
                            stat = "Rendering"
                        elif stat_num == 2:
                            stat = "Idle"
                        elif stat_num == 3:
                            stat = "Offline"
                        elif stat_num == 4:
                            stat = "Stalled"
                        elif stat_num == 8:
                            stat = "StartingJob"
                        else:
                            stat = f"Unknown ({stat_num})"
                        messages.append(f"{name:<24} {stat}")
                    header = f"{'Name':<24} Status"
                    header += f"\n{'-'*40}"
                    body = "\n".join(messages) if messages else "Нет данных"
                    await msg.edit_text(f"<pre>{header}\n{body}</pre>", parse_mode="HTML")
                else:
                    await msg.edit_text(f"Ошибка: {resp.status}")
        except Exception as e:
            await msg.edit_text(f"Ошибка при запросе: {e}")

"""
Включает или выключает уведомления для текущего пользователя.
"""
@dp_router.message(lambda message: message.text == "🔔 Уведомления")
async def toggle_notifications(message: types.Message):
    if message.chat.id in active_realtime_tasks:
        active_realtime_tasks[message.chat.id].cancel()
        del active_realtime_tasks[message.chat.id]
    user_id = str(message.from_user.id)
    login, password, notifications_enabled = get_auth_credentials(user_id)
    if not login or not password:
        await message.answer("Сначала введите логин и пароль с помощью /start.")
        return
    new_state = not notifications_enabled
    user_credentials[user_id] = [login, password, new_state]
    save_credentials(user_credentials)
    status_str = "включены" if new_state else "выключены"
    await message.answer(f"Уведомления теперь {status_str}.")

"""
Обрабатывает переключение страниц в списке задач (Jobs).
"""
@dp_router.callback_query(lambda c: c.data and c.data.startswith("jobs_page:"))
async def jobs_page_callback(callback_query: types.CallbackQuery):
    page_str = callback_query.data.split(":", 1)[1]
    try:
        page = int(page_str)
    except ValueError:
        await callback_query.answer("Неверный номер страницы.", show_alert=True)
        return
    await callback_query.answer()

    user_id = callback_query.from_user.id
    login, password, _ = get_auth_credentials(user_id)
    if not login or not password:
        await callback_query.message.edit_text("Сначала введите логин и пароль с помощью /start.")
        return

    async with aiohttp.ClientSession() as session:
        try:
            headers = aiohttp.BasicAuth(login, password)
            async with session.get(f"{BASE_API_URL}/jobs", auth=headers, ssl=False) as resp:
                if resp.status == 200:
                    json_data = await resp.json()

                    from collections import defaultdict

                    grouped_jobs = defaultdict(list)
                    for job in json_data:
                        batch = job.get("Props", {}).get("Batch", "Без имени")
                        grouped_jobs[batch].append(job)

                    combined_jobs = []
                    for batch, jobs in grouped_jobs.items():
                        total_tasks = sum(j.get("Props", {}).get("Tasks", 0) for j in jobs)
                        completed_chunks = sum(j.get("CompletedChunks", 0) for j in jobs)
                        stat = max(j.get("Stat", 0) for j in jobs)
                        combined_jobs.append({
                            "_id": jobs[0].get("_id"),
                            "Props": {"Batch": batch, "Tasks": total_tasks},
                            "CompletedChunks": completed_chunks,
                            "Stat": stat
                        })

                    combined_jobs.sort(key=lambda j: j.get("Stat", 0) == 2)
                    jobs_slice = combined_jobs[page*4 : page*4+4]
                    normal_jobs = [job for job in jobs_slice if job.get("Stat", 0) != 2]
                    suspended_jobs = [job for job in jobs_slice if job.get("Stat", 0) == 2]
                    messages = []
                    buttons = []
                    for job in normal_jobs:
                        props = job.get("Props", {})
                        batch = props.get("Batch", "Без имени")
                        total_tasks = props.get("Tasks", 0)
                        completed_chunks = job.get("CompletedChunks", 0)
                        progress_str = format_progress(completed_chunks, total_tasks)
                        messages.append(f"{batch:<24} {progress_str:^16}\n{'-'*40}")
                    if suspended_jobs:
                        messages.append("")
                        title = " suspended "
                        line_length = 40
                        dashes_each_side = (line_length - len(title)) // 2
                        separator = "-" * dashes_each_side + title + "-" * (line_length - dashes_each_side - len(title))
                        messages.append(separator)
                        for job in suspended_jobs:
                            props = job.get("Props", {})
                            batch = props.get("Batch", "Без имени")
                            total_tasks = props.get("Tasks", 0)
                            completed_chunks = job.get("CompletedChunks", 0)
                            progress_str = format_progress(completed_chunks, total_tasks)
                            messages.append(f"{batch:<24} {progress_str:^16}\n{'-'*40}")
                    for job in jobs_slice:
                        props = job.get("Props", {})
                        batch = props.get("Batch", "Без имени")
                        job_id = job.get("_id")
                        if job_id:
                            buttons.append(InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}"))
                    header = f"{'Batch':<24} {'Progress':^16}"
                    header += f"\n{'-'*40}"
                    batch_text = "\n".join(messages) if messages else "Нет данных"
                    text = f"<pre>{header}\n{batch_text}</pre>\n\nВыберите задачу для подробной информации:"
                    inline_keyboard = []
                    row = []
                    for i, button in enumerate(buttons, 1):
                        row.append(button)
                        if i % 2 == 0:
                            inline_keyboard.append(row)
                            row = []
                    if row:
                        inline_keyboard.append(row)
                    # Navigation buttons: Назад and Вперёд ➡
                    nav_buttons = []
                    if page > 0:
                        nav_buttons.append(InlineKeyboardButton(text="⬅ Назад", callback_data=f"jobs_page:{page-1}"))
                    if (page + 1)*4 < len(combined_jobs):
                        nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡", callback_data=f"jobs_page:{page+1}"))
                    if nav_buttons:
                        inline_keyboard.append(nav_buttons)
                    keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
                    await callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
                else:
                    await callback_query.message.edit_text(f"Ошибка: {resp.status}")
        except Exception as e:
            await callback_query.message.edit_text(f"Ошибка при запросе: {e}")

"""
Обрабатывает повторную постановку задачи (Requeue).
"""
@dp_router.callback_query(lambda c: c.data and c.data.startswith("requeue_job:"))
async def requeue_job_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    login, password, _ = get_auth_credentials(user_id)
    if not login or not password:
        await callback_query.answer("Сначала введите логин и пароль с помощью /start.", show_alert=True)
        return
    await callback_query.answer("Запрос на повторную постановку отправлен...", show_alert=True)
    job_id = callback_query.data.split(":", 1)[1]
    url = f"{BASE_API_URL}/jobs"
    json_body = {"Command": "requeue", "JobID": job_id}
    async with aiohttp.ClientSession() as session:
        try:
            headers = aiohttp.BasicAuth(login, password)
            async with session.put(url, json=json_body, auth=headers, ssl=False) as resp:
                text_resp = await resp.text()
                if resp.status == 200:
                    text = "Задача успешно поставлена заново."
                    # Чтобы определить stat, нужно получить job info
                    stat = None
                    async with session.get(f"{BASE_API_URL}/jobs", auth=headers, ssl=False) as jobs_resp:
                        if jobs_resp.status == 200:
                            jobs = await jobs_resp.json()
                            for j in jobs:
                                jid = j.get("_id")
                                if jid == job_id:
                                    stat = j.get("Stat", 0)
                                    break
                    requeue_button = InlineKeyboardButton(text="🔄 Requeue", callback_data=f"requeue_job:{job_id}")
                    delete_button = InlineKeyboardButton(text="🗑️ Delete", callback_data=f"delete_job:{job_id}")
                    if stat == 3:
                        # Completed: только Requeue и Delete
                        new_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                            [requeue_button, delete_button]
                        ])
                    else:
                        if stat == 2:
                            action_button = InlineKeyboardButton(text="✅ Resume", callback_data=f"resume_job:{job_id}")
                        else:
                            action_button = InlineKeyboardButton(text="❌ Suspend", callback_data=f"suspend_job:{job_id}")
                        new_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                            [action_button, requeue_button],
                            [delete_button]
                        ])
                    await bot.edit_message_reply_markup(
                        chat_id=callback_query.message.chat.id,
                        message_id=callback_query.message.message_id,
                        reply_markup=new_keyboard
                    )
                else:
                    text = f"Ошибка при повторной постановке задачи: {resp.status}"
                await callback_query.answer(text, show_alert=True)
        except Exception as e:
            await callback_query.answer(f"Ошибка: {e}", show_alert=True)

"""
Обрабатывает возобновление задачи (Resume).
"""
@dp_router.callback_query(lambda c: c.data and c.data.startswith("resume_job:"))
async def resume_job_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    login, password, _ = get_auth_credentials(user_id)
    if not login or not password:
        await callback_query.answer("Сначала введите логин и пароль с помощью /start.", show_alert=True)
        return
    # подтверждение отправки запроса
    await callback_query.answer("Запрос на возобновление отправлен...", show_alert=True)
    job_id = callback_query.data.split(":", 1)[1]
    url = f"{BASE_API_URL}/jobs"
    json_body = {"Command": "resume", "JobID": job_id}
    async with aiohttp.ClientSession() as session:
        try:
            headers = aiohttp.BasicAuth(login, password)
            async with session.put(url, json=json_body, auth=headers, ssl=False) as resp:
                text_resp = await resp.text()
                if resp.status == 200:
                    text = "Задача успешно возобновлена."
                    # Чтобы определить stat, нужно получить job info
                    # Получаем job info
                    stat = None
                    async with session.get(f"{BASE_API_URL}/jobs", auth=headers, ssl=False) as jobs_resp:
                        if jobs_resp.status == 200:
                            jobs = await jobs_resp.json()
                            for j in jobs:
                                jid = j.get("_id")
                                if jid == job_id:
                                    stat = j.get("Stat", 0)
                                    break
                    requeue_button = InlineKeyboardButton(text="🔄 Requeue", callback_data=f"requeue_job:{job_id}")
                    delete_button = InlineKeyboardButton(text="🗑️ Delete", callback_data=f"delete_job:{job_id}")
                    if stat == 3:
                        # Completed: только Requeue и Delete
                        new_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                            [requeue_button, delete_button]
                        ])
                    else:
                        action_button = InlineKeyboardButton(text="❌ Suspend", callback_data=f"suspend_job:{job_id}")
                        new_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                            [action_button, requeue_button],
                            [delete_button]
                        ])
                    await bot.edit_message_reply_markup(
                        chat_id=callback_query.message.chat.id,
                        message_id=callback_query.message.message_id,
                        reply_markup=new_keyboard
                    )
                else:
                    text = f"Ошибка при возобновлении задачи: {resp.status}"
                await callback_query.answer(text, show_alert=True)
        except Exception as e:
            await callback_query.answer(f"Ошибка: {e}", show_alert=True)

"""
Обрабатывает приостановку задачи (Suspend).
"""
@dp_router.callback_query(lambda c: c.data and c.data.startswith("suspend_job:"))
async def suspend_job_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    login, password, _ = get_auth_credentials(user_id)
    if not login or not password:
        await callback_query.answer("Сначала введите логин и пароль с помощью /start.", show_alert=True)
        return
    # подтверждение отправки запроса
    await callback_query.answer("Запрос на приостановку отправлен...", show_alert=True)
    job_id = callback_query.data.split(":", 1)[1]
    url = f"{BASE_API_URL}/jobs"
    json_body = {"Command": "suspend", "JobID": job_id}
    async with aiohttp.ClientSession() as session:
        try:
            headers = aiohttp.BasicAuth(login, password)
            async with session.put(url, json=json_body, auth=headers, ssl=False) as resp:
                text_resp = await resp.text()
                if resp.status == 200:
                    text = "Задача успешно приостановлена."
                    # Чтобы определить stat, нужно получить job info
                    stat = None
                    async with session.get(f"{BASE_API_URL}/jobs", auth=headers, ssl=False) as jobs_resp:
                        if jobs_resp.status == 200:
                            jobs = await jobs_resp.json()
                            for j in jobs:
                                jid = j.get("_id")
                                if jid == job_id:
                                    stat = j.get("Stat", 0)
                                    break
                    requeue_button = InlineKeyboardButton(text="🔄 Requeue", callback_data=f"requeue_job:{job_id}")
                    delete_button = InlineKeyboardButton(text="🗑️ Delete", callback_data=f"delete_job:{job_id}")
                    if stat == 3:
                        # Completed: только Requeue и Delete
                        new_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                            [requeue_button, delete_button]
                        ])
                    else:
                        action_button = InlineKeyboardButton(text="✅ Resume", callback_data=f"resume_job:{job_id}")
                        new_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                            [action_button, requeue_button],
                            [delete_button]
                        ])
                    await bot.edit_message_reply_markup(
                        chat_id=callback_query.message.chat.id,
                        message_id=callback_query.message.message_id,
                        reply_markup=new_keyboard
                    )
                else:
                    text = f"Ошибка при приостановке задачи: {resp.status}"
                await callback_query.answer(text, show_alert=True)
        except Exception as e:
            await callback_query.answer(f"Ошибка: {e}", show_alert=True)

"""
Обрабатывает удаление задачи (Delete).
"""
@dp_router.callback_query(lambda c: c.data and c.data.startswith("delete_job:"))
async def delete_job_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    login, password, _ = get_auth_credentials(user_id)
    if not login or not password:
        await callback_query.answer("Сначала введите логин и пароль с помощью /start.", show_alert=True)
        return
    await callback_query.answer("Запрос на удаление отправлен...", show_alert=True)
    job_id = callback_query.data.split(":", 1)[1]
    url = f"{BASE_API_URL}/jobs?JobID={job_id}"
    async with aiohttp.ClientSession() as session:
        try:
            headers = aiohttp.BasicAuth(login, password)
            async with session.delete(url, auth=headers, ssl=False) as resp:
                text_resp = await resp.text()
                if resp.status == 200:
                    text = "Задача успешно удалена."
                    await bot.edit_message_reply_markup(
                        chat_id=callback_query.message.chat.id,
                        message_id=callback_query.message.message_id,
                        reply_markup=None
                    )
                else:
                    text = f"Ошибка при удалении задачи: {resp.status}"
                await callback_query.answer(text, show_alert=True)
        except Exception as e:
            await callback_query.answer(f"Ошибка: {e}", show_alert=True)

"""
Обрабатывает выход пользователя из аккаунта ("🚪 Выход").
"""
@dp_router.message(lambda message: message.text == "🚪 Выход")
async def logout_handler(message: types.Message, state: FSMContext):
    if message.chat.id in active_realtime_tasks:
        active_realtime_tasks[message.chat.id].cancel()
        del active_realtime_tasks[message.chat.id]
    user_id = str(message.from_user.id)
    if user_id in user_credentials:
        del user_credentials[user_id]
        save_credentials(user_credentials)
    await state.clear()
    await message.answer("Вы успешно вышли. Введите /start для повторного входа.")

"""
Обрабатывает режим реального времени обновления задач по кнопке "Realtime".
"""
@dp_router.message(lambda message: message.text == "Realtime")
async def handle_realtime(message: types.Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    login, password, _ = get_auth_credentials(user_id)
    if not login or not password:
        await message.answer("Сначала введите логин и пароль с помощью /start.")
        return
    if chat_id in active_realtime_tasks:
        active_realtime_tasks[chat_id].cancel()
        del active_realtime_tasks[chat_id]

    await message.answer("Задачи обновляются каждые 5 секунд до следующего сообщения.")

    async def realtime_loop():
        async with aiohttp.ClientSession() as session:
            try:
                headers = aiohttp.BasicAuth(login, password)
                msg = await message.answer("Загрузка...")
                last_text = None
                while True:
                    async with session.get(f"{BASE_API_URL}/jobs", auth=headers, ssl=False) as resp:
                        if resp.status != 200:
                            await msg.edit_text("Ошибка при получении задач.")
                            break
                        json_data = await resp.json()

                        from collections import defaultdict

                        grouped_jobs = defaultdict(list)
                        for job in json_data:
                            batch = job.get("Props", {}).get("Batch", "Без имени")
                            grouped_jobs[batch].append(job)

                        combined_jobs = []
                        for batch, jobs in grouped_jobs.items():
                            total_tasks = sum(j.get("Props", {}).get("Tasks", 0) for j in jobs)
                            completed_chunks = sum(j.get("CompletedChunks", 0) for j in jobs)
                            stat = max(j.get("Stat", 0) for j in jobs)
                            combined_jobs.append({
                                "Props": {"Batch": batch, "Tasks": total_tasks},
                                "CompletedChunks": completed_chunks,
                                "Stat": stat
                            })

                        combined_jobs.sort(key=lambda j: j.get("Stat", 0) == 2)

                        normal_jobs = [job for job in combined_jobs if job.get("Stat", 0) != 2]
                        suspended_jobs = [job for job in combined_jobs if job.get("Stat", 0) == 2]

                        messages = []
                        for job in normal_jobs:
                            props = job.get("Props", {})
                            batch = props.get("Batch", "Без имени")
                            total_tasks = props.get("Tasks", 0)
                            completed_chunks = job.get("CompletedChunks", 0)
                            progress_str = format_progress(completed_chunks, total_tasks)
                            messages.append(f"{batch:<24} {progress_str:^16}\n{'-'*40}")

                        if suspended_jobs:
                            messages.append("")
                            title = " suspended "
                            line_length = 40
                            dashes_each_side = (line_length - len(title)) // 2
                            separator = "-" * dashes_each_side + title + "-" * (line_length - dashes_each_side - len(title))
                            messages.append(separator)
                            for job in suspended_jobs:
                                props = job.get("Props", {})
                                batch = props.get("Batch", "Без имени")
                                total_tasks = props.get("Tasks", 0)
                                completed_chunks = job.get("CompletedChunks", 0)
                                progress_str = format_progress(completed_chunks, total_tasks)
                                messages.append(f"{batch:<24} {progress_str:^16}\n{'-'*40}")

                        header = f"{'Batch':<24} {'Progress':^16}"
                        header += f"\n{'-'*40}"
                        batch_text = "\n".join(messages) if messages else "Нет данных"
                        new_text = f"<pre>{header}\n{batch_text}</pre>"
                        if new_text != last_text:
                            await msg.edit_text(new_text, parse_mode="HTML")
                            last_text = new_text
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                await message.answer(f"Ошибка: {e}")

    task = asyncio.create_task(realtime_loop())
    active_realtime_tasks[chat_id] = task

"""
Главная функция запускает бота и watcher уведомлений.
"""
# Перемещено в конец файла:
async def main():
    from aiogram.fsm.storage.memory import MemoryStorage
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(dp_router)
    await bot.delete_webhook(drop_pending_updates=True)
    # Запускаем watcher в фоне
    asyncio.create_task(job_progress_watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())