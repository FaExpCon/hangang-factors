"""Verify production output against the original baseline and disk cache only."""

import argparse
import hashlib
from collections import Counter
from pathlib import Path

from kr_quant.dart_gap import build_record, read_json, atomic_json

BASELINE_SHA = "e2b645c74e2bd4ca0e0cbdee62e6f14ca6f332f64dcae6cd8881b76e1df35b25"


def verify(output_file, baseline_file, plan_file, *, expected_baseline_sha=BASELINE_SHA):
    assert hashlib.sha256(Path(baseline_file).read_bytes()).hexdigest() == expected_baseline_sha
    baseline = read_json(baseline_file)
    output = read_json(output_file)
    assert output[:len(baseline)] == baseline, "Baseline objects changed"
    companies = {c["stock_code"]: c for c in read_json(plan_file)["companies"]}
    additions = output[len(baseline):]
    sources = {}
    verified = 0
    period_keys = set()
    record_keys = set()
    for record in output:
        key = record["stock_code"], int(record["year"])
        assert key not in record_keys, "Duplicate company-year"
        record_keys.add(key)
        for p in record.get("fiscal_periods", []):
            pk = record["stock_code"], p["end"]
            assert pk not in period_keys, "Duplicate actual period"
            period_keys.add(pk)
            assert int(p["end"][:4]) == int(record["year"]), "Wrong fiscal year"
    for record in additions:
        refs = record.get("gap_fill", {}).get("source_responses")
        assert refs, "No disk cache provenance"
        filings, accounts, fallback_accounts = [], [], None
        for ref in refs:
            path = ref["path"]
            if path not in sources:
                raw = Path(path).read_bytes()
                sources[path] = hashlib.sha256(raw).hexdigest(), read_json(path)
            digest, payload = sources[path]
            assert digest == ref["sha256"], "Source hash changed"
            assert payload["status"] in ("000", "013"), "Invalid cache status"
            rows = payload.get("list", [])
            if ref["endpoint"] == "list":
                filings.extend(rows)
            elif ref["endpoint"] == "fnlttMultiAcnt":
                accounts.extend(rows)
            elif ref["endpoint"] == "fnlttSinglAcnt":
                fallback_accounts = [{**r, "corp_code": ref["params"]["corp_code"]} for r in rows]
        used_fallback = record["gap_fill"].get("used_single_company_fallback", False)
        expected = build_record(companies[record["stock_code"]], int(record["year"]), filings,
                                fallback_accounts if used_fallback else accounts)
        actual = {**record, "gap_fill": {k: v for k, v in record["gap_fill"].items()
                                         if k not in ("source_responses", "used_single_company_fallback")}}
        assert actual == expected, f"Record not reproduced by cache: {record['stock_code']} {record['year']}"
        verified += 1
    return {"baseline_records": len(baseline), "output_records": len(output),
            "cache_verified_additions": verified, "unverified_or_synthetic_additions": len(additions)-verified,
            "source_files_verified": len(sources), "duplicate_periods": 0,
            "baseline_by_year": dict(sorted(Counter(r["year"] for r in baseline).items())),
            "output_by_year": dict(sorted(Counter(r["year"] for r in output).items())),
            "additions_by_year": dict(sorted(Counter(r["year"] for r in additions).items())),
            "account_status": dict(Counter(r["gap_fill"]["accounts_status"] for r in additions)),
            "output_sha256": hashlib.sha256(Path(output_file).read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-file", type=Path, default=Path("outputs/opendart_full/annual_financials_FY2015_2024.json"))
    parser.add_argument("--baseline-file", type=Path, default=Path("outputs/opendart_full/gap_backups/annual_financials_FY2015_2024_before_gap.json"))
    parser.add_argument("--plan-file", type=Path, default=Path("outputs/opendart_gap/audit_reviewed.json"))
    parser.add_argument("--report", type=Path, default=Path("outputs/opendart_gap/recovery/verification.json"))
    args = parser.parse_args()
    result = verify(args.output_file, args.baseline_file, args.plan_file)
    atomic_json(args.report, result)
    import json
    print(json.dumps(result, indent=2))
    print("CACHE RECOVERY VERIFIED")


if __name__ == "__main__":
    main()
