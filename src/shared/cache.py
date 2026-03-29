import json
import logging
from typing import Any, Optional

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


# ---------------------------------------------------------------------------
# 고수준 캐시 헬퍼 (JSON 직렬화 포함)
# ---------------------------------------------------------------------------

def cache_get(key: str) -> Optional[Any]:
    """Redis에서 JSON 캐시 조회. 미스이면 None 반환."""
    try:
        r = get_redis()
        if r is None:
            return None
        raw = r.get(key)
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.warning("Redis GET error key=%s: %s", key, exc)
    return None


def cache_set(key: str, data: Any, ttl: int) -> None:
    """Redis에 JSON 캐시 저장. Redis 장애 시 조용히 무시."""
    try:
        r = get_redis()
        if r is None:
            return
        r.setex(key, ttl, json.dumps(data, default=str))
    except Exception as exc:
        logger.warning("Redis SET error key=%s: %s", key, exc)


def cache_delete_pattern(pattern: str) -> None:
    """패턴 매칭 키를 일괄 무효화. SCAN 커서 방식 사용 (KEYS는 blocking).

    업로드·이상치 갱신 후 관련 캐시를 무효화할 때 호출한다.
    """
    try:
        r = get_redis()
        if r is None:
            return
        cursor = 0
        keys_to_delete: list[str] = []
        while True:
            cursor, keys = r.scan(cursor, match=pattern, count=200)
            keys_to_delete.extend(keys)
            if cursor == 0:
                break
        if keys_to_delete:
            r.delete(*keys_to_delete)
    except Exception as exc:
        logger.warning("Redis DEL error pattern=%s: %s", pattern, exc)
