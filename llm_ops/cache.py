"""In-memory LLM response cache (Cat 8.3).

Keyed by SHA256 of (model, system_prompt, user_prompt). Default TTL 300s.
Stats kept so the cost dashboard can show hit rate.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Any

_LOCK = threading.Lock()
_CACHE: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
_MAX_ENTRIES = 512
_TTL_DEFAULT = 300.0

_STATS = {"hits": 0, "misses": 0, "stored": 0}


def _key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def get_cached(model: str, system_prompt: str, user_prompt: str,
               ttl: float = _TTL_DEFAULT) -> Any | None:
    k = _key(model, system_prompt, user_prompt)
    now = time.monotonic()
    with _LOCK:
        item = _CACHE.get(k)
        if item is None:
            _STATS["misses"] += 1
            return None
        ts, value = item
        if (now - ts) > ttl:
            _CACHE.pop(k, None)
            _STATS["misses"] += 1
            return None
        # LRU bump
        _CACHE.move_to_end(k)
        _STATS["hits"] += 1
        return value


def put_cached(model: str, system_prompt: str, user_prompt: str,
               value: Any) -> None:
    k = _key(model, system_prompt, user_prompt)
    now = time.monotonic()
    with _LOCK:
        _CACHE[k] = (now, value)
        _STATS["stored"] += 1
        while len(_CACHE) > _MAX_ENTRIES:
            _CACHE.popitem(last=False)


def cache_stats() -> dict[str, Any]:
    with _LOCK:
        total = _STATS["hits"] + _STATS["misses"]
        return {
            "size": len(_CACHE),
            "max_size": _MAX_ENTRIES,
            "hits": _STATS["hits"],
            "misses": _STATS["misses"],
            "stored": _STATS["stored"],
            "hit_rate": (_STATS["hits"] / total) if total else 0.0,
        }
