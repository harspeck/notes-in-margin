import importlib.util
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Prototype-owned tables go to a scratch file for the test run, so pytest never leaves
# test rows (RQs, highlights, voice memos...) behind in db/prototype.db, the real dev
# database.
# A DATABASE_URL in the shell (say, after `vercel env pull`) would send test rows to the real
# Postgres sidecar, and DATA_SOURCE would switch modes. Tests always use scratch SQLite.
for _var in ("DATABASE_URL", "POSTGRES_URL", "DATA_SOURCE", "SESSION_SECRET"):
    os.environ.pop(_var, None)
os.environ.setdefault("PROTOTYPE_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_prototype.db"))

# The Cortico snapshot is the real, read-only one when it is at the repo root. Without it,
# a synthetic stand-in (tests/fixtures/build_snapshot_fixture.py) is built into a scratch
# file. CORTICO_DB_PATH must be absolute and set before `app` is imported, because the
# engine is created at import time.
_REAL_SNAPSHOT = Path(__file__).resolve().parents[2] / "acp_pipeline_snapshot.db"
if "CORTICO_DB_PATH" not in os.environ and not _REAL_SNAPSHOT.exists():
    _spec = importlib.util.spec_from_file_location(
        "build_snapshot_fixture", Path(__file__).resolve().parent / "fixtures" / "build_snapshot_fixture.py"
    )
    _builder = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_builder)
    _fixture = _builder.build(Path(tempfile.mkdtemp()) / "snapshot_fixture.db")
    os.environ.setdefault("CORTICO_DB_PATH", str(_fixture))

import pytest  # noqa: E402

from app import models_prototype  # noqa: E402, F401 — registers tables on PrototypeBase
from app.db import PrototypeBase, prototype_engine  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _create_prototype_tables():
    PrototypeBase.metadata.create_all(bind=prototype_engine)
