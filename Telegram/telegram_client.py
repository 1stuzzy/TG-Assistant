import os
import logging
import asyncio
import yaml
from pathlib import Path
from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.handlers import MessageHandler
from pyrogram.enums import ChatAction, ChatType
from pyrogram.errors import (
    AuthKeyUnregistered, BadRequest, Unauthorized,
    FloodWait, InternalServerError,
    PhoneNumberInvalid, PhoneCodeInvalid
)

from src.utils.inference import ResponseGenerator

class TelegramResponder:
    def __init__(
            self,
            config_manager=None,
            model_path=None,
            api_id=None,
            api_hash=None,
            phone=None,
            login=None,
            target_user_ids=None,
            target_channel_ids=None
    ):

        load_dotenv()
        try:
            if config_manager:
                telegram_config = config_manager.get_telegram_config()
                inference_config = config_manager.get_inference_config()
                logging_config = config_manager.get_section('logging', {})
                logging.basicConfig(level=getattr(logging, logging_config.get('level', 'INFO')),
                                    format=logging_config.get('format', '%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
            else:
                config_path = Path(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))) / 'config' / 'config.yaml'
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)
                telegram_config = config['telegram']
                inference_config = config['inference']
                logging_config = config['logging']
                logging.basicConfig(level=getattr(logging, logging_config['level']), format=logging_config['format'])

            self.logger = logging.getLogger(__name__)

            self.api_id = api_id or os.getenv("API_ID") or telegram_config.get('api_id')
            self.api_hash = api_hash or os.getenv("API_HASH") or telegram_config.get('api_hash')
            self.phone = phone or os.getenv("PHONE") or telegram_config.get('phone')
            self.login = login or os.getenv("LOGIN") or telegram_config.get('login')

            env_user_ids = os.getenv("TARGET_USER_IDS")
            env_channel_ids = os.getenv("TARGET_CHANNEL_IDS")
            self.target_user_ids = (
                target_user_ids if target_user_ids is not None else
                [int(x.strip()) for x in env_user_ids.split(",") if x.strip()] if env_user_ids else
                telegram_config.get('target_user_ids', [-1])
            )
            self.target_channel_ids = (
                target_channel_ids if target_channel_ids is not None else
                [int(x.strip()) for x in env_channel_ids.split(",") if x.strip()] if env_channel_ids else
                telegram_config.get('target_channel_ids', [-1])
            )

            self.mode = telegram_config.get('mode', 'only_private_chats')

            self.session_path = os.path.join(os.path.dirname(__file__), 'session', self.login)
            os.makedirs(os.path.dirname(self.session_path), exist_ok=True)

            # Настройка MTProto прокси
            proxy_config = None
            proxy_scheme = os.getenv("PROXY_SCHEME")
            proxy_host = os.getenv("PROXY_HOST")
            proxy_port = os.getenv("PROXY_PORT")
            proxy_secret = os.getenv("PROXY_SECRET")

            if proxy_scheme and proxy_host and proxy_port:
                proxy_config = {
                    "scheme": proxy_scheme,  # mtproto
                    "hostname": proxy_host,
                    "port": int(proxy_port),
                }
                # Добавляем секрет для MTProto
                if proxy_secret:
                    proxy_config["secret"] = bytes.fromhex(proxy_secret)

                self.logger.info(f"Используется MTProto прокси: {proxy_host}:{proxy_port}")
                self.logger.info(f"Секрет: {proxy_secret[:16]}...")

            # Создаем клиент с настройками для работы через MTProto прокси
            self.client = Client(
                self.session_path,
                api_id=self.api_id,
                api_hash=self.api_hash,
                phone_number=self.phone,
                app_version="AI Responder 1.0",
                device_model="PC",
                system_version="Python",
                sleep_threshold=30,
                proxy=proxy_config,
                takeout=False,
            )

            self.response_generator = ResponseGenerator(model_path, config_manager)
            self.model_info = self._get_model_info()

            self.is_running = False

            self.logger.info(f"TelegramResponder инициализирован с моделью: {self.model_info}")
            self.logger.info(f"Режим работы: {self.mode}")
            self.logger.info(f"Целевые пользователи: {self.target_user_ids}")
            self.logger.info(f"Целевые каналы/группы: {self.target_channel_ids}")

        except KeyError as e:
            self.logger.error(f"Ошибка конфигурации: отсутствует параметр {e}")
            self.config_error = f"Отсутствует необходимый параметр в config.yaml: {str(e)}"
            raise ValueError(f"Ошибка конфигурации: {e}")

        except Exception as e:
            self.logger.error(f"Ошибка инициализации TelegramResponder: {e}")
            self.config_error = f"Ошибка инициализации: {str(e)}"
            raise

    def _get_model_info(self):
        if hasattr(self.response_generator, 'metadata'):
            return f"{self.response_generator.metadata.get('target_user', 'Неизвестно')}"
        return "Последняя доступная модель"

    async def _send_response(self, client, message):
        try:
            self.logger.info(f"Получено сообщение от {message.from_user.first_name} ({message.from_user.id}): {message.text}")

            response = self.response_generator.generate_response(message.text)
            delay = min(len(response) * 0.1, 3)

            await client.send_chat_action(message.chat.id, ChatAction.TYPING)
            await asyncio.sleep(delay)
            await message.reply(response)

            self.logger.info(f"Отправлен ответ: {response[:50]}...")
        except FloodWait as e:
            self.logger.warning(f"FloodWait: Ожидаем {e.value} секунд перед повторной попыткой")
            await asyncio.sleep(e.value)
            await self._send_response(client, message)
        except Exception as e:
            self.logger.error(f"Ошибка при отправке ответа: {e}")

    async def message_handler(self, client, message):
        if message.outgoing:
            return

        if not message.text:
            return

        try:
            if self.mode == "only_private_chats":
                if message.chat.type == ChatType.PRIVATE:
                    if -1 in self.target_user_ids or message.from_user.id in self.target_user_ids:
                        await self._send_response(client, message)

            elif self.mode == "only_channel_messages":
                if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL]:
                    if -1 in self.target_channel_ids or message.chat.id in self.target_channel_ids:
                        await self._send_response(client, message)

            elif self.mode == "stalker":
                if message.from_user and message.from_user.id in self.target_user_ids:
                    await self._send_response(client, message)
        except Exception as e:
            self.logger.error(f"Ошибка при обработке сообщения: {e}")

    async def _safe_start_client(self):
        """Безопасный запуск клиента с обработкой ошибок синхронизации"""
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                await self.client.start()
                return True
            except Exception as e:
                error_str = str(e)
                if "BadMsgNotification" in error_str or "msg_id" in error_str or "time" in error_str:
                    self.logger.warning(f"Попытка {attempt + 1}/{max_attempts}: Ошибка синхронизации времени")

                    try:
                        await self.client.stop()
                    except:
                        pass

                    await asyncio.sleep(3)

                    # Удаляем старую сессию
                    session_file = self.session_path + ".session"
                    if os.path.exists(session_file):
                        try:
                            os.remove(session_file)
                            self.logger.info(f"Удален файл сессии: {session_file}")
                        except Exception as ex:
                            self.logger.error(f"Ошибка удаления сессии: {ex}")

                    # Создаем новый клиент
                    try:
                        proxy_config = None
                        proxy_scheme = os.getenv("PROXY_SCHEME")
                        proxy_host = os.getenv("PROXY_HOST")
                        proxy_port = os.getenv("PROXY_PORT")
                        proxy_secret = os.getenv("PROXY_SECRET")

                        if proxy_scheme and proxy_host and proxy_port:
                            proxy_config = {
                                "scheme": proxy_scheme,
                                "hostname": proxy_host,
                                "port": int(proxy_port),
                            }
                            if proxy_secret:
                                proxy_config["secret"] = bytes.fromhex(proxy_secret)

                        self.client = Client(
                            self.session_path,
                            api_id=self.api_id,
                            api_hash=self.api_hash,
                            phone_number=self.phone,
                            app_version="AI Responder 1.0",
                            device_model="PC",
                            system_version="Python",
                            sleep_threshold=30,
                            proxy=proxy_config,
                            takeout=False,
                        )
                        self.client.add_handler(MessageHandler(self.message_handler))
                        self.logger.info("Клиент пересоздан с MTProto прокси")
                    except Exception as ex:
                        self.logger.error(f"Ошибка пересоздания клиента: {ex}")
                        continue
                else:
                    raise
        return False

    async def start(self):
        if not self.response_generator.model:
            self.logger.error("Нет загруженной модели. Не могу запустить клиент.")
            print("❌ Нет загруженной модели. Сначала обучите модель или выберите существующую.")
            return

        self.is_running = True

        # Добавляем обработчик сообщений
        self.client.add_handler(MessageHandler(self.message_handler))

        print("\n=== TELEGRAM RESPONDER ===")
        print(f"Активная модель: {self.model_info}")
        print(f"Режим: {self.mode}")
        print("Запуск клиента...")

        try:
            self.logger.info("Подключение к Telegram API через MTProto прокси...")

            # Безопасный запуск с повторными попытками
            connected = await self._safe_start_client()

            if not connected:
                self.logger.error("Не удалось подключиться к Telegram API после нескольких попыток")
                print("❌ Не удалось подключиться к Telegram API")
                self.is_running = False
                return

            self.logger.info("Успешное подключение к Telegram API")

            if self.mode == "only_private_chats":
                target_info = "всех личных чатах" if -1 in self.target_user_ids else f"личных чатах с пользователями {self.target_user_ids}"
            elif self.mode == "only_channel_messages":
                target_info = "всех беседах/группах" if -1 in self.target_channel_ids else f"беседах/группах {self.target_channel_ids}"
            else:
                target_info = f"любых чатах для пользователей {self.target_user_ids}"

            print(f"✅ Бот успешно запущен и готов отвечать в {target_info}")
            print("Нажмите Ctrl+C для остановки.")

            me = await self.client.get_me()
            self.logger.info(f"Подключен как: {me.first_name} {me.last_name} (@{me.username})")

            while self.is_running:
                await asyncio.sleep(1)

        except asyncio.TimeoutError:
            self.logger.error("Тайм-аут подключения к Telegram API")
            print("❌ Не удалось подключиться к Telegram API: тайм-аут соединения")
            print("Проверьте ваше интернет-соединение и настройки прокси")
            self.is_running = False

        except (AuthKeyUnregistered, BadRequest, Unauthorized) as e:
            self.logger.error(f"Ошибка авторизации: {e}")
            print(f"❌ Ошибка авторизации Telegram: {e}")
            print("Проверьте правильность API_ID, API_HASH и PHONE в файле config.yaml")

            try:
                session_file = self.session_path + ".session"
                if os.path.exists(session_file):
                    os.remove(session_file)
                    self.logger.info(f"Удален файл сессии: {session_file}")
            except Exception as ex:
                self.logger.error(f"Ошибка при удалении файла сессии: {ex}")

            self.is_running = False

        except (PhoneNumberInvalid, PhoneCodeInvalid) as e:
            self.logger.error(f"Ошибка с номером телефона: {e}")
            print(f"❌ Проблема с номером телефона: {e}")
            print("Проверьте правильность номера телефона в файле config.yaml")
            self.is_running = False

        except KeyboardInterrupt:
            self.logger.info("Получен сигнал прерывания")
            print("⚠️ Остановка по запросу пользователя...")
            self.is_running = False
            await self.stop()

        except Exception as e:
            self.logger.exception(f"Непредвиденная ошибка при запуске клиента: {e}")
            print(f"❌ Произошла ошибка: {e}")
            self.is_running = False

        finally:
            if self.is_running:
                await self.stop()

    async def stop(self):
        if not self.is_running:
            return

        self.is_running = False
        try:
            self.logger.info("Остановка клиента...")
            await self.client.stop()
            self.logger.info("Клиент успешно остановлен")
            print("✅ Клиент успешно остановлен")
        except Exception as e:
            self.logger.error(f"Ошибка при остановке клиента: {e}")
            print(f"⚠️ Проблема при остановке клиента: {e}")