# --- Стадия 1: собираем React-фронт мини-аппа ---
FROM node:20-slim AS frontend
WORKDIR /app/webapp/frontend
COPY webapp/frontend/package*.json ./
RUN npm install
COPY webapp/frontend/ ./
RUN npm run build
# результат сборки уляжется в /app/webapp/backend/static (см. vite.config.js outDir)

# --- Стадия 2: питоновский рантайм — бот + FastAPI ---
FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# подхватываем собранную статику из первой стадии
COPY --from=frontend /app/webapp/backend/static ./webapp/backend/static

CMD ["python", "main.py"]
