"""Back up the prototype's data: a Markdown export of the memo and every voice memo
transcript, plus a full SQLite copy of `db/prototype.db`.

Read-only against both databases. Writes only under the output directory (default
`db/local/`, gitignored):
- `exports/memo.md` and `exports/transcripts/*.md`. An existing `exports/` is first moved
  to `exports-<date of its memo.md>/`, so earlier exports are never overwritten.
- `prototype-<YYYY-MM-DDTHHMMSS>.db` (never overwriting an earlier one), made with SQLite's online backup, so it's safe while
  the app is running. Restore it by stopping the backend and copying it over
  `db/prototype.db`.

Run from backend/: `python scripts/export_md.py` (add --no-db-backup to skip the database
copy, or --out-dir DIR to write somewhere else).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_config  # noqa: E402
from app.auth import CurrentUser  # noqa: E402
from app.db import CorticoSession, PrototypeSession  # noqa: E402
from app.models_cortico import Conversation  # noqa: E402
from app.models_prototype import VoiceMemo  # noqa: E402
from app.routers.memo import get_memo  # noqa: E402


def mmss(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def quote(text: str | None) -> str:
    return "\n".join("> " + line for line in (text or "").strip().splitlines()) or "> (empty)"


def archive_previous_export(exports: Path) -> Path | None:
    """Moves an existing export aside, named by the date of its memo.md."""
    if not exports.exists():
        return None
    stamp_source = exports / "memo.md" if (exports / "memo.md").exists() else exports
    stamp = datetime.fromtimestamp(stamp_source.stat().st_mtime)
    target = exports.with_name(f"exports-{stamp:%Y-%m-%d}")
    if target.exists():
        target = exports.with_name(f"exports-{stamp:%Y-%m-%dT%H%M%S}")
    exports.rename(target)
    return target


def write_transcripts(exports: Path, cortico_db: Session, prototype_db: Session) -> int:
    (exports / "transcripts").mkdir(parents=True, exist_ok=True)
    count = 0
    for vm in prototype_db.query(VoiceMemo).order_by(VoiceMemo.recorded_at):
        conv = cortico_db.get(Conversation, vm.conversation_id)
        title = conv.title if conv else f"Conversation {vm.conversation_id}"
        recorded = vm.recorded_at.replace(tzinfo=timezone.utc)
        body = [
            f"# Voice memo {vm.id}: {title}",
            "",
            f"- **Conversation:** {title} (id {vm.conversation_id})",
            f"- **Recorded:** {recorded:%Y-%m-%d %H:%M} UTC",
            f"- **Length:** {mmss(vm.duration)}",
            f"- **Status:** {vm.status}",
            f"- **Transcription engine:** {vm.transcription_engine or 'unknown'}",
            f"- **Audio:** `{vm.audio_path}`",
            "",
            "## Transcript",
            "",
            (vm.transcription_edited or "(no transcript)").strip(),
            "",
        ]
        (exports / "transcripts" / f"{recorded:%Y-%m-%d}_voice-memo-{vm.id}.md").write_text("\n".join(body))
        count += 1
    return count


def _render_entry(e: dict, lines: list[str]) -> None:
    lines += [f"### {e['insight_text'].strip()}", ""]
    meta = [f"Conversation: {e['conversation_title'] or e['conversation_id']}", f"voice memo {e['voice_memo_id']}"]
    if e.get("reflection_timestamp") is not None:
        meta.append(f"at {mmss(e['reflection_timestamp'])}")
    src = "AI, edited" if e["edited_from_ai"] else ("AI" if e["source"] == "ai" else "you")
    meta += [f"source: {src}", f"filed {e['filed_at'][:10]}"]
    lines += ["*" + " · ".join(meta) + "*", ""]
    for ev in e["evidence"]:
        lines += [quote(ev["text"]) + (" ★" if ev["starred"] else ""), ""]


def write_memo(exports: Path, cortico_db: Session, prototype_db: Session) -> int:
    exports.mkdir(parents=True, exist_ok=True)
    memo = get_memo(cortico_db=cortico_db, prototype_db=prototype_db, user=CurrentUser(id=get_config().default_user_id))
    lines = ["# Memo", "", "Exported from Notes in the Margin.", ""]
    total = 0
    for sec in memo["sections"]:
        if not sec["entries"] and sec["archived"]:
            continue
        lines += [f"## {sec['name']}" + (" (archived)" if sec["archived"] else ""), ""]
        if not sec["entries"]:
            lines += ["_No entries yet._", ""]
        for e in sec["entries"]:
            _render_entry(e, lines)
            total += 1
    if memo["unmapped"]:
        lines += ["## Not mapped to a research question", ""]
        for e in memo["unmapped"]:
            _render_entry(e, lines)
            total += 1
    if memo["starred_unassigned"]:
        lines += ["## Starred highlights not yet in the memo", ""]
        for g in memo["starred_unassigned"]:
            lines += [f"### {g['conversation_title'] or g['conversation_id']}", ""]
            for h in g["highlights"]:
                for s in h["snippets"]:
                    lines += [quote(s["text"]) + " ★", ""]
                if h["notes"]:
                    lines += [f"Note: {h['notes'].strip()}", ""]
    (exports / "memo.md").write_text("\n".join(lines))
    return total


def backup_database(source: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y-%m-%dT%H%M%S}"
    target = out_dir / f"prototype-{stamp}.db"
    n = 2
    while target.exists():
        target = out_dir / f"prototype-{stamp}-{n}.db"
        n += 1
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def main() -> None:
    config = get_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=config.project_root / "db" / "local")
    parser.add_argument("--no-db-backup", action="store_true", help="skip the SQLite copy")
    args = parser.parse_args()

    exports = args.out_dir / "exports"
    archived = archive_previous_export(exports)
    if archived:
        print(f"moved previous export to {archived}")
    with CorticoSession() as cortico_db, PrototypeSession() as prototype_db:
        n_transcripts = write_transcripts(exports, cortico_db, prototype_db)
        n_entries = write_memo(exports, cortico_db, prototype_db)
    print(f"wrote {n_transcripts} transcripts and memo.md ({n_entries} entries) to {exports}")
    if not args.no_db_backup:
        print(f"backed up database to {backup_database(config.prototype_db_abspath, args.out_dir)}")


if __name__ == "__main__":
    main()
