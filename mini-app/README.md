# TasksBot Mini App

Telegram Mini App для управления задачами Deadline через веб-интерфейс.

## Возможности

- 🔐 Аутентификация через Deadline credentials
- 📋 Просмотр списка задач (Jobs) с группировкой по батчам
- 👥 Мониторинг воркеров (Workers)
- ⚡ Управление задачами: requeue, resume, suspend, delete
- 📊 Отображение прогресса выполнения задач
- 🎨 Современный UI с адаптивным дизайном
- 🔄 Автоматическое обновление данных

## Установка

1. Установите зависимости:
```bash
npm install
```

2. Создайте файл `.env` в корне проекта:
```env
VITE_API_URL=http://localhost:8000
```

3. Запустите сервер разработки:
```bash
npm run dev
```

## Сборка для продакшена

```bash
npm run build
```

## Интеграция с Telegram Bot

Для работы mini app необходимо добавить API endpoints в ваш бот:

### Аутентификация
- `POST /api/auth/login` - вход пользователя
- `POST /api/auth/logout` - выход пользователя  
- `GET /api/auth/check` - проверка аутентификации

### Задачи
- `GET /api/jobs` - получение списка задач
- `GET /api/jobs/{id}` - детали задачи
- `GET /api/jobs/{id}/tasks` - задачи в рамках job
- `PUT /api/jobs/{id}/requeue` - перезапуск задачи
- `PUT /api/jobs/{id}/resume` - возобновление задачи
- `PUT /api/jobs/{id}/suspend` - приостановка задачи
- `DELETE /api/jobs/{id}` - удаление задачи

### Воркеры
- `GET /api/workers` - получение списка воркеров

## Структура проекта

```
mini-app/
├── src/
│   ├── components/     # React компоненты
│   ├── services/       # API сервисы
│   ├── types/          # TypeScript типы
│   ├── utils/          # Утилиты
│   ├── App.tsx         # Главный компонент
│   └── main.tsx        # Entry point
├── public/             # Статические файлы
├── package.json        # Зависимости
└── README.md          # Документация
```

## Технологии

- **React 18** - UI библиотека
- **TypeScript** - типизация
- **Tailwind CSS** - стилизация
- **Vite** - сборщик
- **Axios** - HTTP клиент
- **Lucide React** - иконки
- **Telegram WebApp API** - интеграция с Telegram

## Разработка

### Добавление новых компонентов

1. Создайте файл в `src/components/`
2. Экспортируйте компонент как default или named export
3. Импортируйте в нужном месте

### Добавление новых API endpoints

1. Добавьте метод в соответствующий API сервис в `src/services/api.ts`
2. Используйте в компонентах через хуки или напрямую

### Стилизация

Используйте Tailwind CSS классы для стилизации. Основные цвета:
- Primary: `primary-500`, `primary-600`, `primary-700`
- Success: `green-500`, `green-600`
- Warning: `yellow-500`, `yellow-600`
- Error: `red-500`, `red-600` 