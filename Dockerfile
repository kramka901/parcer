FROM python:3.12-slim

# Отключаем буферизацию вывода Python для мгновенного отображения логов
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY . .

# Запуск бота
CMD ["python", "main.py"]
