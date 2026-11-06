"""Shared ingest plumbing: column mapping, raw-file bookkeeping, bulk upsert.

Every source module turns a raw CSV into (source_id, canon, raw) tuples and
hands them to `load_records`, which writes `source_records` in bulk.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

import httpx
import polars as pl
import psycopg

# Fixed namespace so IDs are stable across rebuilds (saved targets keep pointing
# at the same business after a re-run).
NAMESPACE = uuid.UUID("6f0e3f6c-2b8e-4a8e-9d5e-0d7b1c7a9e21")


def stable_id(*parts: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, ":".join(parts))


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", h.lower())


class ColumnMappingError(ValueError):
    pass


@dataclass(frozen=True)
class ColumnSpec:
    """canonical field -> accepted header spellings (compared case/punct-insensitively)."""
    fields: Mapping[str, tuple[str, ...]]
    required: tuple[str, ...]
    drop_patterns: tuple[str, ...] = ()    # regexes on normalized header; matching columns are never stored

    def resolve(self, header: list[str]) -> dict[str, str]:
        lookup = {_norm_header(h): h for h in header}
        out: dict[str, str] = {}
        for canon, aliases in self.fields.items():
            for alias in aliases:
                if _norm_header(alias) in lookup:
                    out[canon] = lookup[_norm_header(alias)]
                    break
        missing = [f for f in self.required if f not in out]
        if missing:
            raise ColumnMappingError(
                f"Could not find required column(s) {missing}.\n"
                f"Columns in file: {header}\n"
                f"Add the right spelling to the aliases in the source module."
            )
        return out

    def dropped(self, header: list[str]) -> list[str]:
        return [h for h in header if any(re.search(p, _norm_header(h)) for p in self.drop_patterns)]


def read_header(path: Path) -> list[str]:
    return pl.read_csv(path, n_rows=0, infer_schema=False).columns


def scan_csv(path: Path) -> pl.LazyFrame:
    # Everything as text; we parse types ourselves so one bad cell can't sink a load.
    return pl.scan_csv(path, infer_schema=False, encoding="utf8-lossy", truncate_ragged_lines=True)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stash_raw_file(path: Path, source: str, raw_dir: Path, pulled: date) -> Path:
    """Copy the raw file into raw_dir/<source>/<date>/ untouched (scope 3.2 step 1)."""
    dest_dir = raw_dir / source / pulled.isoformat()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if path.resolve() != dest.resolve():
        shutil.copy2(path, dest)
    return dest


def download(url: str, dest: Path, timeout: float = 600) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "dealfeed-ingest/0.1"}) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    tmp.replace(dest)
    return dest


def start_run(conn: psycopg.Connection, source: str, path: Path) -> int:
    row = conn.execute(
        "INSERT INTO pipeline_runs (source, file_path, file_sha256) VALUES (%s, %s, %s) RETURNING id",
        (source, str(path), file_sha256(path)),
    ).fetchone()
    conn.commit()
    return row["id"]


def finish_run(conn: psycopg.Connection, run_id: int, status: str, rows_read: int | None = None,
               rows_loaded: int | None = None, notes: str | None = None) -> None:
    conn.execute(
        "UPDATE pipeline_runs SET finished_at = now(), status = %s, rows_read = %s, rows_loaded = %s, notes = %s "
        "WHERE id = %s",
        (status, rows_read, rows_loaded, notes, run_id),
    )
    conn.commit()


def _json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(type(o))


def load_records(conn: psycopg.Connection, source: str, run_id: int,
                 records: Iterable[tuple[str, dict, dict]], pulled_at: datetime | None = None) -> int:
    """Bulk upsert into source_records keyed on (source, source_id).

    New records get first_seen_at = pulled_at; existing ones keep theirs. Links
    to businesses (business_id, match_method) are preserved on re-load.
    """
    pulled_at = pulled_at or datetime.now(timezone.utc)
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _stage (id UUID, source_id TEXT, canon JSONB, raw JSONB) ON COMMIT DROP"
    )
    n = 0
    seen: set[str] = set()
    with conn.cursor().copy("COPY _stage (id, source_id, canon, raw) FROM STDIN") as cp:
        for source_id, canon, raw in records:
            if source_id in seen:      # duplicate rows inside one file: keep first
                continue
            seen.add(source_id)
            cp.write_row((
                stable_id(source, source_id), source_id,
                json.dumps(canon, default=_json_default), json.dumps(raw, default=_json_default),
            ))
            n += 1
    conn.execute(
        """
        INSERT INTO source_records (id, source, source_id, pulled_at, first_seen_at, run_id, canon, raw)
        SELECT id, %(source)s, source_id, %(pulled)s, %(pulled)s, %(run)s, canon, raw FROM _stage
        ON CONFLICT (source, source_id) DO UPDATE
           SET pulled_at = EXCLUDED.pulled_at, run_id = EXCLUDED.run_id,
               canon = EXCLUDED.canon, raw = EXCLUDED.raw
        """,
        {"source": source, "pulled": pulled_at, "run": run_id},
    )
    conn.commit()
    return n
