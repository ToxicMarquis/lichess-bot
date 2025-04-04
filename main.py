from flask import Flask
import pandas as pd
from datetime import datetime
import async_timeout
import threading
import chess
import chess.engine
import os
import time
import logging
import atexit
import asyncio
import aiohttp
import random
import json

app = Flask(__name__)

API_TOKEN = os.environ['BOT_TOKEN']
BASE_URL = 'https://lichess.org/api'
STOCKFISH_PATH = './stockfish'
REQUEST_DELAY = (3, 7)
MAX_RETRIES = 3

# Критерии вызовов
ACCEPTANCE_CRITERIA = {
    'min_rating': 1500,
    'max_rating': 3000,
    'time_controls': [(60,0), (180, 0), (180, 2), (300, 0), (300, 5), (600, 0), (600, 10)],
    'variants': ['standard'],
    'rated': False,
    'allow_rematches': True,
    'deny_bots': True
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'),
              logging.StreamHandler()])

class BotManager:
    # Подключение
    def __init__(self):
        self._session = None
        self.engine = None
        self.transport = None
        self.active_games = {}
        self.games = {}
        self.lock = asyncio.Lock()
        self.params = {
            'Threads': 1,
            'Hash': 2048,
            'Skill Level': 1,
            'UCI_LimitStrength': True,
            'UCI_Elo': 1350
        }
    @property
    def session(self):
        """Ленивая инициализация сессии"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _create_save_directory(self):
        """Создает директорию для сохранения, если ее нет"""
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)
            logging.info(f"Создана директория: {os.path.abspath(self.save_dir)}")
            
    async def init(self):
        """Инициализация с проверкой доступности Stockfish"""
        try:
            if not os.path.exists(STOCKFISH_PATH):
                raise FileNotFoundError(
                    f"Stockfish не найден: {STOCKFISH_PATH}")

            self.transport, self.engine = await chess.engine.popen_uci(
                STOCKFISH_PATH, setpgrp=True)
            await self.engine.configure(self.params)
            logging.info("Stockfish инициализирован")
        except Exception as e:
            await self.close()
            raise

    async def close(self):
        """Корректное закрытие ресурсов"""
        tasks = []
        if self.engine:
            tasks.append(self.engine.quit())
        if self._session and not self._session.closed:
            tasks.append(self._session.close())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._session = None
        self.engine = None
        logging.info("Все ресурсы закрыты")

    def parse_time_control(self, tc):
        """Парсинг временного контроля"""
        if tc['type'] == 'unlimited':
            return (0, 0)
        if tc['type'] == 'correspondence':
            days = tc.get('daysPerTurn', 1)
            return (days * 86400, 0)
        return (tc.get('limit', 0), tc.get('increment', 0))

    def is_challenge_acceptable(self, challenge):
        """Проверка вызова с учетом реваншей"""
        try:
            challenger = challenge.get('challenger', {})
            is_rematch = challenge.get('rematch', False)
            logging.info(challenger)
            # Обработка реваншей
            if is_rematch:
                if not ACCEPTANCE_CRITERIA['allow_rematches']:
                    return False, "Реванши отключены"

                # Проверка на ботов
                if ACCEPTANCE_CRITERIA['deny_bots'] and challenger.get('title') == 'BOT':
                    return False, "Реванш от бота отклонен"

                return True, "Принят реванш"

            # Оригинальный код проверки для обычных вызовов
            tc = challenge.get('timeControl', {})

            # Проверка на ботов
            if ACCEPTANCE_CRITERIA['deny_bots'] and challenger.get('title') == 'BOT':
                return False, "Вызов от бота отклонен"

            # Проверка варианта игры
            variant = challenge.get('variant', {}).get('key')
            if variant not in ACCEPTANCE_CRITERIA['variants']:
                return False, f"Неподдерживаемый вариант {variant}"
            # Проверка на рейтинг
            rated = challenger.get('rated', False)
            if rated != ACCEPTANCE_CRITERIA['rated']:
                return False, f"Неподдерживаемый режим {rated}"
            
            # Проверка типа игры
            if tc.get('type') not in ['clock', 'correspondence', 'unlimited']:
                return False, "Неподдерживаемый тип игры"

            # Парсинг времени
            parsed_tc = self.parse_time_control(tc)

            # Проверка рейтинга
            if challenger.get('rating'):
                rating = challenger['rating']
                if not (ACCEPTANCE_CRITERIA['min_rating'] <= rating <=
                            ACCEPTANCE_CRITERIA['max_rating']):
                    return False, f"Рейтинг {rating} вне диапазона"

            # Проверка временного контроля
            acceptable = any(parsed_tc[0] == t and parsed_tc[1] >= i
                             for t, i in ACCEPTANCE_CRITERIA['time_controls'])
            if not acceptable:
                return False, "Недопустимый временной контроль"

            return True, "Вызов принят"

        except Exception as e:
            logging.error(f"Ошибка проверки: {str(e)}")
            return False, "Ошибка обработки"
    async def safe_request(self, method, url, **kwargs):
        """Исправленное формирование URL"""
        if not url.startswith(("http://", "https://")):
            url = f"{BASE_URL}{url}"

        logging.debug(f"Request: {method.__name__} {url}")

        try:
            async with method(url, **kwargs) as response:
                if response.status == 200:
                    return await response.json()
                logging.error(f"HTTP Error {response.status}")
        except Exception as e:
            logging.error(f"Request failed: {str(e)}")

        return None
    async def _reconnect_session(self):
        """Пересоздание сессии с проверкой"""
        try:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = aiohttp.ClientSession()
            logging.warning("Сессия пересоздана")
        except Exception as e:
            logging.error(f"Ошибка пересоздания сессии: {str(e)}")

    async def accept_challenge(self, challenge_id):
        await self.session.post(
            f"{BASE_URL}/challenge/{challenge_id}/accept",
            headers={"Authorization": f"Bearer {API_TOKEN}"})
    async def decline_challenge(self, challenge_id):
        """Отклонить вызов"""
        await self.safe_request(
            self.session.post,
            f"{BASE_URL}/challenge/{challenge_id}/decline",
            headers={"Authorization": f"Bearer {API_TOKEN}"})
    async def poll_events(self):
        """Основной цикл опроса событий"""
        while True:
            try:
                # Получение списка вызовов с обработкой ошибок
                challenges = await self.safe_request(
                    self.session.get,
                    f"{BASE_URL}/challenge",
                    headers={"Authorization": f"Bearer {API_TOKEN}"}) or {
                        'in': []
                    }  # Значение по умолчанию при ошибке

                # Обработка вызовов
                for challenge in challenges.get('in', []):
                    logging.info(f"Получен вызов: {challenge.get('id')} Тип: {'Реванш' if challenge.get('rematch') else 'Обычный'}")
                    acceptable, reason = self.is_challenge_acceptable(
                        challenge)
                    if acceptable:
                        await self.accept_challenge(challenge['id'])
                    else:
                        await self.decline_challenge(challenge['id'])

                # Получение активных игр
                games = await self.safe_request(
                    self.session.get,
                    f"{BASE_URL}/account/playing",
                    headers={"Authorization": f"Bearer {API_TOKEN}"}) or {
                        'nowPlaying': []
                    }

                # Управление задачами игр
                async with self.lock:
                    current_ids = {
                        g['gameId']
                        for g in games.get('nowPlaying', [])
                    }

                    # Удаление завершенных игр
                    for game_id in list(self.active_games.keys()):
                        if game_id not in current_ids:
                            del self.active_games[game_id]

                    # Добавление новых игр
                    for game in games.get('nowPlaying', []):
                        if game['gameId'] not in self.active_games:
                            self.active_games[
                                game['gameId']] = asyncio.create_task(
                                    self.process_game(game))

                await asyncio.sleep(random.uniform(*REQUEST_DELAY))

            except Exception as e:
                logging.error(f"Ошибка основного цикла: {str(e)}")
                await asyncio.sleep(5)

    async def restart_engine(self):
        try:
            if self.engine:
                await self.engine.quit()
                await asyncio.sleep(1)  # Пауза для завершения процессов

            self.transport, self.engine = await chess.engine.popen_uci(
                STOCKFISH_PATH)
            await self.engine.configure(self.params)
            logging.info("Движок успешно перезапущен")

        except Exception as e:
            logging.critical(f"Ошибка перезапуска движка: {str(e)}")
            raise
            
    # Процессы в партии
    async def get_game_stream(self, game_id):
        """Упрощенный поток событий игры"""
        url = f"{BASE_URL}/bot/game/stream/{game_id}"
        headers = {"Authorization": f"Bearer {API_TOKEN}"}

        async with self.session.get(url, headers=headers) as response:
            buffer = ""

            async for chunk in response.content.iter_chunked(1024):
                buffer += chunk.decode(errors="replace")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.strip():
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            pass  # Игнорируем битые JSON-сообщения
    async def process_game(self, game):
        game_id = game['gameId']
        logging.info(f"Обработка игры {game_id}")

        self.games[game_id] = {
            'board': chess.Board(),
            'is_white': game.get('color') == 'white'
        }
        logging.info(
            f"Установлен цвет: {'Белые' if self.games[game_id]['is_white'] else 'Чёрные'}"
        )

        try:
            async for event in self.get_game_stream(game_id):
                try:
                    logging.debug(
                        f"Игра {game_id}: получено событие {event.get('type')}"
                    )

                    if event['type'] == 'gameFull':
                        initial_state = event['state']
                        logging.debug(
                            f"Начальное состояние игры {game_id}: {initial_state}"
                        )
                        await self.handle_initial_state(initial_state, game_id)

                    elif event['type'] == 'gameState':
                        await self.handle_game_state(event, game_id)

                    elif event['type'] == 'chatLine':
                        await self.handle_chat_message(event, game_id)
                    elif event['type'] in ['gameFinish', 'aborted', 'resign']:
                        logging.info(
                            f"Игра {game_id} завершена: {event['type']}")
                        break
                    else:
                        logging.debug(
                            f"Необработанный тип события: {event.get('type')}")

                except Exception as e:
                    logging.error(f"Ошибка обработки события: {str(e)}",
                                  exc_info=True)

        except Exception as e:
            logging.error(f"Ошибка игры {game_id}: {str(e)}", exc_info=True)
        finally:
            async with self.lock:
                if game_id in self.active_games:
                    del self.active_games[game_id]
                    logging.info(f"Игра {game_id} завершена")

    async def handle_initial_state(self, state, game_id):
        """Обработка начального состояния игры с улучшенным логированием"""
        try:
            # Определение цвета из данных события
            game_data = self.games[game_id]
            board = game_data['board']

            # Применение истории ходов
            if 'moves' in state and state['moves']:
                logging.debug(f"Применяем историю ходов: {state['moves']}")
                try:
                    for move in state['moves'].split():
                        board.push_uci(move)
                except ValueError as e:
                    logging.error(
                        f"Некорректный ход в истории: {move} ({str(e)})")
                    return

            # Логирование позиции
            logging.info(
                f"Очередь хода: {'Белые' if board.turn else 'Чёрные'}")

            # Проверка и запуск обработки хода
            if (board.turn == chess.WHITE and game_data) or \
               (board.turn == chess.BLACK and not game_data):
                logging.info("Инициируем расчет хода")
                await self.handle_game_state(
                    {
                        'moves': state.get('moves', ''),
                        'isMyTurn': True
                    }, game_id)
            else:
                logging.warning("Пропуск: не очередь хода")

        except Exception as e:
            logging.error(f"Критическая ошибка: {str(e)}", exc_info=True)
            await self.restart_engine()
    async def handle_chat_message(self, event, game_id):
        if event['username'] == 'lichess':
            return  # Игнорируем системные сообщения

        message = f"{event['username']}: {event['text']}"
        logging.info(f"[Чат {game_id}] {message}")

        # Пример ответа на команду
        if event['text'].lower() == '!help':
            await self.send_chat_message(game_id, "Доступные команды: !help")
    async def send_chat_message(self, game_id, message):
        url = f"{BASE_URL}/bot/game/{game_id}/chat"
        data = {'room': 'player', 'text': message}
        await self.safe_request(self.session.post, url, json=data)
    async def handle_game_end(self, event, game_id):
        logging.info(f"Игра {game_id} завершена. Причина: {event.get('status')}")

        # Опционально: предложение реванша при поражении
        if event.get('winner') != 'white' and self.games[game_id]['is_white']:
            await self.send_chat_message(game_id, "Хорошая игра! Хотите реванш?")
    async def handle_game_state(self, state, game_id):
        """Обработка состояния игры с проверкой целостности"""
        try:
            game_data = self.games.get(game_id)
            if not game_data:
                logging.error(f"Данные игры {game_id} не найдены")
                return

            board = game_data['board']
            is_white = game_data['is_white']
            current_moves = [m.uci() for m in board.move_stack]
            new_moves = state.get('moves', '').split()

            # Проверка расхождений в истории ходов
            if len(new_moves) < len(current_moves) or new_moves[:len(current_moves)] != current_moves:
                logging.warning("Обнаружено расхождение в истории ходов! Перезагружаем доску...")
                await self.reload_game_state(game_id)
                return

            # Применяем только новые ходы
            for move in new_moves[len(current_moves):]:
                try:
                    board.push_uci(move)
                    logging.debug(f"Применен ход {move}. Новая позиция:\n{board.unicode(borders=True)}")
                except chess.IllegalMoveError as e:
                    logging.error(f"Недопустимый ход {move}: {str(e)}")
                    await self.reload_game_state(game_id)
                    return

            # Дальнейшая обработка хода...

            # Расчет хода с обработкой таймаута
            logging.info("Запуск расчета хода...")
            try:
                async with async_timeout.timeout(5):  # Исправленная строка
                    result = await self.engine.play(
                        board,
                        chess.engine.Limit(time=0.05),
                        info=chess.engine.INFO_BASIC)

                    if result.move and result.move in board.legal_moves:  # Добавлена проверка
                        logging.info(
                            f"Stockfish: {result.move.uci()}"
                        )
                        if await self.make_move(game_id, result.move.uci()):
                            logging.info("Ход успешно выполнен")
                        else:
                            logging.error("Ошибка отправки хода")
                    else:
                        logging.warning("Движок вернул недопустимый ход")

            except asyncio.TimeoutError:
                logging.error("Превышено время расчета хода!")
            except chess.engine.EngineTerminatedError:
                logging.critical("Движок перестал отвечать!")
                await self.restart_engine()

        except Exception as e:
            logging.error(f"Фатальная ошибка: {str(e)}", exc_info=True)
    async def reload_game_state(self, game_id):
        """Перезагружает состояние игры с сервера"""
        try:
            async with self.session.get(
                f"{BASE_URL}/bot/game/{game_id}",
                headers={"Authorization": f"Bearer {API_TOKEN}"}
            ) as response:
                game_data = await response.json()
                new_fen = game_data.get('fen', chess.STARTING_FEN)
                self.games[game_id]['board'] = chess.Board(new_fen)
                logging.info(f"Состояние игры {game_id} перезагружено. FEN: {new_fen}")
        except Exception as e:
            logging.error(f"Ошибка перезагрузки игры {game_id}: {str(e)}")
    async def make_move(self, game_id, move):
        """Улучшенная версия отправки хода с обновлением доски"""
        for attempt in range(MAX_RETRIES):
            try:
                async with self.session.post(
                    f"{BASE_URL}/bot/game/{game_id}/move/{move}",
                    headers={"Authorization": f"Bearer {API_TOKEN}"},
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    logging.debug(f"Статус ответа: {response.status}")

                    if response.status == 200:
                        logging.info(f"Ход {move} успешно отправлен!")

                        # Обновляем локальную доску
                        if game_id in self.games:
                            try:
                                self.games[game_id]['board'].push_uci(move)
                            except chess.IllegalMoveError as e:
                                logging.error(f"Ошибка обновления доски: {str(e)}")
                                await self.reload_game_state(game_id)

                        return True
                    else:
                        return False

            except Exception as e:
                logging.error(f"Ошибка отправки (попытка {attempt+1}): {str(e)}")
                await asyncio.sleep(1)

        logging.error("Все попытки отправки хода провалились")
        return False
    async def get_best_move(self, fen: str) -> chess.Move | None:
        """Асинхронный расчет хода"""
        try:
            board = chess.Board(fen)
            result = await self.engine.play(board,
                                            chess.engine.Limit(time=0.05))
            return result.move
        except Exception as e:
            logging.error(f"Ошибка расчета хода: {e}")
            return None

@app.route('/')
def home():
    return "Бот работает!"

async def main():
    bot_manager = BotManager()

    await bot_manager.init()

    await asyncio.gather(bot_manager.poll_events())

if __name__ == '__main__':
    def run_bot():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(main())
        except KeyboardInterrupt:
            logging.info("Бот остановлен пользователем")
        except Exception as e:
            logging.critical(f"Критическая ошибка: {str(e)}")
        finally:
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            loop.close()

    flask_thread = threading.Thread(
        target=app.run,
        kwargs={'host': '0.0.0.0', 'port': 8080},
        daemon=True
    )
    flask_thread.start()

    run_bot()