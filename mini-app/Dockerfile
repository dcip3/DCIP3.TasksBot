FROM node:18-alpine

# Установка рабочей директории
WORKDIR /app

# Копирование package.json и package-lock.json
COPY package*.json ./

# Установка всех зависимостей (включая dev-зависимости для сборки)
RUN npm ci

# Копирование исходного кода
COPY . .

# Сборка приложения
RUN npm run build

# Установка serve для раздачи статических файлов
RUN npm install -g serve

# Удаление dev-зависимостей для уменьшения размера образа
RUN npm prune --production

# Открытие порта
EXPOSE 3000

# Команда запуска
CMD ["serve", "-s", "dist", "-l", "3000"] 