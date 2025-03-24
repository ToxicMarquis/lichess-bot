from flask import Flask
import threading
import time

app = Flask(__name__)

def bot_task():
    while True:
        print("Бот работает...")
        time.sleep(10)  # Пример задачи, которая выполняется каждые 10 секунд

@app.route('/')
def home():
    return "Бот работает в фоновом режиме!"

if __name__ == '__main__':
    # Запуск бота в отдельном потоке
    bot_thread = threading.Thread(target=bot_task)
    bot_thread.daemon = True
    bot_thread.start()

    # Запуск Flask-сервера
    app.run(host='0.0.0.0', port=8080)