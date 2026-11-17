# --- Стадия 1: собираем React-фронт мини-аппа ---
FROM node:20-slim AS frontend
WORKDIR /app/webapp/frontend
COPY webapp/frontend/package.json webapp/frontend/package-lock.json ./
# npm ci ставить рівно те, що в package-lock.json (відтворювана збірка)
RUN npm ci --no-audit --no-fund
COPY webapp/frontend/ ./
RUN npm run build
# результат сборки уляжется в /app/webapp/backend/static (см. vite.config.js outDir)

# --- Стадия 2: питоновский рантайм — бот + FastAPI ---
FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# від root не запускаємо: при зламі процесу менше прав у контейнері
RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app
# подхватываем собранную статику из первой стадии
COPY --from=frontend /app/webapp/backend/static ./webapp/backend/static

CMD ["python", "main.py"]
