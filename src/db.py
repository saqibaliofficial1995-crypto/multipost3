"""SQLite job queue. Ek video = ek job, har platform ka apna status.
Isse retry safe rehta hai aur duplicate upload nahi hota.
"""
import sqlite3
import os
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    src_path      TEXT UNIQUE NOT NULL,
    norm_path     TEXT,
    title         TEXT,
    description   TEXT,
    hashtags      TEXT,
    public_url    TEXT,
    created_at    REAL NOT NULL,
    normalized    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS uploads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER NOT NULL,
    platform     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|done|failed|skipped
    remote_id    TEXT,
    remote_url   TEXT,
    error        TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL,
    UNIQUE(job_id, platform)
);

CREATE TABLE IF NOT EXISTS daily_counter (
    day          TEXT PRIMARY KEY,
    count        INTEGER NOT NULL DEFAULT 0
);
"""


@contextmanager
def conn(db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    c = sqlite3.connect(db_path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init(db_path):
    with conn(db_path) as c:
        c.executescript(SCHEMA)


def add_job(db_path, src_path, title, description, hashtags, public_url, platforms):
    """Naya job add karo. Agar pehle se hai to kuch na karo (idempotent)."""
    with conn(db_path) as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO jobs(src_path,title,description,hashtags,public_url,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (src_path, title, description, hashtags, public_url, time.time()),
        )
        if cur.rowcount == 0:
            return None
        job_id = c.execute(
            "SELECT id FROM jobs WHERE src_path=?", (src_path,)
        ).fetchone()["id"]
        for p in platforms:
            c.execute(
                "INSERT OR IGNORE INTO uploads(job_id,platform,updated_at) VALUES(?,?,?)",
                (job_id, p, time.time()),
            )
        return job_id


def set_norm_path(db_path, job_id, norm_path):
    with conn(db_path) as c:
        c.execute(
            "UPDATE jobs SET norm_path=?, normalized=1 WHERE id=?", (norm_path, job_id)
        )


def next_pending(db_path, max_attempts=3):
    """Ek pending upload row return karo (oldest job pehle)."""
    with conn(db_path) as c:
        return c.execute(
            "SELECT u.*, j.src_path, j.norm_path, j.title, j.description, "
            "       j.hashtags, j.public_url "
            "FROM uploads u JOIN jobs j ON j.id = u.job_id "
            "WHERE u.status='pending' AND u.attempts < ? "
            "ORDER BY j.created_at ASC, u.id ASC LIMIT 1",
            (max_attempts,),
        ).fetchone()


def mark(db_path, upload_id, status, remote_id=None, remote_url=None, error=None):
    with conn(db_path) as c:
        c.execute(
            "UPDATE uploads SET status=?, remote_id=?, remote_url=?, error=?, "
            "attempts=attempts+1, updated_at=? WHERE id=?",
            (status, remote_id, remote_url, error, time.time(), upload_id),
        )


def job_fully_done(db_path, job_id):
    with conn(db_path) as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM uploads WHERE job_id=? AND status IN ('pending','failed')",
            (job_id,),
        ).fetchone()
        return row["n"] == 0


def bump_daily(db_path, day):
    with conn(db_path) as c:
        c.execute(
            "INSERT INTO daily_counter(day,count) VALUES(?,1) "
            "ON CONFLICT(day) DO UPDATE SET count=count+1",
            (day,),
        )


def daily_count(db_path, day):
    with conn(db_path) as c:
        row = c.execute("SELECT count FROM daily_counter WHERE day=?", (day,)).fetchone()
        return row["count"] if row else 0


def report(db_path):
    with conn(db_path) as c:
        return c.execute(
            "SELECT j.title, u.platform, u.status, u.remote_url, u.error "
            "FROM uploads u JOIN jobs j ON j.id=u.job_id ORDER BY u.id DESC LIMIT 50"
        ).fetchall()
