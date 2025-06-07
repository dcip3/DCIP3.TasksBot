import asyncio
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path, PurePosixPath

import aiohttp
import json
import numpy as np
import OpenEXR
import PyOpenColorIO as ocio
import Imath

from aiogram import Bot, Dispatcher, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from config import (
    TG_API_TOKEN,
    BASE_API_URL,
    DROPBOX_APP_KEY,
    DROPBOX_APP_SECRET,
    DROPBOX_REFRESH_TOKEN,
    TEAM_MEMBER_ID,
    ROOT_NAMESPACE_ID,
    DROPBOX_ROOT_MARKER,
    WORKER_STATUS_MAP,
    JOB_STATUS_MAP,
    user_credentials,
    load_credentials,
    save_credentials,
    get_auth_credentials,
    get_main_keyboard
)

from dropbox_helpers import (
    get_fresh_access_token,
    fetch_dropbox_metadata,
    count_exr_files,
    download_exr_folder,
    upload_video_to_dropbox
)

from video_helpers import (
    convert_exr_folder_to_srgb, 
    assemble_video_from_exr
)



# --- Logging setup ---
import logging

# Initialize bot and router using TG_API_TOKEN
bot = Bot(token=TG_API_TOKEN)
dp_router = Router()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Переменная для хранения состояния загрузки (для возобновления)
download_states = {}

# Для хранения флагов остановки загрузок (download_folder)
stop_downloads = {}

# Для уведомлений о завершённых задачах
notified_jobs = set()

# Активные задачи в режиме реального времени
active_realtime_tasks = {}

# Ограничения по параллельным загрузкам
MAX_CONCURRENT_DOWNLOADS = 2
current_downloads = 0

# Семафор для ограничениия числа параллельных конвертация/сборок видео
conversion_semaphore = asyncio.Semaphore(1)

def has_enough_space(path: str, min_free_bytes: int) -> bool:
    """
    Проверяет, что на том разделе, где находится path, доступно не менее min_free_bytes байт.

    Args:
        path (str): путь к папке или файлу на диске.
        min_free_bytes (int): минимально требуемое свободное место в байтах.

    Returns:
        bool: True, если доступного места >= min_free_bytes, иначе False.
    """
    total, used, free = shutil.disk_usage(path)
    return free >= min_free_bytes

def clear_folder(folder_path):
    """
    Очищает содержимое папки, но не удаляет саму папку. Если папки не существует, создаёт её.

    Args:
        folder_path (str or Path): путь к папке для очистки.

    Returns:
        None
    """
    folder = Path(folder_path)
    if folder.exists():
        for item in folder.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink()
            except Exception:
                pass
    else:
        folder.mkdir(parents=True, exist_ok=True)



def cleanup_temp_and_conv():
    """
    Очищает содержимое папок 'temp' и 'conv', но не удаляет сами папки.

    Returns:
        None
    """
    clear_folder(Path("temp"))
    clear_folder(Path("conv"))

# --- Credential and helper functions ---

class AuthStates(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()


# Обеспечивает существование директории temp и возвращает её путь
def ensure_temp_dir():
    temp_path = Path("temp")
    temp_path.mkdir(exist_ok=True)
    return temp_path

# Форматирует строку прогресса задачи.
def format_progress(completed, total):
    return f"{int((completed / total) * 100) if total else 0}% {completed}/{total}"


# Следит за завершением задач и уведомляет пользователей, если задача завершена.
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
                                # Remove job_id from notified_jobs if status is no longer Completed (Stat != 3)
                                stat_num = job.get("Stat", 0)
                                if job_id in notified_jobs and stat_num != 3:
                                    notified_jobs.remove(job_id)
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
                            logger.error(f"Watcher: Ошибка при запросе jobs для user {user_id}: {resp.status}")
            except Exception as e:
                logger.error(f"Watcher: Ошибка при мониторинге задач для user {user_id}: {e}", exc_info=True)


# --- Handlers ---

# Обрабатывает команду /start: приветствует пользователя или запрашивает логин.

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


# Обрабатывает ввод логина пользователя.
@dp_router.message(AuthStates.waiting_for_login)
async def process_login(message: types.Message, state: FSMContext):
    await state.update_data(login=message.text)
    await message.answer("Введите пароль:")
    await state.set_state(AuthStates.waiting_for_password)


# Обрабатывает ввод пароля пользователя и сохраняет учётные данные.

@dp_router.message(AuthStates.waiting_for_password)
async def process_password(message: types.Message, state: FSMContext):
    data = await state.get_data()
    login = data["login"]
    password = message.text
    user_id = str(message.from_user.id)

    # Валидация введённых учётных данных через запрос к API
    try:
        async with aiohttp.ClientSession() as session:
            headers = aiohttp.BasicAuth(login, password)
            async with session.get(f"{BASE_API_URL}/jobs", auth=headers, ssl=False) as resp:
                if resp.status != 200:
                    # Неверные учётные данные
                    await message.answer("Неверный логин или пароль. Попробуйте ещё раз.\nВведите логин:")
                    await state.clear()
                    await state.set_state(AuthStates.waiting_for_login)
                    return
    except Exception:
        await message.answer("Ошибка при проверке учётных данных. Попробуйте ещё раз.\nВведите логин:")
        await state.clear()
        await state.set_state(AuthStates.waiting_for_login)
        return

    # Если проверка успешна, сохраняем данные
    user_credentials[user_id] = [login, password, False]
    save_credentials(user_credentials)
    await state.clear()

    keyboard = get_main_keyboard()
    await message.answer("Выберите действие:", reply_markup=keyboard)

# Очищает чат от последних сообщений (до 1000) по кнопке "🧹 Очистить".

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
    await bot.send_message(chat_id, "\u200b", reply_markup=keyboard)


# Обрабатывает запрос пользователя на просмотр списка задач (Jobs).

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

                    from datetime import datetime
                    combined_jobs = []
                    for batch, jobs in grouped_jobs.items():
                        total_tasks = sum(j.get("Props", {}).get("Tasks", 0) for j in jobs)
                        completed_chunks = sum(j.get("CompletedChunks", 0) for j in jobs)
                        # Determine batch-level status with priority: Active > Pending > Suspended > Failed > Completed > Unknown
                        status_list = [j.get("Stat", 0) for j in jobs]
                        if 1 in status_list:
                            batch_stat = 1      # Active
                        elif 6 in status_list:
                            batch_stat = 6      # Pending
                        elif 2 in status_list:
                            batch_stat = 2      # Suspended
                        elif 4 in status_list:
                            batch_stat = 4      # Failed
                        elif all(s == 3 for s in status_list):
                            batch_stat = 3      # Completed
                        else:
                            batch_stat = 0      # Unknown
                        # Determine the most recent Date among jobs in this batch
                        dates = []
                        for j in jobs:
                            date_str = j.get("Date")
                            if date_str:
                                try:
                                    dates.append(datetime.fromisoformat(date_str))
                                except Exception:
                                    pass
                        max_date = max(dates) if dates else datetime.min
                        combined_jobs.append({
                            "_id": jobs[0].get("_id"),
                            "Props": {"Batch": batch, "Tasks": total_tasks},
                            "CompletedChunks": completed_chunks,
                            "Stat": batch_stat,
                            "DateParsed": max_date
                        })
                    # Sort by DateParsed descending (newest first)
                    combined_jobs.sort(key=lambda j: j["DateParsed"], reverse=True)
                    json_data = combined_jobs
                    # json_data already sorted by DateParsed
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
                        stat = job.get("Stat", 0)
                        if stat == 3:
                            icon = "✅"
                        else:
                            icon = "▶️"
                        messages.append(f"{icon} {batch:<22} {progress_str:^16}\n{'-'*40}")
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
                            messages.append(f"⏸️ {batch:<22} {progress_str:^16}\n{'-'*40}")
                    # Add buttons in the order that matches the display: active batches first, then suspended.
                    for job in normal_jobs:
                        props = job.get("Props", {})
                        batch = props.get("Batch", "Без имени")
                        job_id = job.get("_id")
                        if job_id:
                            buttons.append(InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}"))
                    if suspended_jobs:
                        for job in suspended_jobs:
                            props = job.get("Props", {})
                            batch = props.get("Batch", "Без имени")
                            job_id = job.get("_id")
                            if job_id:
                                buttons.append(InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}"))
                    header = f"{'Batch':<24} {'Progress':^16}"
                    header += f"\n{'-'*40}"
                    batch_text = "\n".join(messages) if messages else "Нет данных"
                    text = f"<pre>{header}\n{batch_text}</pre>"
                    if buttons:
                        inline_keyboard = []
                        row = []
                        for i, button in enumerate(buttons, 1):
                            row.append(button)
                            if i % 2 == 0:
                                inline_keyboard.append(row)
                                row = []
                        if row:
                            inline_keyboard.append(row)
                        # Навигация
                        total_items = len(json_data)
                        total_pages = (total_items + 3) // 4
                        # Кнопки навигации
                        nav_buttons = []
                        if page > 0:
                            nav_buttons.append(InlineKeyboardButton(text="⬅ Назад", callback_data=f"jobs_page:{page-1}"))
                        if (page + 1) < total_pages:
                            nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡", callback_data=f"jobs_page:{page+1}"))
                        if nav_buttons:
                            inline_keyboard.append(nav_buttons)
                        keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
                        page_info = f"Страница {page+1} из {total_pages}"
                        await msg.edit_text(
                            f"<pre>{header}\n{batch_text}\n{page_info}</pre>\n\nВыберите задачу для подробной информации:",
                            parse_mode="HTML",
                            reply_markup=keyboard
                        )
                    else:
                        await msg.edit_text(text, parse_mode="HTML")
                else:
                    await msg.edit_text(f"Ошибка: {resp.status}")
        except Exception as e:
            await msg.edit_text(f"Ошибка при запросе: {e}")

# Обрабатывает нажатие на кнопку задачи и выводит подробную информацию о задаче.

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
                        full_name = props.get("Name", "Без имени")
                        name = full_name.split("/")[-1] if "/" in full_name else full_name

                        # --- Расчет примерного времени ожидания (ETA) ---
                        eta_str = "N/A"
                        async with session.get(f"{BASE_API_URL}/tasks?JobID={job_id}", auth=headers, ssl=False) as tasks_resp:
                            if tasks_resp.status == 200:
                                tasks_data = await tasks_resp.json()
                                if isinstance(tasks_data, list):
                                    tasks_list = tasks_data
                                else:
                                    tasks_list = tasks_data.get("Tasks", [])
                                logger.info(f"job_info_callback: retrieved {len(tasks_list)} tasks for job {job_id}")
                                try:
                                    # Вычисляем длительности всех завершенных задач
                                    durations = []
                                    for t in tasks_list:
                                        if t.get("Stat") == 5:
                                            start_str = t.get("StartRen")
                                            comp_str = t.get("Comp")
                                            if start_str and comp_str and start_str != "0001-01-01T00:00:00Z" and comp_str != "0001-01-01T00:00:00Z":
                                                start_time = datetime.fromisoformat(start_str)
                                                comp_time = datetime.fromisoformat(comp_str)
                                                duration_val = (comp_time - start_time).total_seconds()
                                                logger.info(f"job_info_callback: task {t.get('TaskID')} start {start_time}, comp {comp_time}, duration {duration_val}")
                                                durations.append(duration_val)
                                            else:
                                                logger.info(f"job_info_callback: task {t.get('TaskID')} has invalid timestamps: start {start_str}, comp {comp_str}")
                                    logger.info(f"job_info_callback: durations for job {job_id}: {durations}")
                                    if durations:
                                        avg_duration = sum(durations) / len(durations)
                                        logger.info(f"job_info_callback: average duration for job {job_id}: {avg_duration}")
                                        # Считаем количество оставшихся задач по общему числу и завершённым
                                        remaining = total_tasks - completed_chunks
                                        logger.info(f"job_info_callback: total_tasks for job {job_id}: {total_tasks}, completed_chunks: {completed_chunks}, remaining: {remaining}")
                                        total_eta_seconds = avg_duration * remaining
                                        logger.info(f"job_info_callback: total_eta_seconds for job {job_id}: {total_eta_seconds}")
                                        if total_eta_seconds > 0:
                                            eta_td = timedelta(seconds=int(total_eta_seconds))
                                            eta_str = str(eta_td)
                                except Exception as e:
                                    logger.error(f"job_info_callback: error calculating ETA for job {job_id}: {e}", exc_info=True)
                                    eta_str = "N/A"
                        # End of ETA block

                        message_text = (
                            f"Информация о подзадаче:\n"
                            f"Hipname: {batch}\n"
                            f"Rop: {name}\n"
                            f"Прогресс: {progress_str}\n"
                            f"ETA: {eta_str}\n"
                        )
                        await callback_query.message.answer(message_text)

                        job_id_btn = job.get("_id")
                        stat_num = job.get("Stat", 0)
                        requeue_button = InlineKeyboardButton(text="🔄 Requeue", callback_data=f"requeue_job:{job_id_btn}")
                        delete_button = InlineKeyboardButton(text="🗑️ Delete", callback_data=f"delete_job:{job_id_btn}")
                        preview_button = InlineKeyboardButton(text="🔍 Preview", callback_data=f"preview_job:{job_id_btn}")
                        tasks_button = InlineKeyboardButton(text="📋 Tasks", callback_data=f"tasks_job:{job_id_btn}")
                        if stat_num == 3:
                            # Completed: two rows, max 2 columns per row
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [requeue_button, delete_button],
                                [preview_button, tasks_button]
                            ])
                        else:
                            if stat_num == 2:
                                action_button = InlineKeyboardButton(text="▶️ Resume", callback_data=f"resume_job:{job_id_btn}")
                            else:
                                action_button = InlineKeyboardButton(text="⏸️ Suspend", callback_data=f"suspend_job:{job_id_btn}")
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [action_button, requeue_button],
                                [delete_button, preview_button],
                                [tasks_button]
                            ])
                        await callback_query.message.answer("Действия:", reply_markup=keyboard)
                    await callback_query.answer()
                else:
                    await callback_query.answer(f"Ошибка при запросе: {resp.status}", show_alert=True)
        except Exception as e:
            await callback_query.answer(f"Ошибка при запросе: {e}", show_alert=True)


# Обрабатывает запрос на просмотр информации по задачам Job (Tasks)
@dp_router.callback_query(lambda c: c.data and c.data.startswith("tasks_job:"))
async def tasks_job_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    login, password, _ = get_auth_credentials(user_id)
    if not login or not password:
        await callback_query.answer("Сначала введите логин и пароль с помощью /start.", show_alert=True)
        return
    job_id = callback_query.data.split(":", 1)[1]
    api_url = f"{BASE_API_URL}/tasks?JobID={job_id}"
    async with aiohttp.ClientSession() as session:
        try:
            headers = aiohttp.BasicAuth(login, password)
            async with session.get(api_url, auth=headers, ssl=False) as resp:
                if resp.status != 200:
                    await callback_query.answer(f"Ошибка при запросе задач: {resp.status}", show_alert=True)
                    return
                data = await resp.json()
                tasks = data.get("Tasks", [])
                if not tasks:
                    await callback_query.message.answer("Нет задач для данного Job.")
                    await callback_query.answer()
                    return

                # Формируем текст для отображения списка задач
                lines = []
                # Заголовок
                header = f"{'Frames':<18} {'Prog':^10} {'Time':^12}"
                header += f"\n{'-'*42}"
                lines.append(header)
                # Определение иконки для статуса задачи
                def get_task_icon(stat):
                    if stat == 5:   # Completed
                        return "✅"
                    elif stat == 4: # Rendering
                        return "▶️"
                    elif stat == 3: # Suspended
                        return "⏸️"
                    elif stat == 6: # Failed
                        return "❌"
                    elif stat in (2, 8): # Queued или Pending
                        return "⏳"
                    else:           # Unknown и другие
                        return "❓"

                from datetime import datetime, timezone
                for task in tasks:
                    frames = task.get("Frames", "")
                    prog = task.get("Prog", "")
                    stat = task.get("Stat", 1)
                    icon = get_task_icon(stat)
                    rendertime_str = ""
                    start_str = task.get("StartRen")
                    if start_str and start_str != "0001-01-01T00:00:00Z":
                        try:
                            start_time = datetime.fromisoformat(start_str)
                            # Completed tasks
                            if stat == 5:
                                comp_str = task.get("Comp")
                                if comp_str and comp_str != "0001-01-01T00:00:00Z":
                                    comp_time = datetime.fromisoformat(comp_str)
                                    duration = comp_time.astimezone(timezone.utc) - start_time.astimezone(timezone.utc)
                                    rendertime_str = str(duration).split(".")[0]
                            # Currently rendering tasks
                            elif stat == 4:
                                now_utc = datetime.now(timezone.utc)
                                duration = now_utc - start_time.astimezone(timezone.utc)
                                rendertime_str = str(duration).split(".")[0]
                        except Exception:
                            pass
                    line = f"{icon} {frames:<16} {prog:^10} {rendertime_str:^12}"
                    lines.append(line)

                message_text = "<pre>" + "\n".join(lines) + "</pre>"
                await callback_query.message.answer(message_text, parse_mode="HTML")
                await callback_query.answer()
        except Exception as e:
            await callback_query.answer(f"Ошибка при получении задач: {e}", show_alert=True)


# Обрабатывает запрос пользователя на просмотр списка воркеров (Workers).

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
                        stat = WORKER_STATUS_MAP.get(stat_num, f"Unknown ({stat_num})")
                        messages.append(f"{name:<24} {stat}")
                    header = f"{'Name':<24} Status"
                    header += f"\n{'-'*40}"
                    body = "\n".join(messages) if messages else "Нет данных"
                    await msg.edit_text(f"<pre>{header}\n{body}</pre>", parse_mode="HTML")
                else:
                    await msg.edit_text(f"Ошибка: {resp.status}")
        except Exception as e:
            await msg.edit_text(f"Ошибка при запросе: {e}")

# Включает или выключает уведомления для текущего пользователя.

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

# Обрабатывает переключение страниц в списке задач (Jobs).

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

                    from datetime import datetime
                    combined_jobs = []
                    for batch, jobs in grouped_jobs.items():
                        total_tasks = sum(j.get("Props", {}).get("Tasks", 0) for j in jobs)
                        completed_chunks = sum(j.get("CompletedChunks", 0) for j in jobs)
                        # Determine batch-level status with priority: Active > Pending > Suspended > Failed > Completed > Unknown
                        status_list = [j.get("Stat", 0) for j in jobs]
                        if 1 in status_list:
                            batch_stat = 1      # Active
                        elif 6 in status_list:
                            batch_stat = 6      # Pending
                        elif 2 in status_list:
                            batch_stat = 2      # Suspended
                        elif 4 in status_list:
                            batch_stat = 4      # Failed
                        elif all(s == 3 for s in status_list):
                            batch_stat = 3      # Completed
                        else:
                            batch_stat = 0      # Unknown
                        # Determine the most recent Date among jobs in this batch
                        dates = []
                        for j in jobs:
                            date_str = j.get("Date")
                            if date_str:
                                try:
                                    dates.append(datetime.fromisoformat(date_str))
                                except Exception:
                                    pass
                        max_date = max(dates) if dates else datetime.min
                        combined_jobs.append({
                            "_id": jobs[0].get("_id"),
                            "Props": {"Batch": batch, "Tasks": total_tasks},
                            "CompletedChunks": completed_chunks,
                            "Stat": batch_stat,
                            "DateParsed": max_date
                        })
                    # Sort by DateParsed descending (newest first)
                    combined_jobs.sort(key=lambda j: j["DateParsed"], reverse=True)
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
                        stat = job.get("Stat", 0)
                        icon = "✅" if stat == 3 else "▶️"
                        messages.append(f"{icon} {batch:<22} {progress_str:^16}\n{'-'*40}")
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
                            icon = "⏸️"
                            messages.append(f"{icon} {batch:<22} {progress_str:^16}\n{'-'*40}")
                    for job in jobs_slice:
                        props = job.get("Props", {})
                        batch = props.get("Batch", "Без имени")
                        job_id = job.get("_id")
                        if job_id:
                            buttons.append(InlineKeyboardButton(text=batch, callback_data=f"job_info:{job_id}"))
                    header = f"{'Batch':<24} {'Progress':^16}"
                    header += f"\n{'-'*40}"
                    batch_text = "\n".join(messages) if messages else "Нет данных"
                    inline_keyboard = []
                    row = []
                    for i, button in enumerate(buttons, 1):
                        row.append(button)
                        if i % 2 == 0:
                            inline_keyboard.append(row)
                            row = []
                    if row:
                        inline_keyboard.append(row)
                    # Навигация
                    total_items = len(combined_jobs)
                    total_pages = (total_items + 3) // 4
                    nav_buttons = []
                    if page > 0:
                        nav_buttons.append(InlineKeyboardButton(text="⬅ Назад", callback_data=f"jobs_page:{page-1}"))
                    if (page + 1) < total_pages:
                        nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡", callback_data=f"jobs_page:{page+1}"))
                    if nav_buttons:
                        inline_keyboard.append(nav_buttons)
                    keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
                    page_info = f"Страница {page+1} из {total_pages}"
                    await callback_query.message.edit_text(
                        f"<pre>{header}\n{batch_text}\n{page_info}</pre>\n\nВыберите задачу для подробной информации:",
                        parse_mode="HTML",
                        reply_markup=keyboard
                    )
                else:
                    await callback_query.message.edit_text(f"Ошибка: {resp.status}")
        except Exception as e:
            await callback_query.message.edit_text(f"Ошибка при запросе: {e}")

# Обрабатывает повторную постановку задачи (Requeue).

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

# Обрабатывает возобновление задачи (Resume).

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
                        action_button = InlineKeyboardButton(text="⏸️ Suspend", callback_data=f"suspend_job:{job_id}")
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

# Обрабатывает приостановку задачи (Suspend).

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
                        action_button = InlineKeyboardButton(text="▶️ Resume", callback_data=f"resume_job:{job_id}")
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

#
# --- Вспомогательные функции для Dropbox EXR ---
#
# Обрабатывает предпросмотр пути задачи (Preview Job Path).

@dp_router.callback_query(lambda c: c.data and c.data.startswith("preview_job:"))
async def preview_job_callback(callback_query: types.CallbackQuery):
    global current_downloads
    user_id = callback_query.from_user.id
    login, password, _ = get_auth_credentials(user_id)
    if not login or not password:
        await callback_query.answer("Сначала введите логин и пароль с помощью /start.", show_alert=True)
        return
    job_id = callback_query.data.split(":", 1)[1]
    # Проверяем лимит параллельных загрузок
    if current_downloads >= MAX_CONCURRENT_DOWNLOADS:
        await callback_query.message.answer(
            "Сейчас выполняются максимальное количество скачиваний. "
            "Пожалуйста, подождите завершения текущих задач."
        )
        return
    current_downloads += 1
    stop_event = asyncio.Event()
    stop_downloads[job_id] = stop_event
    async with aiohttp.ClientSession() as session:
        try:
            headers = aiohttp.BasicAuth(login, password)
            async with session.get(f"{BASE_API_URL}/jobs", auth=headers, ssl=False) as resp:
                if resp.status != 200:
                    await callback_query.answer(f"Ошибка при запросе: {resp.status}", show_alert=True)
                    return
                jobs = await resp.json()
                matching_jobs = [j for j in jobs if j.get("_id") == job_id]
                if not matching_jobs:
                    await callback_query.answer("Задача не найдена.", show_alert=True)
                    return
                job_obj = matching_jobs[0]
                outdirs = job_obj.get("OutDir", [])
                if not outdirs:
                    await callback_query.answer("Поле OutDir отсутствует.", show_alert=True)
                    return
                fullpath = outdirs[0]
                # Обрезаем до корневой папки, указанной в DROPBOX_ROOT_MARKER
                idx = fullpath.find(DROPBOX_ROOT_MARKER)
                if idx != -1:
                    trimmed = fullpath[idx:]
                else:
                    await callback_query.message.answer(f"Нет доступа к файлу. Путь: {fullpath}")
                    return
                dropbox_path = "/" + trimmed.replace("\\", "/").lstrip("/")
                temp_dir = ensure_temp_dir()
                headers_dbx = {
                    "Authorization": f"Bearer {get_fresh_access_token()}",
                    "Dropbox-API-Select-User": TEAM_MEMBER_ID,
                    "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": ROOT_NAMESPACE_ID}),
                    "Content-Type": "application/json"
                }
                async with aiohttp.ClientSession() as session_dbx:
                    try:
                        # --- Получение метаданных через новую функцию ---
                        metadata = await fetch_dropbox_metadata(session_dbx, dropbox_path, headers_dbx)
                    except Exception as e:
                        await callback_query.message.answer(str(e))
                        return

                    # Проверяем, существует ли видео уже на Dropbox
                    exr_parent = str(PurePosixPath(metadata["path_display"]).parent)
                    video_filename = f"{metadata['name']}.mp4"
                    video_dropbox_path = f"{exr_parent}/{video_filename}"
                    try:
                        # Если файл найден, fetch_dropbox_metadata не бросит ошибку
                        await fetch_dropbox_metadata(session_dbx, video_dropbox_path, headers_dbx)
                        # Формируем клавиатуру для выбора действия
                        send_button = InlineKeyboardButton(
                            text="Отправить с Dropbox",
                            callback_data=f"send_dbx_video:{job_id}"
                        )
                        recreate_button = InlineKeyboardButton(
                            text="Создать заново",
                            callback_data=f"create_new_video:{job_id}"
                        )
                        keyboard = InlineKeyboardMarkup(
                            inline_keyboard=[[send_button, recreate_button]]
                        )
                        await callback_query.message.answer(
                            f"Видео '{video_filename}' уже существует на Dropbox.",
                            reply_markup=keyboard
                        )
                        await callback_query.answer()
                        return
                    except Exception:
                        # Файл не найден – продолжаем создавать заново
                        pass

                    exr_folder_name = metadata["name"]
                    local_root = temp_dir / exr_folder_name
                    conv_root = Path("conv") / exr_folder_name
                    video_path = conv_root / f"{exr_folder_name}.mp4"

                    # Этап 1: Скачивание файлов (если ещё не скачаны)
                    exr_files_exist = lambda folder: folder.exists() and any(str(f).endswith(".exr") for f in folder.glob("*.exr"))
                    if exr_files_exist(local_root):
                        stage_msg = await callback_query.message.answer("Этап 1: Файлы уже скачаны, пропускаем скачивание.")
                    else:
                        try:
                            if metadata.get(".tag") == "file":
                                stage_msg = await callback_query.message.answer("Загрузка файла не поддерживается для предпросмотра.")
                                return
                            elif metadata.get(".tag") == "folder":
                                # Send a single message for downloading and keep editing it
                                stop_button = InlineKeyboardButton(text="Stop", callback_data=f"stop_download:{job_id}")
                                stop_kb = InlineKeyboardMarkup(inline_keyboard=[[stop_button]])
                                stage_msg = await callback_query.message.answer("Этап 1: Скачивание 0%", reply_markup=stop_kb)
                                download_url = "https://content.dropboxapi.com/2/files/download"
                                total_files = await count_exr_files(session_dbx, metadata["path_display"], headers_dbx)
                                downloaded_count = 0
                                download_states[job_id] = {
                                    "total_files": total_files,
                                    "downloaded_count": downloaded_count,
                                    "progress_msg": stage_msg,
                                    "stop_kb": stop_kb
                                }
                                local_root.mkdir(exist_ok=True)
                                await download_exr_folder(session_dbx, download_url, headers_dbx, metadata["path_display"], local_root, job_id, download_states, stop_downloads)
                                if stop_downloads.get(job_id) and stop_downloads[job_id].is_set():
                                    stop_downloads.pop(job_id, None)
                                    await stage_msg.edit_text("Скачивание остановлено пользователем.")
                                    return
                                stop_downloads.pop(job_id, None)
                                await stage_msg.edit_text("Этап 1: Скачивание 100%")
                            else:
                                stage_msg = await callback_query.message.answer("Неподдерживаемый тип метаданных.")
                                return
                        except Exception as e:
                            if stop_downloads.get(job_id) and stop_downloads[job_id].is_set():
                                return
                            await stage_msg.edit_text(f"Ошибка при скачивании: {e}")
                            return

                    try:
                        # --- Блок конвертации кадров ---
                        await stage_msg.edit_text("Этап 2: Конвертация кадров из ACES в sRGB...")
                        async with conversion_semaphore:
                            loop = asyncio.get_event_loop()
                            await loop.run_in_executor(None, convert_exr_folder_to_srgb, local_root, conv_root, "config.ocio")
                    except Exception as e:
                        await stage_msg.edit_text(f"Ошибка при конвертации: {e}")
                        return

                    try:
                        # --- Блок сборки видео ---
                        await stage_msg.edit_text("Этап 3: Сборка видео из кадров...")
                        async with conversion_semaphore:
                            loop = asyncio.get_event_loop()
                            video_path = await loop.run_in_executor(None, assemble_video_from_exr, conv_root, exr_folder_name)
                        await callback_query.message.answer_document(document=types.FSInputFile(str(video_path)))
                        await stage_msg.delete()
                    except Exception as e:
                        await stage_msg.edit_text(f"Ошибка при сборке видео: {e}")
                        return

                    try:
                        # --- Dropbox upload after Telegram send ---
                        dropbox_path = await upload_video_to_dropbox(video_path, metadata)
                        await callback_query.message.answer("Видео загружено на Dropbox")
                    except Exception as e:
                        await callback_query.message.answer(str(e))
                        return

                    # Очистка папок после отправки видео
                    cleanup_temp_and_conv()
                    download_states.pop(job_id, None)
        except Exception as e:
            await callback_query.answer(f"Ошибка при обработке Preview: {e}", show_alert=True)
        finally:
            current_downloads -= 1

# Handler for send_dbx_video
@dp_router.callback_query(lambda c: c.data and c.data.startswith("send_dbx_video:"))
async def send_dbx_video(callback_query: types.CallbackQuery):
    job_id = callback_query.data.split(":", 1)[1]
    # Повторяем получение метаданных папки
    async with aiohttp.ClientSession() as session:
        headers_auth = aiohttp.BasicAuth(*get_auth_credentials(callback_query.from_user.id)[:2])
        async with session.get(f"{BASE_API_URL}/jobs", auth=headers_auth, ssl=False) as resp:
            jobs = await resp.json() if resp.status == 200 else []
        matching = [j for j in jobs if j.get("_id") == job_id]
        if not matching:
            await callback_query.message.answer("Задача не найдена.")
            return
        outdirs = matching[0].get("OutDir", [])
        if not outdirs:
            await callback_query.message.answer("OutDir отсутствует.")
            return
        fullpath = outdirs[0]
        # Ищем индекс первого вхождения корневой папки, указанной в DROPBOX_ROOT_MARKER
        idx = fullpath.find(DROPBOX_ROOT_MARKER)
        if idx != -1:
            trimmed = fullpath[idx:]
        else:
            await callback_query.message.answer(f"Нет доступа к файлу. Путь: {fullpath}")
            return
        dropbox_path = "/" + trimmed.replace("\\", "/").lstrip("/")
        metadata = await fetch_dropbox_metadata(session, dropbox_path, {
            "Authorization": f"Bearer {get_fresh_access_token()}",
            "Dropbox-API-Select-User": TEAM_MEMBER_ID,
            "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": ROOT_NAMESPACE_ID}),
            "Content-Type": "application/json"
        })
    exr_parent = str(PurePosixPath(metadata["path_display"]).parent)
    video_filename = f"{metadata['name']}.mp4"
    video_dropbox_path = f"{exr_parent}/{video_filename}"
    download_url = "https://content.dropboxapi.com/2/files/download"
    dl_headers = {
        "Authorization": f"Bearer {get_fresh_access_token()}",
        "Dropbox-API-Select-User": TEAM_MEMBER_ID,
        "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": ROOT_NAMESPACE_ID}),
        "Dropbox-API-Arg": json.dumps({"path": video_dropbox_path})
    }
    async with aiohttp.ClientSession() as session_dbx:
        async with session_dbx.post(download_url, headers=dl_headers) as f_resp:
            if f_resp.status != 200:
                text = await f_resp.text()
                await callback_query.message.answer(f"Ошибка при загрузке видео: {text}")
                return
            temp_path = ensure_temp_dir() / video_filename
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp_path, "wb") as f:
                data = await f_resp.read()
                f.write(data)
    # Отправляем файл из локальной временной директории
    await callback_query.message.answer_document(document=types.FSInputFile(str(temp_path)), filename=video_filename)
    # Опционально: удалить временный файл после отправки
    try:
        temp_path.unlink()
    except Exception:
        pass

    await callback_query.answer()

# Handler for create_new_video
@dp_router.callback_query(lambda c: c.data and c.data.startswith("create_new_video:"))
async def create_new_video(callback_query: types.CallbackQuery):
    global current_downloads
    # Просто повторяем логику из preview_job_callback без проверки существования
    job_id = callback_query.data.split(":", 1)[1]
    # Проверяем лимит параллельных загрузок
    if current_downloads >= MAX_CONCURRENT_DOWNLOADS:
        await callback_query.message.answer(
            "Сейчас выполняются максимальное количество скачиваний. "
            "Пожалуйста, подождите завершения текущих задач."
        )
        return
    current_downloads += 1
    stop_event = asyncio.Event()
    stop_downloads[job_id] = stop_event
    await callback_query.answer()
    async with aiohttp.ClientSession() as session:
        try:
            headers = aiohttp.BasicAuth(*get_auth_credentials(callback_query.from_user.id)[:2])
            async with session.get(f"{BASE_API_URL}/jobs", auth=headers, ssl=False) as resp:
                if resp.status != 200:
                    await callback_query.answer(f"Ошибка при запросе: {resp.status}", show_alert=True)
                    return
                jobs = await resp.json()
            matching_jobs = [j for j in jobs if j.get("_id") == job_id]
            if not matching_jobs:
                await callback_query.answer("Задача не найдена.", show_alert=True)
                return
            job_obj = matching_jobs[0]
            outdirs = job_obj.get("OutDir", [])
            if not outdirs:
                await callback_query.answer("Поле OutDir отсутствует.", show_alert=True)
                return
            fullpath = outdirs[0]
            # Ищем индекс вхождения корневой папки в DROPBOX_ROOT_MARKER
            idx = fullpath.find(DROPBOX_ROOT_MARKER)
            if idx != -1:
                trimmed = fullpath[idx:]
            else:
                await callback_query.message.answer(f"Нет доступа к файлу. Путь: {fullpath}")
                return
            dropbox_path = "/" + trimmed.replace("\\", "/").lstrip("/")
            temp_dir = ensure_temp_dir()
            headers_dbx = {
                "Authorization": f"Bearer {get_fresh_access_token()}",
                "Dropbox-API-Select-User": TEAM_MEMBER_ID,
                "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": ROOT_NAMESPACE_ID}),
                "Content-Type": "application/json"
            }
            async with aiohttp.ClientSession() as session_dbx:
                try:
                    metadata = await fetch_dropbox_metadata(session_dbx, dropbox_path, headers_dbx)
                except Exception as e:
                    await callback_query.message.answer(str(e))
                    return
                exr_folder_name = metadata["name"]
                local_root = temp_dir / exr_folder_name
                conv_root = Path("conv") / exr_folder_name
                video_path = conv_root / f"{exr_folder_name}.mp4"
                exr_files_exist = lambda folder: folder.exists() and any(str(f).endswith(".exr") for f in folder.glob("*.exr"))
                if exr_files_exist(local_root):
                    stage_msg = await callback_query.message.answer("Этап 1: Файлы уже скачаны, пропускаем скачивание.")
                else:
                    try:
                        if metadata.get(".tag") == "file":
                            stage_msg = await callback_query.message.answer("Загрузка файла не поддерживается для предпросмотра.")
                            return
                        elif metadata.get(".tag") == "folder":
                            # Send a single message for downloading and keep editing it
                            stop_button = InlineKeyboardButton(text="Stop", callback_data=f"stop_download:{job_id}")
                            stop_kb = InlineKeyboardMarkup(inline_keyboard=[[stop_button]])
                            stage_msg = await callback_query.message.answer("Этап 1: Скачивание 0%", reply_markup=stop_kb)
                            download_url = "https://content.dropboxapi.com/2/files/download"
                            total_files = await count_exr_files(session_dbx, metadata["path_display"], headers_dbx)
                            downloaded_count = 0
                            download_states[job_id] = {
                                "total_files": total_files,
                                "downloaded_count": downloaded_count,
                                "progress_msg": stage_msg,
                                "stop_kb": stop_kb
                            }
                            local_root.mkdir(exist_ok=True)
                            await download_exr_folder(session_dbx, download_url, headers_dbx, metadata["path_display"], local_root, job_id, download_states, stop_downloads)
                            if stop_downloads.get(job_id) and stop_downloads[job_id].is_set():
                                stop_downloads.pop(job_id, None)
                                await stage_msg.edit_text("Скачивание остановлено пользователем.")
                                return
                            stop_downloads.pop(job_id, None)
                            await stage_msg.edit_text("Этап 1: Скачивание 100%")
                        else:
                            stage_msg = await callback_query.message.answer("Неподдерживаемый тип метаданных.")
                            return
                    except Exception as e:
                        if stop_downloads.get(job_id) and stop_downloads[job_id].is_set():
                            return
                        await stage_msg.edit_text(f"Ошибка при скачивании: {e}")
                        return
                try:
                    await stage_msg.edit_text("Этап 2: Конвертация кадров из ACES в sRGB...")
                    async with conversion_semaphore:
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, convert_exr_folder_to_srgb, local_root, conv_root, "config.ocio")
                except Exception as e:
                    await stage_msg.edit_text(f"Ошибка при конвертации: {e}")
                    return
                try:
                    await stage_msg.edit_text("Этап 3: Сборка видео из кадров...")
                    async with conversion_semaphore:
                        loop = asyncio.get_event_loop()
                        video_path = await loop.run_in_executor(None, assemble_video_from_exr, conv_root, exr_folder_name)
                    await callback_query.message.answer_document(document=types.FSInputFile(str(video_path)))
                    await stage_msg.delete()
                except Exception as e:
                    await stage_msg.edit_text(f"Ошибка при сборке видео: {e}")
                    return
                try:
                    dropbox_path2 = await upload_video_to_dropbox(video_path, metadata)
                    await callback_query.message.answer("Видео загружено на Dropbox")
                except Exception as e:
                    await callback_query.message.answer(str(e))
                    return
                cleanup_temp_and_conv()
                download_states.pop(job_id, None)
        finally:
            current_downloads -= 1

# Обрабатывает удаление задачи (Delete).

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

# Обрабатывает выход пользователя из аккаунта ("🚪 Выход").

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

# Обрабатывает режим реального времени обновления задач по кнопке "Realtime".

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

                        from datetime import datetime
                        combined_jobs = []
                        for batch, jobs in grouped_jobs.items():
                            total_tasks = sum(j.get("Props", {}).get("Tasks", 0) for j in jobs)
                            completed_chunks = sum(j.get("CompletedChunks", 0) for j in jobs)
                            # Determine batch-level status with priority: Active > Pending > Suspended > Failed > Completed > Unknown
                            status_list = [j.get("Stat", 0) for j in jobs]
                            if 1 in status_list:
                                batch_stat = 1
                            elif 6 in status_list:
                                batch_stat = 6
                            elif 2 in status_list:
                                batch_stat = 2
                            elif 4 in status_list:
                                batch_stat = 4
                            elif all(s == 3 for s in status_list):
                                batch_stat = 3
                            else:
                                batch_stat = 0
                            # Determine the most recent Date among jobs in this batch
                            dates = []
                            for j in jobs:
                                date_str = j.get("Date")
                                if date_str:
                                    try:
                                        dates.append(datetime.fromisoformat(date_str))
                                    except Exception:
                                        pass
                            max_date = max(dates) if dates else datetime.min
                            combined_jobs.append({
                                "Props": {"Batch": batch, "Tasks": total_tasks},
                                "CompletedChunks": completed_chunks,
                                "Stat": batch_stat,
                                "DateParsed": max_date
                            })
                        # Sort by DateParsed descending (newest first)
                        combined_jobs.sort(key=lambda j: j["DateParsed"], reverse=True)

                        normal_jobs = [job for job in combined_jobs if job.get("Stat", 0) != 2]
                        suspended_jobs = [job for job in combined_jobs if job.get("Stat", 0) == 2]

                        messages = []
                        for job in normal_jobs:
                            props = job.get("Props", {})
                            batch = props.get("Batch", "Без имени")
                            total_tasks = props.get("Tasks", 0)
                            completed_chunks = job.get("CompletedChunks", 0)
                            progress_str = format_progress(completed_chunks, total_tasks)
                            stat = job.get("Stat", 0)
                            if stat == 3:
                                icon = "✅"
                            else:
                                icon = "▶️"
                            messages.append(f"{icon} {batch:<22} {progress_str:^16}\n{'-'*40}")

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
                                messages.append(f"⏸️ {batch:<22} {progress_str:^16}\n{'-'*40}")

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


@dp_router.callback_query(lambda c: c.data and c.data.startswith("stop_download:"))
async def stop_download_callback(callback_query: types.CallbackQuery):
    download_job_id = callback_query.data.split(":", 1)[1]
    event = stop_downloads.get(download_job_id)
    if event:
        event.set()
        try:
            await callback_query.answer("Загрузка остановлена", show_alert=True)
            # Убираем кнопку Stop
            await callback_query.message.edit_reply_markup(reply_markup=None)
            # Очистка папок после остановки скачивания
            clear_folder("temp")
            clear_folder("conv")
        except TelegramBadRequest:
            pass
    else:
        try:
            await callback_query.answer("Загрузка уже завершена или не найдена", show_alert=True)
        except TelegramBadRequest:
            pass

# --- End of Handlers ---

# Главная функция запускает бота и watcher уведомлений.
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