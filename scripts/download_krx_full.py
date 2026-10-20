"""Resumable KRX Open API download; run the full range in a separate cmd window."""

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from kr_quant.krx import KRXClient


DAILY_SERVICES = ("stk_bydd_trd", "ksq_bydd_trd", "kosdaq_dd_trd")
MONTHLY_SERVICES = ("stk_isu_base_info", "ksq_isu_base_info")


def weekdays(start: date, end: date):
    current = start
    while current <= end:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def _checkpoint(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp.replace(path)


def download(client: KRXClient, start: date, end: date, progress_path: Path) -> dict:
    """Resume from cached service/date responses; stop on errors without skipping."""
    if start > end:
        raise ValueError("Start date must not follow end date")
    state = {"range": [start.isoformat(), end.isoformat()], "completed_dates": 0,
             "closed_weekdays": 0, "snapshots": 0, "last_complete_date": None,
             "status": "running"}
    month_last = None

    def snapshot(day: date) -> None:
        for service in MONTHLY_SERVICES:
            rows = client.get(service, day)["OutBlock_1"]
            if not rows:
                raise ValueError(f"Empty basic-info snapshot: {service} on {day}")
        state["snapshots"] += 1
        _checkpoint(progress_path, state)
        print(f"Snapshot {day}: {state['snapshots']} month(s), {client.request_count} new calls", flush=True)

    try:
        for day in weekdays(start, end):
            if month_last is not None and (day.year, day.month) != (month_last.year, month_last.month):
                snapshot(month_last)
                month_last = None

            index_rows = client.get("kospi_dd_trd", day)["OutBlock_1"]
            if not index_rows:
                state["closed_weekdays"] += 1
                state["last_complete_date"] = day.isoformat()
                _checkpoint(progress_path, state)
                continue
            if sum(row.get("IDX_NM", "").strip() == "코스피 200" for row in index_rows) != 1:
                raise ValueError(f"Missing or ambiguous exact KOSPI 200 index on {day}")
            for service in DAILY_SERVICES:
                rows = client.get(service, day)["OutBlock_1"]
                if not rows:
                    raise ValueError(f"Empty {service} for KOSPI index trading day {day}")
            month_last = day
            state["completed_dates"] += 1
            state["last_complete_date"] = day.isoformat()
            _checkpoint(progress_path, state)
            if state["completed_dates"] % 25 == 0:
                print(f"{day}: {state['completed_dates']} trading dates, "
                      f"{state['snapshots']} snapshots, {client.request_count} new calls", flush=True)
        if month_last is not None:
            snapshot(month_last)
    except (RuntimeError, PermissionError) as exc:
        state["status"] = "paused" if "ceiling" in str(exc) else "failed"
        state["reason"] = str(exc)
        _checkpoint(progress_path, state)
        raise
    except Exception:
        state["status"] = "failed"
        _checkpoint(progress_path, state)
        raise

    state["status"] = "complete"
    state["new_calls_this_run"] = client.request_count
    _checkpoint(progress_path, state)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2014, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2024, 12, 31))
    args = parser.parse_args()
    client = KRXClient()
    progress = Path("outputs/krx_full/progress.json")
    print(f"KRX Open API {args.start}..{args.end}; cache {client.cache_dir}; "
          "self-imposed max 2500 new requests/Korean day", flush=True)
    try:
        result = download(client, args.start, args.end, progress)
    except RuntimeError as exc:
        if "ceiling" in str(exc):
            print("Daily ceiling reached. Run the same command on the next Korean day; cache resumes.")
            return
        raise
    print(f"Complete: {result['completed_dates']} trading dates, "
          f"{result['snapshots']} monthly snapshots; {client.request_count} new requests this run")


if __name__ == "__main__":
    main()
