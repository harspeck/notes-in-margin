"""Creates any missing sidecar tables in the configured database (DATABASE_URL, else the
local SQLite file). Safe to re-run: create_all only adds tables, never alters them.

    .venv/bin/python scripts/init_db.py                 # uses the environment
    .venv/bin/python scripts/init_db.py --env-file .env.local   # e.g. after `vercel env pull`
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

parser = argparse.ArgumentParser()
parser.add_argument("--env-file", help="load variables (DATABASE_URL, ...) from this file first")
args = parser.parse_args()
if args.env_file:
    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=True)

from sqlalchemy import inspect  # noqa: E402

from app import models_prototype  # noqa: E402, F401
from app.db import PrototypeBase, prototype_engine  # noqa: E402

before = set(inspect(prototype_engine).get_table_names())
PrototypeBase.metadata.create_all(bind=prototype_engine)
after = set(inspect(prototype_engine).get_table_names())
print(f"dialect={prototype_engine.dialect.name} tables={len(after)} created={sorted(after - before) or 'none'}")
