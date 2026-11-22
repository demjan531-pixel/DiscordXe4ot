FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATABASE_PATH=/app/data/tasks.sqlite3
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py tracker_store.py ./
CMD ["python", "bot.py"]
