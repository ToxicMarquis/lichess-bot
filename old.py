from flask import Flask
import threading
import chess
import chess.engine
import os
import time
import logging
import atexit
import asyncio
import aiohttp

app = Flask(__name__)

# Токен бота
API_TOKEN = os.environ['BOT_TOKEN']
BASE_URL = 'https://lichess.org/api'
STOCKFISH_PATH = './stockfish'

# Настройка логирования
logging.basicConfig(filename='bot.log', level=logging.INFO, format='%(asctime)s - %(message)s')

# Глобальная переменная для движка
engine = None

# Функция для корректного закрытия движка
def close_engine():
    global engine
    if engine:
        try:
            engine.quit()
            logging.info("Движок Stockfish закрыт.")
        except Exception as e:
            logging.error(f"Ошибка при закрытии движка: {e}")
        finally:
            engine = None

# Регистрируем функцию закрытия движка
atexit.register(close_engine)

# Инициализация движка
def init_engine():
    global engine
    try:
        if engine is None:
            engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
            logging.info("Движок Stockfish инициализирован.")
    except Exception as e:
        logging.error(f"Ошибка при инициализации движка: {e}")
        engine = None

# Конфигурация фильтров
ACCEPTANCE_CRITERIA = {
    'min_rating': 1000,
    'max_rating': 3000,
    'time_controls': [
        (180, 0),
        (180, 2),
        (300, 0),
        (300, 5),
        (600, 0),
        (600, 10),
    ],
    'variants': [
        'standard',
        'chess960',
        'crazyhouse',
        'atomic',
    ],
    'modes': [
        'casual',
        'rated',
    ],
    'deny_bots': True, # Отклонять вызовы от ботов
}

def parse_time_control(tc):
    """Парсит объект временного контроля в (основное время, добавка)"""
    if tc['type'] == 'unlimited':
        return (0, 0)

    if tc['type'] == 'correspondence':
        days_per_move = tc.get('daysPerTurn', 1)
        return (days_per_move * 86400, 0)

    # Для стандартных контролей: clockLimit + clockIncrement
    return (tc.get('limit', 0), tc.get('increment', 0))

def is_challenge_acceptable(challenge):
    """Проверяет соответствует ли вызов критериям приемлемости"""
    try:
        challenger = challenge.get('challenger', {})
        tc = challenge.get('timeControl', {})

        # Базовые проверки структуры
        if not isinstance(tc, dict):
            return False, "Некорректный формат временного контроля"

        # Фильтр по типу временного контроля
        tc_type = tc.get('type')
        if tc_type not in ['clock', 'correspondence', 'unlimited']:
            return False, "Неподдерживаемый тип игры"

        # Парсинг времени
        parsed_tc = parse_time_control(tc)

        # Фильтр по рейтингу
        if challenger.get('rating'):
            rating = challenger['rating']
            min_r = ACCEPTANCE_CRITERIA.get('min_rating', 0)
            max_r = ACCEPTANCE_CRITERIA.get('max_rating', 3000)
            if not (min_r <= rating <= max_r):
                return False, f"Рейтинг {rating} вне диапазона {min_r}-{max_r}"

        # Фильтр по временному контролю
        acceptable = any(
            parsed_tc[0] == acceptable[0] and parsed_tc[1] >= acceptable[1]

for acceptable in ACCEPTANCE_CRITERIA['time_controls']
        )
        if not acceptable:
            times = ", ".join(f"{t//60}+{i}" for t,i in ACCEPTANCE_CRITERIA['time_controls'])
            return False, f"Время {parsed_tc[0]//60}+{parsed_tc[1]} не в списке допустимых: {times}"

        # Остальные фильтры...

        return True, "Вызов принят"

    except Exception as e:
        logging.error(f"Ошибка проверки вызова: {str(e)}")
        return False, "Ошибка обработки вызова"

# Модифицированная функция для обработки вызовов
async def process_challenges():
    challenges = await get_challenges()
    for challenge in challenges:
        if not isinstance(challenge, dict):
            continue

        challenge_id = challenge.get('id')
        acceptable, reason = is_challenge_acceptable(challenge)

        if acceptable:
            await accept_challenge(challenge_id)
            logging.info(f"Принят вызов {challenge_id} от {challenge['challenger']['name']}")
        else:
            await decline_challenge(challenge_id)
            logging.info(f"Отклонён вызов {challenge_id}: {reason}")

# Функция для отклонения вызова
async def decline_challenge(challenge_id):
    async with aiohttp.ClientSession() as session:
        headers = {'Authorization': f'Bearer {API_TOKEN}'}
        async with session.post(f'{BASE_URL}/challenge/{challenge_id}/decline', headers=headers) as response:
            if response.status == 200:
                logging.info(f"Вызов {challenge_id} отклонён")
            else:
                logging.error(f"Ошибка при отклонении вызова {challenge_id}: {response.status}")

# Асинхронная функция для получения текущих вызовов
async def get_challenges():
    async with aiohttp.ClientSession() as session:
        headers = {'Authorization': f'Bearer {API_TOKEN}'}
        async with session.get(f'{BASE_URL}/challenge', headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                return data.get('in', []) if isinstance(data, dict) else []
            else:
                logging.error(f"Ошибка при запросе вызовов: {response.status}")
                return []

# Асинхронная функция для принятия вызова
async def accept_challenge(challenge_id):
    async with aiohttp.ClientSession() as session:
        headers = {'Authorization': f'Bearer {API_TOKEN}'}
        async with session.post(f'{BASE_URL}/challenge/{challenge_id}/accept', headers=headers) as response:
            if response.status == 200:
                logging.info(f"Вызов {challenge_id} принят.")
            else:
                logging.error(f"Ошибка при принятии вызова {challenge_id}: {response.status}")

# Асинхронная функция для получения текущей игры
async def get_current_game():
    async with aiohttp.ClientSession() as session:
        headers = {'Authorization': f'Bearer {API_TOKEN}'}
        async with session.get(f'{BASE_URL}/account/playing', headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                return data.get('nowPlaying', []) if isinstance(data, dict) else []
            else:
                logging.error(f"Ошибка при запросе текущих игр: {response.status}")
                return []

# Асинхронная функция для отправки хода
async def make_move(game_id, move):
    async with aiohttp.ClientSession() as session:
        headers = {'Authorization': f'Bearer {API_TOKEN}'}
        async with session.post(f'{BASE_URL}/bot/game/{game_id}/move/{move}', headers=headers) as response:
            return await response.json()

# Функция для получения лучшего хода
def get_best_move(fen, params):
    global engine
    try:
        # Перезапуск движка, если он не инициализирован
        if engine is None:
            init_engine()

        board = chess.Board(fen)
        engine.configure(params)
        result = engine.play(board, chess.engine.Limit(time=1.0))
        return result.move
    except Exception as e:
        logging.error(f"Ошибка в get_best_move: {e}")
        # Перезапускаем движок при ошибке
        close_engine()
        init_engine()
        return None

# Асинхронный основной цикл бота
async def bot_task():
    # Параметры движка по умолчанию
    params = {
        'Threads': 1,
        'Hash': 1024,
        'Skill Level': 10,
        'UCI_LimitStrength': True,
        'UCI_Elo': 2000,
    }

    while True:
        try:
            await process_challenges()

            # Проверка входящих вызовов
            challenges = await get_challenges()
            for challenge in challenges:
                if isinstance(challenge, dict) and 'id' in challenge:
                    await accept_challenge(challenge['id'])

            # Получение текущих игр
            games = await get_current_game()
            if not games:
                await asyncio.sleep(2)
                continue

            for game in games:
                if isinstance(game, dict) and 'fullId' in game and 'fen' in game:
                    game_id = game['fullId']
                    fen = game['fen']
                    best_move = get_best_move(fen, params)
                    if best_move:
                        await make_move(game_id, best_move.uci())

            await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"Ошибка в основном цикле бота: {e}")
            await asyncio.sleep(5)

@app.route('/')
def home():
    return "Бот работает в фоновом режиме!"

if __name__ == '__main__':
    # Инициализация движка при старте
    init_engine()

    # Запуск бота в отдельном потоке
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_thread = threading.Thread(target=loop.run_forever)
    bot_thread.daemon = True
    bot_thread.start()

    # Запуск асинхронного цикла бота
    asyncio.run_coroutine_threadsafe(bot_task(), loop)

    # Запуск Flask-сервера
    app.run(host='0.0.0.0', port=8080, debug=False)