FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
EXPOSE 5001
CMD ["gunicorn", "bot:app", "--bind", "0.0.0.0:5001", "--workers", "1", "--timeout", "120"]
