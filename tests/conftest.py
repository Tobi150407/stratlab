import os

import pytest

# Tests use SQLite by default; set SL_TEST_DATABASE_URL to run against PostgreSQL.
os.environ["SL_DATABASE_URL"] = os.environ.get("SL_TEST_DATABASE_URL", "sqlite:///./test.db")
os.environ["SL_LOG_JSON"] = "false"


@pytest.fixture(autouse=True)
def fresh_db():
    from app.config import get_settings
    from app.db import Base, get_engine, init_db, reset_engine

    get_settings.cache_clear()
    reset_engine()
    Base.metadata.drop_all(get_engine())
    init_db()
    yield
    reset_engine()
