FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    RELOAD=0 \
    SHOW=0 \
    DATABASE_PATH=/data/lookingfor.db \
    NICEGUI_STORAGE_PATH=/data/.nicegui

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /data

EXPOSE 8080

CMD ["python", "main.py"]
