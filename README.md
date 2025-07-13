# TasksBot - Telegram Bot с Mini App

Telegram бот для управления задачами Deadline с веб-интерфейсом в виде Telegram Mini App.

## Возможности

- 🔐 Аутентификация через Deadline credentials
- 📋 Просмотр и управление задачами (jobs)
- 👥 Мониторинг воркеров (workers)
- 🎬 Создание превью видео из EXR файлов
- ☁️ Интеграция с Dropbox
- 📱 Telegram Mini App интерфейс

## Развертывание на VDS

### Предварительные требования

- Ubuntu 20.04+ или другой Linux дистрибутив
- Docker и Docker Compose
- Домен (для HTTPS)

### 1. Подготовка сервера

```bash
# Обновление системы
sudo apt update && sudo apt upgrade -y

# Установка Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Установка Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/download/v2.20.0/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# Добавление пользователя в группу docker
sudo usermod -aG docker $USER
```

### 2. Клонирование проекта

```bash
git clone <your-repo-url>
cd DCIP3.TasksBot
```

### 3. Настройка переменных окружения

Создайте файл `.env` с необходимыми переменными:

```env
# Telegram Bot
TG_API_TOKEN=your_telegram_bot_token

# Dropbox API
DROPBOX_APP_KEY=your_dropbox_app_key
DROPBOX_APP_SECRET=your_dropbox_app_secret
DROPBOX_REFRESH_TOKEN=your_dropbox_refresh_token
DROPBOX_TEAM_MEMBER_ID=your_team_member_id
DROPBOX_ROOT_NAMESPACE_ID=your_root_namespace_id
DROPBOX_ROOT_MARKER=Team Folder
```

### 4. Настройка SSL (опционально)

Для HTTPS создайте папку `ssl` и поместите туда сертификаты:

```bash
mkdir ssl
# Поместите certificate.crt и private.key в папку ssl/
```

### 5. Развертывание

#### Для разработки:
```bash
./deploy.sh
```

#### Для production:
```bash
docker-compose -f docker-compose.prod.yml up -d
```

### 6. Настройка Telegram Bot

1. Создайте бота через @BotFather
2. Получите токен и добавьте в `.env`
3. Настройте webhook (если используется):
   ```
   https://your-domain.com/webhook/set
   ```

### 7. Настройка Telegram Mini App

1. Создайте Mini App через @BotFather
2. Укажите URL: `https://your-domain.com`
3. Добавьте кнопку в меню бота

## Структура проекта

```
DCIP3.TasksBot/
├── app/                    # Backend код
│   ├── core/              # Основные модули
│   ├── api_routes.py      # API маршруты
│   ├── auth.py           # Аутентификация
│   ├── handlers.py       # Telegram handlers
│   └── services.py       # Бизнес-логика
├── mini-app/              # Frontend (React)
├── Dockerfile            # Backend контейнер
├── docker-compose.yml    # Разработка
├── docker-compose.prod.yml # Production
├── nginx.conf           # Nginx конфигурация
└── deploy.sh            # Скрипт развертывания
```

## Управление

### Просмотр логов
```bash
# Все сервисы
docker-compose logs -f

# Конкретный сервис
docker-compose logs -f backend
```

### Остановка
```bash
docker-compose down
```

### Обновление
```bash
git pull
./deploy.sh
```

## Проблемы и решения

### Проблема: Контейнеры не запускаются
- Проверьте логи: `docker-compose logs`
- Убедитесь, что все переменные окружения установлены

### Проблема: Telegram webhook не работает
- Проверьте доступность сервера извне
- Убедитесь, что порт 80/443 открыт
- Проверьте SSL сертификаты

### Проблема: Dropbox интеграция не работает
- Проверьте правильность токенов в `.env`
- Убедитесь, что приложение имеет нужные права

## Поддержка

При возникновении проблем:
1. Проверьте логи контейнеров
2. Убедитесь в правильности конфигурации
3. Проверьте доступность внешних сервисов 