#!/usr/bin/env python
"""Repair or rebuild the local bots.db from data.json."""

from __future__ import annotations

import json
from pathlib import Path

from flask import Flask
from backend.models import db, Group, Account

DATA_FILE = Path(__file__).resolve().parent.parent / "data.json"
DB_PATH = Path("bots.db")


def load_data() -> dict:
    if DATA_FILE.is_file():
        return json.loads(DATA_FILE.read_text())
    return {"accounts": [], "groups": []}


def rebuild_db(app: Flask | None = None) -> None:
    if app is None:
        from backend import create_app
        app = create_app()
    data = load_data()
    with app.app_context():
        db.drop_all()
        db.create_all()

        # map account id -> group id from group definitions
        id_to_gid: dict[int, int] = {}
        for grp in data.get("groups", []):
            group = Group(
                name=grp.get("name"),
                target=grp.get("target", "unknown"),
                interval=grp.get("interval", 600),
            )
            db.session.add(group)
            db.session.flush()
            for aid in grp.get("accounts", []):
                id_to_gid[aid] = group.id

        for acc in data.get("accounts", []):
            account = Account(
                id=acc.get("id"),
                username=acc.get("email"),
                password=acc.get("password"),
                proxy=acc.get("proxy"),
                group_id=id_to_gid.get(acc.get("id")),
            )
            db.session.add(account)
        db.session.commit()
    print("Database rebuilt at", DB_PATH)


def main() -> None:
    rebuild_db()


if __name__ == "__main__":
    main()
