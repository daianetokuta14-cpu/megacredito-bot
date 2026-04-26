FROM python:3.12-slim
WORKDIR /app
COPY requirements_bot.txt .
RUN pip install --no-cache-dir -r requirements_bot.txt
COPY bot.py .
EXPOSE 5001
CMD ["gunicorn", "bot:app", "--bind", "0.0.0.0:5001", "--workers", "1", "--timeout", "120"]
