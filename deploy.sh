#!/bin/bash

# Скрипт для развертывания TasksBot на VDS

set -e

echo "🚀 Начинаем развертывание TasksBot..."

# Проверка наличия Docker и Docker Compose
if ! command -v docker &> /dev/null; then
    echo "❌ Docker не установлен. Установите Docker и попробуйте снова."
    exit 1
fi

if ! command -v docker-compose &> /dev/null; then
    echo "❌ Docker Compose не установлен. Установите Docker Compose и попробуйте снова."
    exit 1
fi

# Проверка наличия .env файла
if [ ! -f .env ]; then
    echo "❌ Файл .env не найден. Создайте .env файл с необходимыми переменными окружения."
    exit 1
fi

# Остановка существующих контейнеров
echo "🛑 Останавливаем существующие контейнеры..."
docker-compose down

# Удаление старых образов
echo "🧹 Удаляем старые образы..."
docker-compose down --rmi all

# Сборка новых образов
echo "🔨 Собираем новые образы..."
docker-compose build --no-cache

# Запуск контейнеров
echo "▶️ Запускаем контейнеры..."
docker-compose up -d

# Проверка статуса
echo "📊 Проверяем статус контейнеров..."
docker-compose ps

echo "✅ Развертывание завершено!"
echo ""
echo "🌐 Доступные сервисы:"
echo "   - Backend API: http://localhost:8000"
echo "   - Frontend: http://localhost:3000"
echo ""
echo "📝 Логи контейнеров:"
echo "   docker-compose logs -f"
echo ""
echo "🛑 Остановка сервисов:"
echo "   docker-compose down" 