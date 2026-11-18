from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

from .config import get_config

config = get_config()

# Read-only connection to the Cortico snapshot. Opened with mode=ro&immutable=1 so the
# OS/SQLite layer itself refuses writes — this is not just a convention, there is no write
# path through this engine at all. See docs/prototype-log.md. In cortico_api mode the file
# need not exist: SQLAlchemy only opens it on first use, and nothing uses it in that mode.
cortico_engine = create_engine(
    f"sqlite:///file:{config.cortico_db_abspath}?mode=ro&immutable=1&uri=true",
    connect_args={"check_same_thread": False},
)


@event.listens_for(cortico_engine, "connect")
def _enforce_query_only(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA query_only = ON;")


CorticoSession = sessionmaker(bind=cortico_engine, autoflush=False, autocommit=False)
CorticoBase = declarative_base()


def _sidecar_url(url: str) -> str:
    # Neon and Vercel hand out postgres:// URLs; SQLAlchemy needs the psycopg 3 dialect named.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


# The sidecar: everything this app owns (research questions, highlights, memos, sessions,
# workspaces, Cortico caches). Postgres when DATABASE_URL is set, else the local SQLite file.
if config.database_url:
    # NullPool: serverless instances are short-lived and Neon's pooled endpoint does the pooling.
    prototype_engine = create_engine(_sidecar_url(config.database_url), poolclass=NullPool)
else:
    prototype_engine = create_engine(
        f"sqlite:///{config.prototype_db_abspath}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(prototype_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys = ON;")


PrototypeSession = sessionmaker(bind=prototype_engine, autoflush=False, autocommit=False)
PrototypeBase = declarative_base()


def get_cortico_db():
    db: Session = CorticoSession()
    try:
        yield db
    finally:
        db.close()


def get_prototype_db():
    db: Session = PrototypeSession()
    try:
        yield db
    finally:
        db.close()
