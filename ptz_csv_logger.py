from __future__ import annotations

import csv
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_PTZ_CSV_LOCK = threading.Lock()
_PTZ_CSV_PATH = Path.cwd() / "ptz_log.csv"
_PTZ_DBG_PATH = Path.cwd() / "ptz_debug.log"


def _csv_header():
    return [
        "ts_utc",
        "source",
        "channel",
        "auth",
        "http_code",
        "pan",
        "tilt",
        "zoom",
        "body_len",
        "parse_err",
        "url",
    ]


def log_ptz_row(
    *,
    source: str,
    url: str,
    http_code: int,
    channel: int,
    auth: str,
    body: str,
    parsed: dict | None,
    err: str | None,
) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    pan = parsed.get("pan") if parsed else None
    tilt = parsed.get("tilt") if parsed else None
    zoom = parsed.get("zoom") if parsed else None
    row = [
        ts,
        source,
        channel,
        auth,
        http_code,
        pan,
        tilt,
        zoom,
        len(body or ""),
        (err or "")[:160],
        url,
    ]

    with _PTZ_CSV_LOCK:
        new_file = not _PTZ_CSV_PATH.exists()
        with open(_PTZ_CSV_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(_csv_header())
            w.writerow(row)
            f.flush()
            os.fsync(f.fileno())

    if err:
        with open(_PTZ_DBG_PATH, "a", encoding="utf-8") as g:
            g.write(f"{ts} ERR={err} URL={url}\nBODY:\n{(body or '')[:1000]}\n---\n")
