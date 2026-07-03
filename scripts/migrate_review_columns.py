"""
One-off migration: add review-flow columns to an existing `bounties` table.

Safe to run repeatedly — each ADD COLUMN is guarded by an existence check.
New databases created by init_db() already have these columns, so this is
only needed for DBs created before the human-in-the-loop rework.

Usage:
    python scripts/migrate_review_columns.py
"""

import sys

from sqlalchemy import create_engine, inspect, text

from app.config import settings

NEW_COLUMNS = {
    "roi_score": "FLOAT DEFAULT 0.0",
    "effort_hours": "FLOAT DEFAULT 0.0",
    "payout_confidence": "FLOAT DEFAULT 0.0",
    "briefing": "TEXT DEFAULT ''",
    "draft_solution": "TEXT DEFAULT ''",
    "approved_at": "DATETIME",
}


def main() -> int:
    engine = create_engine(settings.DATABASE_URL)
    inspector = inspect(engine)

    if "bounties" not in inspector.get_table_names():
        print("No 'bounties' table yet — nothing to migrate. init_db() will "
              "create it with the new columns.")
        return 0

    existing = {c["name"] for c in inspector.get_columns("bounties")}
    to_add = {k: v for k, v in NEW_COLUMNS.items() if k not in existing}

    if not to_add:
        print("All review-flow columns already present. Nothing to do.")
        return 0

    with engine.begin() as conn:
        for name, ddl in to_add.items():
            print(f"Adding column: {name}")
            conn.execute(text(f"ALTER TABLE bounties ADD COLUMN {name} {ddl}"))

    print(f"Migration complete. Added {len(to_add)} column(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
