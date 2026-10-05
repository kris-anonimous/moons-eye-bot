"""
database/storage.py — хранилище состояний FSM в MongoDB.

MemoryStorage теряет все анкеты в процессе заполнения при каждом
перезапуске (а Hugging Face Spaces перезапускаются регулярно), поэтому
состояния хранятся в базе.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from config import db


class MongoStorage(BaseStorage):
    def __init__(self) -> None:
        self.col = db["fsm"]

    @staticmethod
    def _key(key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id or 0}:{key.destiny}"

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        await self.col.update_one({"_id": self._key(key)}, {"$set": {"state": value}}, upsert=True)

    async def get_state(self, key: StorageKey) -> Optional[str]:
        doc = await self.col.find_one({"_id": self._key(key)})
        return doc.get("state") if doc else None

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        await self.col.update_one({"_id": self._key(key)}, {"$set": {"data": dict(data)}}, upsert=True)

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        doc = await self.col.find_one({"_id": self._key(key)})
        return dict(doc.get("data") or {}) if doc else {}

    async def close(self) -> None:
        return None
