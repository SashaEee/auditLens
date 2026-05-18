from __future__ import annotations
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from .config import Settings

_engine = None
_Session: sessionmaker | None = None

def init(settings: Settings | None = None):
    global _engine, _Session
    settings = settings or Settings.load()
    _engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)

@contextmanager
def session() -> Session:
    if _Session is None:
        init()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
