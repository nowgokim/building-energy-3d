from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from .config import get_settings

engine = create_engine(get_settings().DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


@contextmanager
def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db_dependency():
    """FastAPI Depends용"""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_pg_conn(db_url: str):
    import psycopg2
    from urllib.parse import urlparse
    p = urlparse(db_url)
    return psycopg2.connect(
        host=p.hostname,
        port=p.port or 5432,
        dbname=p.path.lstrip("/"),
        user=p.username,
        password=p.password,
    )


def execute_sql(sql: str, params: dict = None) -> None:
    """DML(INSERT/UPDATE/DELETE) 실행. 반환값 없음."""
    with engine.connect() as conn:
        conn.execute(text(sql), params or {})
        conn.commit()


def execute_sql_scalar(sql: str, params: dict = None):
    """SELECT 결과를 커넥션 내에서 즉시 소비 후 첫 Row 반환."""
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        row = result.fetchone()
        conn.commit()
        return row


def execute_ddl(sql: str) -> None:
    """DDL 또는 REFRESH MATERIALIZED VIEW — AUTOCOMMIT 모드로 실행."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(sql))
