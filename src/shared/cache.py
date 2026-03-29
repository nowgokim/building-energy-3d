from typing import Optional

import redis as redis_lib

from .config import get_settings

_redis: Optional[redis_lib.Redis] = None


def get_redis() -> Optional[redis_lib.Redis]:
    global _redis
    if _redis is None:
        try:
            _redis = redis_lib.from_url(get_settings().REDIS_URL, decode_responses=True)
        except Exception:
            return None
    return _redis
