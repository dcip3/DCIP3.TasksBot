FROM python:3.11-slim

# Установка системных зависимостей
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libopenexr-dev \
    libopencolorio-dev \
    && rm -rf /var/lib/apt/lists/*

# Установка рабочей директории
WORKDIR /app

# Копирование файлов зависимостей
COPY requirements.txt .

# Установка Python зависимостей
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода
COPY app/ ./app/
COPY main_api.py .
COPY config.ocio .

# Создание необходимых директорий
RUN mkdir -p conv temp

# Установка прав доступа
RUN chmod +x main_api.py

# Открытие порта
EXPOSE 8000

# Команда запуска
CMD ["python", "main_api.py"] 