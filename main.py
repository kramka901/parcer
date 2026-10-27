"""
Точка входа для хостинга Deployka, Docker и системных служб.
Запускает основного бота школы Tallinna Mustjõe Gümnaasium (bot.py).
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bot import main

if __name__ == "__main__":
    main()
