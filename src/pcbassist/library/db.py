"""SQLite-backed component library loaded from ``data/components_seed.json``.

The JSON seed is the source of truth (it is reviewable and diffable); SQLite gives fast
filtered search for candidate retrieval. The DB is rebuilt in memory on load unless a path
is given.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..paths import data_dir
from ..schemas import LibraryPart

_SCHEMA = """
CREATE TABLE IF NOT EXISTS parts (
    mpn          TEXT PRIMARY KEY,
    manufacturer TEXT NOT NULL,
    category     TEXT NOT NULL,
    description  TEXT NOT NULL,
    tags         TEXT NOT NULL,      -- space-separated, padded with spaces for LIKE matching
    supply_min   REAL,
    supply_max   REAL,
    current_max  REAL,
    footprint    TEXT NOT NULL,
    body         TEXT NOT NULL       -- full LibraryPart JSON
);
CREATE INDEX IF NOT EXISTS parts_category ON parts(category);
"""


@dataclass(frozen=True)
class SearchQuery:
    categories: Sequence[str] = ()
    keywords: Sequence[str] = ()
    any_tags: Sequence[str] = ()
    all_tags: Sequence[str] = ()
    supply_v: float | None = None
    limit: int = 25


class ComponentLibrary:
    def __init__(self, parts: Iterable[LibraryPart], db_path: Path | str = ":memory:") -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.executescript(_SCHEMA)
        self._parts: dict[str, LibraryPart] = {}
        rows = []
        for p in parts:
            if p.mpn in self._parts:
                raise ValueError(f"duplicate MPN in library: {p.mpn}")
            self._parts[p.mpn] = p
            rows.append(
                (
                    p.mpn,
                    p.manufacturer,
                    p.category,
                    p.description,
                    " " + " ".join(t.lower() for t in p.tags) + " ",
                    p.supply_range.min_v if p.supply_range else None,
                    p.supply_range.max_v if p.supply_range else None,
                    p.current.max_ma,
                    p.footprint,
                    p.model_dump_json(),
                )
            )
        self._conn.executemany("INSERT OR REPLACE INTO parts VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        self._conn.commit()

    @classmethod
    def load(
        cls, seed_path: Path | None = None, db_path: Path | str = ":memory:"
    ) -> ComponentLibrary:
        path = seed_path or data_dir() / "components_seed.json"
        raw = json.loads(path.read_text())
        items = raw["parts"] if isinstance(raw, dict) else raw
        return cls((LibraryPart.model_validate(x) for x in items), db_path=db_path)

    def __len__(self) -> int:
        return len(self._parts)

    def __contains__(self, mpn: object) -> bool:
        return isinstance(mpn, str) and self.get(mpn) is not None

    def all(self) -> list[LibraryPart]:
        return list(self._parts.values())

    def categories(self) -> list[str]:
        cur = self._conn.execute("SELECT DISTINCT category FROM parts ORDER BY category")
        return [str(r[0]) for r in cur.fetchall()]

    def get(self, mpn: str) -> LibraryPart | None:
        """Exact MPN lookup, falling back to a case/whitespace-insensitive match."""
        if mpn in self._parts:
            return self._parts[mpn]
        norm = _norm(mpn)
        for k, v in self._parts.items():
            if _norm(k) == norm:
                return v
        return None

    def search(self, q: SearchQuery) -> list[LibraryPart]:
        sql = ["SELECT mpn FROM parts WHERE 1=1"]
        args: list[object] = []
        if q.categories:
            sql.append(f"AND category IN ({','.join('?' * len(q.categories))})")
            args += list(q.categories)
        for kw in q.keywords:
            sql.append("AND (lower(mpn) LIKE ? OR lower(description) LIKE ? OR tags LIKE ?)")
            like = f"%{kw.lower()}%"
            args += [like, like, like]
        if q.any_tags:
            sql.append("AND (" + " OR ".join("tags LIKE ?" for _ in q.any_tags) + ")")
            args += [f"% {t.lower()} %" for t in q.any_tags]
        for t in q.all_tags:
            sql.append("AND tags LIKE ?")
            args.append(f"% {t.lower()} %")
        if q.supply_v is not None:
            sql.append("AND (supply_min IS NULL OR (supply_min <= ? AND supply_max >= ?))")
            args += [q.supply_v, q.supply_v]
        sql.append("ORDER BY category, mpn LIMIT ?")
        args.append(q.limit)
        cur = self._conn.execute(" ".join(sql), args)
        return [self._parts[str(r[0])] for r in cur.fetchall()]


def _norm(mpn: str) -> str:
    return re.sub(r"[\s\-_/(),.]+", "", mpn).upper()
