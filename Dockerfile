FROM python:3.12-slim
WORKDIR /app
COPY requirements_.txt .
RUN pip install --no-cache-dir -r requirements_.txt
COPY bot.py .
EXPOSE 5001
CMD ["gunicorn", "bot:app", "--bind", "0.0.0.0:5001", "--workers", "1", "--timeout", "120"]
