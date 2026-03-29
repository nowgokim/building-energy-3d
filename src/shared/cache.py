import logging
from typing import Optional

import redis as redis_lib

from .config import get_settings

logger = logging.getLogger(__name__)
_redis: Optional[redis_lib.Redis] = None


def get_redis() -> Optional[redis_lib.Redis]:
    global _redis
    if _redis is None:
        try:
            _redis = redis_lib.from_url(get_settings().REDIS_URL, decode_responses=True)
            _redis.ping()  # 연결 즉시 확인
        except Exception as e:
            logger.error("Redis connection failed: %s — 캐시 비활성화됨", e)
            _redis = None
            return None
    return _redis
