"""
Ізоляція даних користувачів у спільних колекціях (багатокористувацький режим).

ScopedDatabase / ScopedCollection — тонкі обгортки над pymongo, які
автоматично додають `user_id` у кожен фільтр, вставку, upsert і aggregate.
Репозиторії (PositionsRepository, TradeAnalyticsRepository, settings) пишуть
звичайні запити через `self.db.<collection>` і не знають про користувачів —
обгортка гарантує, що один користувач не побачить і не змінить чужі рядки.

Підтримуються лише методи, які реально використовують репозиторії; виклик
будь-якого іншого падає з AttributeError, а не мовчки обходить ізоляцію.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional


def _scoped_filter(user_id: str, flt: Optional[dict]) -> dict:
    scoped = dict(flt or {})
    scoped["user_id"] = user_id
    return scoped


class ScopedCollection:
    """Колекція, у якій кожна операція обмежена одним user_id."""

    def __init__(self, collection, user_id: str):
        self._c = collection
        self.user_id = user_id

    @property
    def name(self) -> str:
        return self._c.name

    # ---------- читання ----------

    def find(self, flt: Optional[dict] = None, *args, **kwargs):
        return self._c.find(_scoped_filter(self.user_id, flt), *args, **kwargs)

    def find_one(self, flt: Optional[dict] = None, *args, **kwargs):
        return self._c.find_one(_scoped_filter(self.user_id, flt), *args, **kwargs)

    def count_documents(self, flt: Optional[dict] = None, **kwargs) -> int:
        return self._c.count_documents(_scoped_filter(self.user_id, flt), **kwargs)

    def aggregate(self, pipeline: List[dict], **kwargs):
        return self._c.aggregate([{"$match": {"user_id": self.user_id}}] + list(pipeline), **kwargs)

    # ---------- запис ----------

    def insert_one(self, doc: dict, **kwargs):
        doc = dict(doc)
        doc["user_id"] = self.user_id
        return self._c.insert_one(doc, **kwargs)

    def insert_many(self, docs: Iterable[dict], **kwargs):
        return self._c.insert_many([{**d, "user_id": self.user_id} for d in docs], **kwargs)

    def update_one(self, flt: dict, update: dict, upsert: bool = False, **kwargs):
        return self._c.update_one(
            _scoped_filter(self.user_id, flt), self._guard_update(update, upsert), upsert=upsert, **kwargs
        )

    def update_many(self, flt: dict, update: dict, upsert: bool = False, **kwargs):
        return self._c.update_many(
            _scoped_filter(self.user_id, flt), self._guard_update(update, upsert), upsert=upsert, **kwargs
        )

    def delete_one(self, flt: dict, **kwargs):
        return self._c.delete_one(_scoped_filter(self.user_id, flt), **kwargs)

    def delete_many(self, flt: dict, **kwargs):
        return self._c.delete_many(_scoped_filter(self.user_id, flt), **kwargs)

    def _guard_update(self, update: dict, upsert: bool) -> dict:
        # user_id змінювати не можна: інакше рядок «переїхав» би до іншого користувача
        for op in ("$set", "$unset", "$setOnInsert", "$rename"):
            if "user_id" in (update.get(op) or {}):
                raise ValueError("ScopedCollection: user_id cannot be modified")
        if upsert:
            # фільтр уже містить user_id, Mongo підставить його в новий документ
            return update
        return update

    # ---------- індекси ----------

    def create_index(self, keys: Any, **kwargs):
        """Індекс завжди з префіксом user_id (і унікальність — у межах користувача)."""
        if isinstance(keys, str):
            keys = [(keys, 1)]
        keys = [("user_id", 1)] + [k for k in keys if k[0] != "user_id"]
        return self._c.create_index(keys, **kwargs)


class ScopedDatabase:
    """`db.<collection>` повертає ScopedCollection для одного user_id."""

    def __init__(self, database, user_id: str):
        self._d = database
        self.user_id = user_id

    @property
    def name(self) -> str:
        return self._d.name

    def __getattr__(self, name: str) -> ScopedCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        return ScopedCollection(self._d[name], self.user_id)

    def __getitem__(self, name: str) -> ScopedCollection:
        return ScopedCollection(self._d[name], self.user_id)
