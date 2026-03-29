"""공통 유틸리티 함수."""
from typing import Any, Optional


def optional_float(value: Any) -> Optional[float]:
    """None 허용 float 변환. DB NULL → None, 유효값 → float."""
    return float(value) if value is not None else None
