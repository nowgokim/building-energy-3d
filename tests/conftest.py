# -*- coding: utf-8 -*-
"""
pytest 공통 픽스처 및 사전 모킹.

database.py는 import 시점에 create_engine()을 호출하므로
psycopg2가 로컬에 없는 CI/로컬 환경에서 임포트 오류가 발생한다.
아래 sys.modules 주입으로 차단한다.
"""
import sys
from unittest.mock import MagicMock

# 로컬 환경에 없는 패키지를 모킹하여 import 오류 방지
_MOCK_MODULES = [
    "psycopg2",
    "psycopg2.extensions",
    "psycopg2.extras",
    "psycopg2.errorcodes",
    "redis",
    "redis.client",
    "redis.exceptions",
    "celery",
    "celery.utils",
    "celery.utils.log",
]
for _mod in _MOCK_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()
