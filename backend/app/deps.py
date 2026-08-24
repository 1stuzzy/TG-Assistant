"""Общие экземпляры сервисов для API и жизненного цикла приложения."""
from app.config import settings
from app.services.account_store import AccountStore
from app.services.agent_manager import AgentManager
from app.services.character_store import CharacterStore
from app.services.llm_engine import LLMEngine
from app.services.model_catalog import ModelCatalog
from app.services.telegram_manager import TelegramAccountManager
from app.services.rental_store import RentalStore
from app.services.worker_store import WorkerStore

store = AccountStore(settings.accounts_file)
telegram = TelegramAccountManager(store)
catalog = ModelCatalog()
llm = LLMEngine()
characters = CharacterStore(settings.characters_file)
workers = WorkerStore(settings.workers_file)
rental = RentalStore(settings.rental_db)
rental.ensure_admin(settings.admin_login, settings.admin_password)
agents = AgentManager(store, llm, catalog, characters, workers, rental=rental)
