"""Data readiness only: verify local calendar/files without strategy evaluation."""

import argparse
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

from kr_quant.bulk_cached import month_bounds
from kr_quant.company_overview import save_json
from scripts.run_offline_assembler import offline_only


def audit_range(start, end, cache_dir):
    first, _ = month_bounds(start)
    _, last = month_bounds(end)
    if first > last:
        raise ValueError("Start follows end")
    warmup = first - timedelta(days=400)
    sessions, missing, invalid = [], [], []
    day = warmup
    while day <= last:
        if day.weekday() < 5:
            index = Path(cache_dir) / "kospi_dd_trd" / f"{day:%Y%m%d}.json"
            if not index.exists():
                missing.append(str(index))
            else:
                rows = json.loads(index.read_text(encoding="utf-8"))["OutBlock_1"]
                if rows:
                    exact = [r for r in rows if r.get("IDX_NM", "").strip() == "코스피 200"]
                    if len(exact) != 1 or exact[0].get("BAS_DD") != f"{day:%Y%m%d}":
                        invalid.append(str(index))
                    sessions.append(day)
                    for service in ("kosdaq_dd_trd", "stk_bydd_trd", "ksq_bydd_trd"):
                        path = Path(cache_dir) / service / f"{day:%Y%m%d}.json"
                        if not path.exists():
                            missing.append(str(path))
        day += timedelta(days=1)
    month_ends = [d for i, d in enumerate(sessions) if d >= first and
                  (i == len(sessions)-1 or sessions[i+1].month != d.month)]
    for signal in month_ends:
        for service in ("stk_isu_base_info", "ksq_isu_base_info"):
            path = Path(cache_dir) / service / f"{signal:%Y%m%d}.json"
            if not path.exists():
                missing.append(str(path))
    expected = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        expected.append(f"{y:04d}-{m:02d}")
        y, m = (y+1, 1) if m == 12 else (y, m+1)
    actual = [str(d)[:7] for d in month_ends]
    missing_months = sorted(set(expected) - set(actual))
    return {"status": "ready" if not (missing or invalid or missing_months) else "incomplete",
            "purpose": "Data/calendar file presence only; no strategy targets, backtests or returns",
            "network_requests": 0, "start": start, "end": end,
            "first_session": str(next((d for d in sessions if d >= first), "")),
            "last_session": str(sessions[-1]) if sessions else None,
            "sessions_in_range": sum(d >= first for d in sessions),
            "warmup_sessions": sum(d < first for d in sessions),
            "month_count": len(month_ends), "missing_months": missing_months,
            "month_ends": [str(d) for d in month_ends], "missing_files": missing, "invalid_indices": invalid}


def input_identity(financials, cache_dir, company_dir, disclosures):
    """Freeze financial bytes and source metadata; not a byte hash of all prices."""
    manifest = []
    for root in (cache_dir, company_dir, disclosures):
        for path in sorted(Path(root).rglob("*.json")):
            if "request" in path.name or path.name == "progress.json":
                continue
            stat = path.stat()
            manifest.append((str(path), stat.st_size, stat.st_mtime_ns))
    return {"financials_sha256": hashlib.sha256(Path(financials).read_bytes()).hexdigest(),
            "source_metadata_sha256": hashlib.sha256(json.dumps(manifest).encode()).hexdigest(),
            "source_file_count": len(manifest),
            "source_fingerprint_convention": "sorted path/size/mtime_ns, not all market-data bytes",
            "code_sha256": hashlib.sha256(b"".join(
                str(p).encode() + p.read_bytes() for folder in ("src/kr_quant", "scripts")
                for p in sorted(Path(folder).glob("*.py")))).hexdigest()}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default="2016-07")
    p.add_argument("--end", default="2024-12")
    p.add_argument("--cache-dir", type=Path, default=Path("data/cache/krx_open_api/v1"))
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    with offline_only():
        result = audit_range(args.start, args.end, args.cache_dir)
        save_json(args.output, result)
    print(f"{result['status']}: {result['month_count']} months, {result['sessions_in_range']} sessions; zero requests, no performance evaluated")
    if result["status"] != "ready":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
