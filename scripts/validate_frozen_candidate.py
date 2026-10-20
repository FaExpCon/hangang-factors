"""One-time validation launcher with read-only preflight; see docs/experiments.md.

Run by the user after review. No parameter search, design-run overwrite, or API
access. The frozen design artifacts supply exact baseline and W-532 configs.
"""

import argparse
import hashlib
import json
import re
from dataclasses import asdict, replace
from datetime import date
from math import isfinite
from pathlib import Path
from statistics import mean

from kr_quant.backtest import HaltPolicy
from kr_quant.bulk_cached import BulkCachedData
from kr_quant.company_overview import DEFAULT_CACHE, save_json
from kr_quant.config import BacktestConfig, Costs, DataMode, FactorWeights, QualityMetric
from scripts.audit_offline_range import audit_range, input_identity
from scripts.compare_offline_backtests import write_table
from scripts.compare_registered_candidates import BASE, reference_output
from scripts.diagnose_offline_backtest import yearly_returns
from scripts.run_offline_assembler import offline_only
from scripts.run_offline_backtest import run_cached_backtest


WINNER_SHA = "95a6462ce2c365ebe519c81c3fa81baf44ece77e07401687069c8eca8a43728e"
START, END = "2021-01", "2024-12"


def configuration_fingerprint(config):
    payload = {"candidate": "W-532", "halt_policy": "resume", "config": config}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def frozen_registration(path):
    blocks = re.findall(r"```json\s*\n(.*?)\n```", path.read_text(encoding="utf-8"), re.DOTALL)
    registrations = [v for block in blocks if isinstance(v := json.loads(block), dict)
                     and v.get("freeze_type") == "W-532-validation-v1"]
    if len(registrations) != 1:
        raise ValueError("Expected one machine-readable freeze in experiment register")
    reg = registrations[0]
    if (reg["start"], reg["end"]) != (START, END):
        raise ValueError("Registered validation window must remain 2021-01 through 2024-12")
    return reg


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def config_from_saved(value):
    value = dict(value)
    if value["periods"] is not None:
        raise ValueError("Unexpected sliced configuration in frozen design")
    value.update(data_mode=DataMode(value["data_mode"]),
                 quality_metric=QualityMetric(value["quality_metric"]),
                 factors=FactorWeights(**value["factors"]), costs=Costs(**value["costs"]))
    return BacktestConfig(**value)


def judgment(chosen, eligible):
    w = chosen.get("performance", {}).get("strategy", {})
    index = chosen.get("performance", {}).get("KOSPI200", {})
    e = eligible.get("performance", {}).get("strategy", {})
    values = [w.get("sharpe"), w.get("mdd"), w.get("cagr"), index.get("sharpe"), index.get("mdd"), e.get("sharpe")]
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not isfinite(v) for v in values):
        return {"status": "BLOCKED", "reason": "Missing/nonfinite primary metric"}
    c1 = w["sharpe"] > e["sharpe"]
    sharpe_branch = w["sharpe"] >= .8 * index["sharpe"]
    mdd_branch = w["mdd"] >= index["mdd"] + .10
    c3 = w["cagr"] > 0
    return {"status": "PASS" if c1 and (sharpe_branch or mdd_branch) and c3 else "FAIL",
            "criterion_1": {"pass": c1, "chosen_sharpe": w["sharpe"], "eligible_sharpe": e["sharpe"]},
            "criterion_2": {"pass": sharpe_branch or mdd_branch, "sharpe_branch": sharpe_branch,
                            "sharpe_threshold": .8 * index["sharpe"], "mdd_branch": mdd_branch,
                            "chosen_mdd": w["mdd"], "mdd_threshold": index["mdd"] + .10},
            "criterion_3": {"pass": c3, "net_cagr": w["cagr"]}}


def code_without_launcher():
    # The only new executable is this launcher. Verify the old engine/source
    # identity separately rather than silently accepting changes to frozen code.
    return hashlib.sha256(b"".join(str(p).encode() + p.read_bytes()
        for folder in ("src/kr_quant", "scripts") for p in sorted(Path(folder).glob("*.py"))
        if p.resolve() != Path(__file__).resolve())).hexdigest()


def preflight(args):
    """Read-only checks shared by dry and real execution. No cache construction."""
    if args.output_dir.exists():
        raise FileExistsError("One-time validation artifacts must not be overwritten; stop for review")
    reg = frozen_registration(args.experiments)
    winner_path = args.design_runs / "resume_W-532.json"
    digest = hashlib.sha256(winner_path.read_bytes()).hexdigest()
    if digest != WINNER_SHA or digest != reg["winner_artifact_sha256"]:
        raise ValueError("Frozen W-532 artifact changed")
    winner = read(winner_path)
    baseline = read(args.design_runs / "resume_baseline.json")
    t50 = read(args.design_runs / "none_T-50.json")
    if winner.get("candidate") != "W-532" or winner.get("halt_policy") != "resume":
        raise ValueError("Only frozen W-532/resume may be primary")
    wcfg, bcfg, tcfg = (config_from_saved(r["config"]) for r in (winner, baseline, t50))
    if wcfg != replace(BASE, factors=FactorWeights(value=.5, quality=.3, momentum=.2)):
        raise ValueError("W-532 configuration differs from the frozen settings")
    if configuration_fingerprint(winner["config"]) != reg["configuration_sha256"]:
        raise ValueError("Configuration fingerprint differs from docs/experiments.md")
    if bcfg != BASE or tcfg != replace(BASE, retention_rank=50):
        raise ValueError("Baseline or diagnostic configuration differs from freeze")
    for saved, name, halt in ((baseline, "baseline", "resume"), (t50, "T-50", "none")):
        if saved.get("candidate") != name or saved.get("halt_policy") != halt:
            raise ValueError("Diagnostic identity differs from freeze")
    for saved in (winner, baseline, t50):
        if saved["protocol_sha256"] != reg["design_protocol_sha256"] or saved["data_identity"] != winner["data_identity"]:
            raise ValueError("Frozen design identities differ")
    for path in (args.financials, args.cache_dir, args.company_cache_dir, args.disclosures):
        if not path.exists():
            raise FileNotFoundError(f"Required data path absent: {path}")
    identity = input_identity(args.financials, args.cache_dir, args.company_cache_dir, args.disclosures)
    for key in ("financials_sha256", "source_metadata_sha256", "source_file_count"):
        if identity[key] != reg[key] or identity[key] != winner["data_identity"][key]:
            raise ValueError(f"Frozen data identity changed: {key}; stop before validation")
    engine_digest = code_without_launcher()
    if engine_digest != reg["engine_code_sha256"] or engine_digest != winner["data_identity"]["code_sha256"]:
        raise ValueError("Frozen engine/source identity changed; review before validation")
    readiness = audit_range(START, END, args.cache_dir)
    if readiness["status"] != "ready" or readiness["month_count"] != 48 or readiness["warmup_sessions"] < 253:
        raise ValueError("Validation data missing or insufficient warm-up; no simulation started")
    jobs = [("W-532", HaltPolicy.RESUME, wcfg), ("eligible_equal_weight", HaltPolicy.RESUME, bcfg),
            ("W-532", HaltPolicy.NONE, wcfg), ("baseline", HaltPolicy.RESUME, bcfg),
            ("baseline", HaltPolicy.NONE, bcfg), ("T-50", HaltPolicy.NONE, tcfg)]
    return winner, identity, jobs, readiness


def run(args):
    with offline_only():
        winner, identity, jobs, readiness = preflight(args)
        if args.dry_run:
            print("DRY RUN VERIFIED: frozen W-532/resume configuration matches docs/experiments.md")
            print(f"Configuration SHA-256: {configuration_fingerprint(winner['config'])}")
            print(f"Window: {START} through {END}; {readiness['month_count']} months; "
                  f"{readiness['sessions_in_range']} sessions; warm-up {readiness['warmup_sessions']} sessions")
            print("Required local calendar and data files exist; full daily-row parsing remains a real-run check.")
            for name, halt, _ in jobs:
                print(f"Planned: {name}/{halt.value}")
            print("References: KOSPI200 and KOSDAQ on matching sessions")
            print("No simulation, trades, returns, output-directory creation or API requests.")
            return {"status": "dry_run_verified", "configuration_sha256": configuration_fingerprint(winner["config"]),
                    "readiness": readiness, "planned_runs": [(n, h.value) for n,h,_ in jobs]}
        args.output_dir.mkdir(parents=True, exist_ok=False)  # Atomic claim; a concurrent launch must also fail.
        save_json(args.output_dir / "protocol.json", {
            "start": "2021-01", "end": "2024-12", "chosen": "W-532/resume",
            "criteria": "Sharpe > eligible/resume AND (Sharpe >= 0.8*KOSPI200 OR signed MDD >= KOSPI200+0.10) AND net CAGR > 0",
            "secondary_only": ["W-532/none", "baseline/resume", "baseline/none", "T-50/none"],
            "jobs": [{"candidate": n, "halt": h.value, "config": asdict(c)} for n,h,c in jobs],
            "data_identity": identity, "design_protocol_sha256": winner["protocol_sha256"],
            "launcher_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "initial_cash": 1_000_000, "warmup_calendar_days": 400})
        cache = BulkCachedData(START, END, cache_dir=args.cache_dir,
            financials_path=args.financials, company_cache_dir=args.company_cache_dir, disclosure_dir=args.disclosures)
        outputs, rows = {}, []
        expected_days = tuple(d for d in cache.trading_days if d >= cache.start)
        if (not expected_days or expected_days[0] < date(2021,1,1) or expected_days[-1] > date(2024,12,31)
                or str(expected_days[0]) != readiness["first_session"]
                or str(expected_days[-1]) != readiness["last_session"]
                or len(expected_days) != readiness["sessions_in_range"]):
            raise ValueError("Validation calendar outside frozen window")
        for i, (name, halt, cfg) in enumerate(jobs, 1):
            print(f"[{i}/{len(jobs)}] {name}/{halt.value}", flush=True)
            output = reference_output(cache, halt) if name == "eligible_equal_weight" else run_cached_backtest(cache, cfg, halt_policy=halt)
            days = tuple(date.fromisoformat(p["date"]) for p in output["equity"])
            if days != expected_days:
                raise ValueError("Portfolio validation sessions differ")
            key = f"{name}_{halt.value}"
            output.update(candidate=name, start="2021-01", end="2024-12", data_identity=identity)
            save_json(args.output_dir / f"{key}.json", output)
            outputs[key] = output
            row = {"candidate": name, "halt": halt.value, **output["performance"]["strategy"],
                   "annual_turnover": output["performance"]["annual_turnover"],
                   "trades": output["performance"]["trades"], "days_halted": output["days_halted"],
                   "mean_exposure": mean(1-p["cash"]/p["equity"] if p["equity"] else 0 for p in output["equity"])}
            for y in yearly_returns(tuple((date.fromisoformat(p["date"]),p["equity"]) for p in output["equity"])):
                row[f"return_{y['year']}"] = y["total_return"]
            rows.append(row)
            write_table(args.output_dir / "comparison.csv", rows)
        benchmarks = outputs["W-532_resume"]["benchmarks"]
        index_years = {n: yearly_returns(tuple((date.fromisoformat(p["date"]),p["close"]) for p in series))
                       for n,series in benchmarks.items()}
        timing = {}
        for name in ("W-532", "T-50"):
            none, resume = (read(args.design_runs / f"{h}_{name}.json") for h in ("none", "resume"))
            timing[name] = {"design_only": True,
                "resume_minus_none": {m: resume["performance"]["strategy"][m]-none["performance"]["strategy"][m]
                                      for m in ("cagr","mdd","sharpe")},
                "halted_days_resume": resume["days_halted"],
                "caveat": "Includes altered holdings/cash/execution paths; not isolated causal attribution"}
        save_json(args.output_dir / "judgment.json", {"primary": judgment(outputs["W-532_resume"], outputs["eligible_equal_weight_resume"]),
            "index_yearly_returns": index_years, "design_halt_timing_diagnostic": timing,
            "secondary_changes_choice": False, "validation_runs_completed": len(jobs)})
        print("Validation artifacts saved; stop for review. No retuning or automatic adoption.", flush=True)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", type=Path, default=Path("outputs/frozen_validation_2021_2024"))
    p.add_argument("--design-runs", type=Path, default=Path("outputs/registered_design_2016-07_2020-12_attempt2/runs"))
    p.add_argument("--financials", type=Path, default=Path("outputs/opendart_full/annual_financials_FY2015_2024.json"))
    p.add_argument("--cache-dir", type=Path, default=Path("data/cache/krx_open_api/v1"))
    p.add_argument("--company-cache-dir", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--disclosures", type=Path, default=Path("cache/opendart/list"))
    p.add_argument("--experiments", type=Path, default=Path("docs/experiments.md"))
    p.add_argument("--dry-run", action="store_true", help="Read-only freeze/file checks; never construct portfolios or report returns")
    return p.parse_args(argv)


def main():
    run(parse_args())


if __name__ == "__main__":
    main()
