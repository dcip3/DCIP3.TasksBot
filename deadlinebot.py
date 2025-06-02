import asyncio
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path, PurePosixPath

import aiohttp
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

# Токен API и базовый URL API
API_TOKEN = "REDACTED_TELEGRAM_BOT_TOKEN"
BASE_API_URL = "https://renderfarm.local:4434/api"

CREDENTIALS_FILE = Path("credentials.json")

# Dropbox access token (создайте приложение в Dropbox и получите токен доступа)
DROPBOX_ACCESS_TOKEN = "REDACTED_DROPBOX_ACCESS_TOKEN"
# Для доступа к Team Space необходимы team_member_id и root_namespace_id
TEAM_MEMBER_ID = "REDACTED_DROPBOX_TEAM_MEMBER_ID"
ROOT_NAMESPACE_ID = "REDACTED_DROPBOX_ROOT_NAMESPACE_ID"

bot = Bot(token=API_TOKEN)
dp_router = Router()

# Переменная для хранения состояния загрузки (для возобновления)
download_states = {}

# Для хранения флагов остановки загрузок (download_folder)
stop_downloads = {}

# Для уведомлений о завершённых задачах
notified_jobs = set()

# Активные задачи в режиме реального времени
active_realtime_tasks = {}

# Очистка папки
def clear_folder(folder_path):
    if os.path.exists(folder_path):
        shutil.rmtree(folder_path, ignore_errors=True)
    os.makedirs(folder_path, exist_ok=True)


# --- Новые функции для preview_job_callback ---

async def fetch_dropbox_metadata(session_dbx, dropbox_path: str, headers_dbx: dict) -> dict:
    meta_url = "https://api.dropboxapi.com/2/files/get_metadata"
    async with session_dbx.post(meta_url, headers=headers_dbx, json={"path": dropbox_path}) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"Ошибка при получении метаданных: {text}")
        return await resp.json()

def convert_exr_folder_to_srgb(local_root: Path, conv_root: Path, ocio_config_path: str):
    conv_root.mkdir(parents=True, exist_ok=True)
    exr_files = sorted([f for f in local_root.glob("*.exr") if "cryptomatte" not in f.name.lower()])
    if not exr_files:
        raise RuntimeError("Не найдено EXR-кадров для конвертации.")

    if not Path(ocio_config_path).exists():
        raise RuntimeError(f"OCIO config не найден: {ocio_config_path}")
    config = ocio.Config.CreateFromFile(str(ocio_config_path))
    transform = ocio.DisplayViewTransform()
    transform.setSrc("ACEScg")
    transform.setDisplay("sRGB")
    transform.setView("ACES 1.0 SDR-video")
    transform.setDirection(ocio.TRANSFORM_DIR_FORWARD)
    processor = config.getProcessor(transform)
    cpu_processor = processor.getDefaultCPUProcessor()

    first_exr = exr_files[0]
    exr_file = OpenEXR.InputFile(str(first_exr))
    header = exr_file.header()
    dw = header["dataWindow"]
    width = dw.max.x - dw.min.x + 1
    height = dw.max.y - dw.min.y + 1

    FLOAT = Imath.PixelType(Imath.PixelType.FLOAT)
    for exr_path in exr_files:
        exr = OpenEXR.InputFile(str(exr_path))
        channels = exr.channels(["R", "G", "B"], FLOAT)
        r = np.frombuffer(channels[0], dtype=np.float32).reshape((height, width))
        g = np.frombuffer(channels[1], dtype=np.float32).reshape((height, width))
        b = np.frombuffer(channels[2], dtype=np.float32).reshape((height, width))
        rgb = np.stack([r, g, b], axis=-1)
        flat_image = rgb.reshape(-1, 3).astype(np.float32)

        img_desc = ocio.PackedImageDesc(flat_image, width, height, 3)
        cpu_processor.apply(img_desc)
        img = flat_image.reshape(height, width, 3)

        out_exr_path = conv_root / exr_path.name
        header_out = OpenEXR.Header(width, height)
        half_chan = Imath.Channel(Imath.PixelType(Imath.PixelType.HALF))
        header_out["channels"] = {"R": half_chan, "G": half_chan, "B": half_chan}
        out_exr = OpenEXR.OutputFile(str(out_exr_path), header_out)
        r_half = (img[:, :, 0].astype(np.float16)).tobytes()
        g_half = (img[:, :, 1].astype(np.float16)).tobytes()
        b_half = (img[:, :, 2].astype(np.float16)).tobytes()
        out_exr.writePixels({"R": r_half, "G": g_half, "B": b_half})
        out_exr.close()

def assemble_video_from_exr(conv_root: Path, exr_folder_name: str) -> Path:
    video_path = conv_root / f"{exr_folder_name}.mp4"
    exr_pattern = str(conv_root / "*.exr")
    if not any(conv_root.glob("*.exr")):
        raise RuntimeError("Нет кадров для сборки видео.")
    cmd = [
        "ffmpeg",
        "-y",
        "-pattern_type", "glob",
        "-i", exr_pattern,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        str(video_path)
    ]
    subprocess.run(cmd, check=True)
    return video_path

async def upload_video_to_dropbox(video_path: Path, metadata: dict):
    filename = video_path.name
    exr_parent = str(PurePosixPath(metadata["path_display"]).parent)
    dropbox_upload_path = f"{exr_parent}/{filename}"

    upload_url = "https://content.dropboxapi.com/2/files/upload"
    headers_upload = {
        "Authorization": f"Bearer {DROPBOX_ACCESS_TOKEN}",
        "Dropbox-API-Select-User": TEAM_MEMBER_ID,
        "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": ROOT_NAMESPACE_ID}),
        "Dropbox-API-Arg": json.dumps({"path": dropbox_upload_path, "mode": "overwrite"}),
        "Content-Type": "application/octet-stream"
    }
    data = video_path.read_bytes()
    async with aiohttp.ClientSession() as session_upload:
        async with session_upload.post(upload_url, headers=headers_upload, data=data) as resp_up:
            if resp_up.status != 200:
                text = await resp_up.text()
                raise RuntimeError(f"Ошибка при загрузке видео на Dropbox: {text}")
    return dropbox_upload_path

def cleanup_temp_and_conv():
    clear_folder("temp")
    clear_folder("conv")

# --- Credential and helper functions ---

class AuthStates(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()

def load_credentials():
    if CREDENTIALS_FILE.exists():
        with CREDENTIALS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}

# Initialize user_credentials after definition of load_credentials
user_credentials = load_credentials()

# Сохраняет учётные данные пользователей в файл.
def save_credentials(data):
    with CREDENTIALS_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f)

# Получает логин, пароль и статус уведомлений для указанного пользователя.
def get_auth_credentials(user_id):
    creds = user_credentials.get(str(user_id))
    if not creds or len(creds) < 2:
        return None, None, False
    login, password = creds[0], creds[1]
    notifications_enabled = creds[2] if len(creds) > 2 else False
    return login, password, notifications_enabled

# Возвращает основную клавиатуру для управления ботом.
def get_main_keyboard():
    kb = [
        [KeyboardButton(text="Jobs"), KeyboardButton(text="Realtime"), KeyboardButton(text="Workers")],
        [KeyboardButton(text="🔔 Уведомления"), KeyboardButton(text="🚪 Выход")],
        [KeyboardButton(text="🧹 Очистить")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

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
    user_credentials[str(message.from_user.id)] = [login, password]
    save_credentials(user_credentials)
    if len(user_credentials[str(message.from_user.id)]) == 2:
        user_credentials[str(message.from_user.id)].append(False)
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
                        preview_button = InlineKeyboardButton(text="Preview", callback_data=f"preview_job:{job_id_btn}")
                        if stat == 3:
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [requeue_button, delete_button, preview_button]
                            ])
                        else:
                            if stat == 2:
                                action_button = InlineKeyboardButton(text="✅ Resume", callback_data=f"resume_job:{job_id_btn}")
                            else:
                                action_button = InlineKeyboardButton(text="❌ Suspend", callback_data=f"suspend_job:{job_id_btn}")
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [action_button, requeue_button],
                                [delete_button, preview_button]
                            ])
                        await callback_query.message.answer("Действия:", reply_markup=keyboard)
                    await callback_query.answer()
                else:
                    await callback_query.answer(f"Ошибка при запросе: {resp.status}", show_alert=True)
        except Exception as e:
            await callback_query.answer(f"Ошибка при запросе: {e}", show_alert=True)


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
                    # Кнопки навигации: Назад и Вперёд ➡
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

#
# --- Вспомогательные функции для Dropbox EXR ---
#
# Считает количество EXR-файлов (исключая cryptomatte) рекурсивно в папке Dropbox
async def count_exr_files(session_dbx, path, headers_dbx):
    list_url = "https://api.dropboxapi.com/2/files/list_folder"
    async with session_dbx.post(list_url, headers=headers_dbx, json={"path": path}) as list_resp:
        if list_resp.status not in (0, 200):
            return 0
        result = await list_resp.json()
    count = 0
    for entry in result.get("entries", []):
        name = entry["name"]
        if "cryptomatte" in name.lower():
            continue
        if entry[".tag"] == "file" and name.lower().endswith(".exr"):
            count += 1
        elif entry[".tag"] == "folder":
            count += await count_exr_files(session_dbx, entry["path_display"], headers_dbx)
    return count

# Рекурсивно скачивает EXR-файлы из папки Dropbox в локальную директорию
async def download_exr_folder(session_dbx, download_url, headers_dbx, path, local_folder, job_id):
    state = download_states.get(job_id)
    downloaded_count = state["downloaded_count"]
    list_url = "https://api.dropboxapi.com/2/files/list_folder"
    async with session_dbx.post(list_url, headers=headers_dbx, json={"path": path}) as list_resp:
        if list_resp.status not in (0, 200):
            return
        result = await list_resp.json()
    for entry in result.get("entries", []):
        name = entry["name"]
        if "cryptomatte" in name.lower():
            continue
        if entry[".tag"] == "file" and name.lower().endswith(".exr"):
            local_file = local_folder / name
            dl_headers = {
                "Authorization": f"Bearer {DROPBOX_ACCESS_TOKEN}",
                "Dropbox-API-Select-User": TEAM_MEMBER_ID,
                "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": ROOT_NAMESPACE_ID}),
                "Dropbox-API-Arg": json.dumps({"path": entry["path_display"]})
            }
            async with session_dbx.post(download_url, headers=dl_headers) as f_resp:
                if f_resp.status != 200:
                    continue
                data = await f_resp.read()
                with open(local_file, "wb") as f:
                    f.write(data)
            if stop_downloads.get(job_id) and stop_downloads[job_id].is_set():
                return
            downloaded_count += 1
            download_states[job_id]["downloaded_count"] = downloaded_count
            total_files = state["total_files"]
            percent = int((downloaded_count / total_files) * 100) if total_files else 0
            progress_msg = state["progress_msg"]
            try:
                await progress_msg.edit_text(f"Скачивание {percent}%", reply_markup=state["stop_kb"])
            except Exception:
                pass
        elif entry[".tag"] == "folder":
            subfolder = local_folder / name
            subfolder.mkdir(exist_ok=True)
            await download_exr_folder(session_dbx, download_url, headers_dbx, entry["path_display"], subfolder, job_id)
            if stop_downloads.get(job_id) and stop_downloads[job_id].is_set():
                return

# Обрабатывает предпросмотр пути задачи (Preview Job Path).

@dp_router.callback_query(lambda c: c.data and c.data.startswith("preview_job:"))
async def preview_job_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    login, password, _ = get_auth_credentials(user_id)
    if not login or not password:
        await callback_query.answer("Сначала введите логин и пароль с помощью /start.", show_alert=True)
        return
    job_id = callback_query.data.split(":", 1)[1]
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
                if len(fullpath) > 1 and fullpath[1] == ":":
                    trimmed = fullpath[2:]
                else:
                    trimmed = fullpath
                if not trimmed.startswith("\\"):
                    trimmed = "\\" + trimmed
                dropbox_path = trimmed.replace("\\", "/")
                temp_dir = ensure_temp_dir()
                headers_dbx = {
                    "Authorization": f"Bearer {DROPBOX_ACCESS_TOKEN}",
                    "Dropbox-API-Select-User": TEAM_MEMBER_ID,
                    "Dropbox-API-Path-Root": json.dumps({".tag": "root", "root": ROOT_NAMESPACE_ID}),
                    "Content-Type": "application/json"
                }
                async with aiohttp.ClientSession() as session_dbx:
                    # --- Получение метаданных через новую функцию ---
                    try:
                        metadata = await fetch_dropbox_metadata(session_dbx, dropbox_path, headers_dbx)
                    except Exception as e:
                        await callback_query.message.answer(str(e))
                        return

                    exr_folder_name = metadata["name"]
                    local_root = temp_dir / exr_folder_name
                    conv_root = Path("conv") / exr_folder_name
                    video_path = conv_root / f"{exr_folder_name}.mp4"

                    # Этап 1: Скачивание файлов (если ещё не скачаны)
                    exr_files_exist = lambda folder: folder.exists() and any(str(f).endswith(".exr") for f in folder.glob("*.exr"))
                    if exr_files_exist(local_root):
                        await callback_query.message.answer("Этап 1: Файлы уже скачаны, пропускаем скачивание.")
                    else:
                        try:
                            if metadata.get(".tag") == "file":
                                await callback_query.message.answer("Загрузка файла не поддерживается для предпросмотра.")
                                return
                            elif metadata.get(".tag") == "folder":
                                await callback_query.message.answer("Этап 1: Начинаем скачивание файлов...")
                                download_url = "https://content.dropboxapi.com/2/files/download"
                                # Подсчёт файлов, исключая cryptomatte
                                total_files = await count_exr_files(session_dbx, metadata["path_display"], headers_dbx)
                                downloaded_count = 0
                                stop_button = InlineKeyboardButton(text="Stop", callback_data=f"stop_download:{job_id}")
                                stop_kb = InlineKeyboardMarkup(inline_keyboard=[[stop_button]])
                                progress_msg = await callback_query.message.answer(f"Скачивание 0%", reply_markup=stop_kb)
                                download_states[job_id] = {
                                    "total_files": total_files,
                                    "downloaded_count": downloaded_count,
                                    "progress_msg": progress_msg,
                                    "stop_kb": stop_kb
                                }
                                local_root.mkdir(exist_ok=True)
                                await download_exr_folder(session_dbx, download_url, headers_dbx, metadata["path_display"], local_root, job_id)
                                if stop_downloads.get(job_id) and stop_downloads[job_id].is_set():
                                    stop_downloads.pop(job_id, None)
                                    await callback_query.message.answer("Скачивание остановлено пользователем.")
                                    return
                                stop_downloads.pop(job_id, None)
                                await progress_msg.edit_text("Скачивание 100%")
                            else:
                                await callback_query.message.answer("Неподдерживаемый тип метаданных.")
                                return
                        except Exception as e:
                            if stop_downloads.get(job_id) and stop_downloads[job_id].is_set():
                                return
                            await callback_query.message.answer(f"Ошибка при скачивании: {e}")
                            return

                    # --- Блок конвертации кадров ---
                    try:
                        await callback_query.message.answer("Этап 2: Конвертация кадров из ACES в sRGB...")
                        convert_exr_folder_to_srgb(local_root, conv_root, "config.ocio")
                    except Exception as e:
                        await callback_query.message.answer(f"Ошибка при конвертации: {e}")
                        return

                    # --- Блок сборки видео ---
                    try:
                        await callback_query.message.answer("Этап 3: Сборка видео из кадров...")
                        video_path = assemble_video_from_exr(conv_root, exr_folder_name)
                        await callback_query.message.answer_document(document=types.FSInputFile(str(video_path)))
                    except Exception as e:
                        await callback_query.message.answer(f"Ошибка при сборке видео: {e}")
                        return

                    # --- Dropbox upload after Telegram send ---
                    try:
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