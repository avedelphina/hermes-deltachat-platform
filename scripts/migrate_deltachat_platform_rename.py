#!/usr/bin/env python3
"""Migrate persisted Hermes routing/session state between the plugin's
deltachat-platform and deltachat platform ids.

v1.6.0 renamed the plugin's registered Platform id from
``deltachat-platform`` to ``deltachat``; v1.7.0 reverted that rename
(``-platform`` turned out to be Hermes's own naming convention for
messaging platform plugins, not something to drop). Either transition
orphans two things Hermes persists under the *previous* id:

  1. ``<profile>/state.db``'s ``gateway_routing`` table (the primary routing
     store) -- ``session_key`` plus the ``platform``/``origin.platform``
     fields embedded in its ``entry_json`` blob.
  2. ``<profile>/sessions/sessions.json`` (a legacy mirror of the same data,
     used by ``hermes plugins list`` / `/sessions` style tooling).

Without this migration, every existing DM/group chat's routing entry becomes
unparseable ("'<old-id>' is not a valid Platform") and the conversation
history for that chat effectively starts over.

This script only rewrites the platform id in those two stores. It never
touches ``session_id`` (the pointer to the actual conversation) or any
message content, and it never touches config.yaml -- see docs/UPGRADING.md
for the (manual, two-line) config.yaml edit this migration does NOT do for
you.

Usage:
    # If you're on a plugin version from the v1.6.0-v1.6.4 window and are
    # updating to v1.7.0+ (the default direction below): restores
    # deltachat-platform, undoing the temporary rename.
    #
    # Dry run (default) -- reports what would change, writes nothing.
    python3 scripts/migrate_deltachat_platform_rename.py ~/.hermes/profiles/myprofile

    # Apply for real. Backs up state.db and sessions.json first
    # (*.bak-rename-<timestamp>), then rewrites in place.
    python3 scripts/migrate_deltachat_platform_rename.py --apply ~/.hermes/profiles/myprofile

    # Default (no-profile) Hermes home also has its own state, e.g.:
    python3 scripts/migrate_deltachat_platform_rename.py --apply ~/.hermes

    # Going the other way (deltachat-platform -> deltachat, e.g. testing a
    # v1.6.x checkout): swap the direction.
    python3 scripts/migrate_deltachat_platform_rename.py --apply --reverse \
        ~/.hermes/profiles/myprofile

Stop the Hermes gateway for the affected profile(s) before running with
--apply -- this writes directly to state.db, which the gateway also holds
open while running.
"""

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Default direction restores deltachat-platform (the v1.7.0+ name).
# --reverse swaps these (see main()).
DEFAULT_OLD = "deltachat"
DEFAULT_NEW = "deltachat-platform"


def _backup(path: Path, timestamp: str) -> Path:
    backup_path = path.with_name(f"{path.name}.bak-rename-{timestamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def migrate_state_db(
    db_path: Path, *, old: str, new: str, apply: bool, timestamp: str
) -> int:
    old_segment = f":{old}:"
    new_segment = f":{new}:"
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT rowid, session_key, entry_json FROM gateway_routing "
            "WHERE session_key LIKE ?",
            (f"%{old_segment}%",),
        )
        rows = cur.fetchall()
        if not rows:
            return 0
        if not apply:
            return len(rows)

        _backup(db_path, timestamp)
        for rowid, session_key, entry_json in rows:
            new_key = session_key.replace(old_segment, new_segment)
            entry = json.loads(entry_json)
            if entry.get("session_key") == session_key:
                entry["session_key"] = new_key
            if entry.get("platform") == old:
                entry["platform"] = new
            origin = entry.get("origin") or {}
            if origin.get("platform") == old:
                origin["platform"] = new
            cur.execute(
                "UPDATE gateway_routing SET session_key = ?, entry_json = ? "
                "WHERE rowid = ?",
                (new_key, json.dumps(entry), rowid),
            )
        con.commit()
        return len(rows)
    finally:
        con.close()


def migrate_sessions_json(
    json_path: Path, *, old: str, new: str, apply: bool, timestamp: str
) -> int:
    old_segment = f":{old}:"
    new_segment = f":{new}:"
    data = json.loads(json_path.read_text())
    changed = 0
    new_data = {}
    for key, value in data.items():
        if key == "_README" or not isinstance(value, dict):
            new_data[key] = value
            continue
        new_key = key.replace(old_segment, new_segment) if old_segment in key else key
        if value.get("session_key") == key:
            value["session_key"] = new_key
        if value.get("platform") == old:
            value["platform"] = new
        origin = value.get("origin")
        if isinstance(origin, dict) and origin.get("platform") == old:
            origin["platform"] = new
        if new_key != key:
            changed += 1
        new_data[new_key] = value

    if changed and apply:
        _backup(json_path, timestamp)
        json_path.write_text(json.dumps(new_data, indent=2))
    return changed


def migrate_profile(
    profile_dir: Path, *, old: str, new: str, apply: bool, timestamp: str
) -> None:
    db_path = profile_dir / "state.db"
    sessions_path = profile_dir / "sessions" / "sessions.json"

    db_count = (
        migrate_state_db(db_path, old=old, new=new, apply=apply, timestamp=timestamp)
        if db_path.exists()
        else 0
    )
    json_count = (
        migrate_sessions_json(
            sessions_path, old=old, new=new, apply=apply, timestamp=timestamp
        )
        if sessions_path.exists()
        else 0
    )

    verb = "Migrated" if apply else "Would migrate"
    if db_count or json_count:
        print(
            f"{profile_dir}: {verb} {db_count} gateway_routing row(s), "
            f"{json_count} sessions.json entr{'y' if json_count == 1 else 'ies'}"
        )
    else:
        print(f"{profile_dir}: nothing to migrate")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "profile_dirs",
        nargs="+",
        help="One or more Hermes profile directories "
        "(e.g. ~/.hermes/profiles/myprofile, or ~/.hermes for the default profile)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes (default: dry run only)",
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help=f"Migrate {DEFAULT_NEW} -> {DEFAULT_OLD} instead of the default "
        f"{DEFAULT_OLD} -> {DEFAULT_NEW}",
    )
    args = parser.parse_args()

    old, new = (
        (DEFAULT_NEW, DEFAULT_OLD) if args.reverse else (DEFAULT_OLD, DEFAULT_NEW)
    )

    if not args.apply:
        print("Dry run (no changes written) -- pass --apply to migrate for real.\n")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for raw_dir in args.profile_dirs:
        profile_dir = Path(raw_dir).expanduser()
        if not profile_dir.is_dir():
            print(f"{profile_dir}: not a directory, skipping", file=sys.stderr)
            continue
        migrate_profile(
            profile_dir, old=old, new=new, apply=args.apply, timestamp=timestamp
        )


if __name__ == "__main__":
    main()
