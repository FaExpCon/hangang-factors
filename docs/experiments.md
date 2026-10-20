# Experiment register

Registered: 2026-10-19. Status: candidates for later testing; no default strategy
rules have been changed. No candidate has been selected or validated.

## Evidence motivating the experiments

The completed twelve-run comparison's best no-halt variant was the default
accounting variant. Sources:

- `outputs/comparison_2016-07_2020-11.csv`
- `outputs/halt_selection_comparison/2016-07_2020-11_none_default.json`
- Saved-run audit: `outputs/diagnosis_2016-07_2020-11/diagnosis.md`
- Detailed prior review: `docs/agent_reports/2026-10-19_2048.md`

These observations cover **2016-07-01 through 2020-11-30**, use halt=none, and
omit dividends. They are development evidence, not out-of-sample validation.

| Period | Default strategy | KOSPI200 | KOSDAQ |
|---|---:|---:|---:|
| Jul–Dec 2016 | -5.61% | +5.47% | -7.72% |
| 2017 | -1.44% | +24.90% | +26.44% |
| 2018 | -5.70% | -19.33% | -15.38% |
| 2019 | -5.37% | +12.13% | -0.86% |
| Jan–Nov 2020 | +29.23% | +17.80% | +32.29% |
| Full-period CAGR | +1.61% | +7.98% | +6.03% |
| Full-period MDD | -40.48% | -41.19% | -53.79% |

Annual returns include the first session's return from the prior year-end close;
the first and final years are partial. The strategy starts in cash and first
buys in August 2016, while index buy-and-hold starts at the first July close.

The largest gaps to KOSPI200 were in 2017 and 2019. The strategy beat KOSPI200
in 2018 and Jan–Nov 2020. Its lifetime drawdown ran from January 29, 2018 to
March 19, 2020, explaining why a modest calendar-2018 loss can coexist with
approximately -40% MDD.

Average invested exposure was **73.64%**, including **58.35% in 2019** and
86.99% in 2017. Low exposure deserves investigation, but it does not alone
explain the high-exposure 2017 shortfall. Annual turnover was **3.132**, with
**1,348 trades**. Modeled commissions, sell tax and slippage totaled
**99,817.95 KRW per 1,000,000 KRW initial equity**, or **2.47% annualized
against average net equity**. This cost sum is not a no-cost CAGR estimate;
a separate rerun is needed for compounding and holdings-path effects. There
were no unfilled orders or artificial conversions in the saved baseline log.

The equal-weight eligible-universe benchmark, standalone factor tests,
filter-on/off comparisons and zero-cost reruns were prepared for offline
execution. Their historical outcomes were not established in the saved-run
audit. Do not attribute the gap to a specific factor or the filter before
reviewing those results. The eligible-universe benchmark is fairer for
selection analysis than KOSPI200 alone, but is not a pure small/mid-cap index:
eligible names can include large caps, and the bottom capitalization group is
excluded. Use the same costs and startup convention for simulated comparisons.

The local annual export (20,826 company-years) and official daily-price inputs
lack dated per-share dividend entitlements. Dividends remain unmeasured for
both portfolios and indices; the differential contribution matters for relative
performance. Current KSIC classifications, final-corrected annual values at
original filing dates, illustrative taxes/costs and historical survivor coverage
gaps also limit interpretation.

## Pre-registered candidates

Keep the **market filter enabled** for every candidate: the existing
market-specific index close versus its trailing 200-session average, with
`below_ma_exposure=0.5`. Filter-off runs are diagnostic comparisons, not
candidates for adoption in this register. Keeping the filter is a research
constraint, not a claim that its performance benefit has been proved.

Test each change separately against the default baseline first. Preserve all
other eligibility, entry, position, sector, cost, execution and data policies.

| ID | Candidate | Comparison with the current default | Hypothesis |
|---|---|---|---|
| T-Q | Quarterly rebalance | Calendar-quarter-end signals in March, June, September and December instead of every month; retain rank 30. | Fewer rebalances may reduce trading frictions, with a possible cost from slower responses. |
| T-40 | Wider retention rank 40 | Monthly rebalance; retain eligible held names through rank 40 instead of 30; new entries remain top 20. | A wider retention buffer may reduce turnover without materially weakening selection. |
| T-50 | Wider retention rank 50 | Monthly rebalance; retain eligible held names through rank 50 instead of 30; new entries remain top 20. | A larger buffer may further reduce turnover, but may retain weaker names longer. |
| Q-OA | Operating-income quality | Rank operating income / year-end total assets instead of PBR/PER; keep default factor weights. | An operating profitability measure may be more useful than the PBR/PER approximation. |
| W-532 | Lower momentum weight | Value 50%, quality 30%, momentum 20%, instead of 40% / 30% / 30%; keep default PBR/PER quality for this isolated test. | Less momentum exposure may improve net performance or reduce turnover. |

For T-Q, form signals after the final market session of each calendar quarter
and execute at the next tradable session's open using prior-close information.
Keep daily risk observations, pending-sale retries and suspension accounting.
Apply market-filter scaling at those rebalance signals. With the existing July
cash start, the first quarterly signal is September month-end; disclose that
startup difference rather than silently adding an initial monthly rebalance.

For Q-OA, use operating income and assets from the same annual report and
accounting basis as the selected fundamentals. Missing operating income or
missing/nonpositive assets makes the ratio unavailable; do not backfill from
another year or basis. Negative operating income remains rankable under the
existing optional quality metric. A separate positive-operating-income filter
is not part of this candidate. All other default eligibility rules remain.

Quarterly rebalancing and wider retention are initially alternative turnover
experiments, not an unrestricted grid search. Any combined candidate must be
recorded here with its exact configuration and rationale **before** running
that combination, and assessed only within the development window. Record all
attempts, including unsuccessful ones; the existing operating-income comparisons
are already part of the development history.

## Selection and one-time validation protocol

1. **Use 2016–2020 only to choose among candidates.** The current evidence is
   July 2016–November 2020; it is not a full five-calendar-year result. Freeze
   exact common development start/end dates, warm-up and cash initialization
   before running candidate comparisons. Any extension within 2016–2020 stays
   development data and requires rerunning the baseline and benchmarks on the
   same dates. Never extend development selection into 2021–2024.
2. Complete the pending diagnosis and compare candidates with the unchanged
   default and the monthly equal-weight eligible universe, plus KOSPI200 and
   KOSDAQ. Evaluate net CAGR, lifetime MDD, Sharpe, Sortino, annual turnover,
   trades, exposure and year-by-year returns. Use costs-on results for selection;
   zero-cost results explain friction sensitivity. Review return improvements
   together with drawdown and turnover, rather than selecting on gross returns
   alone. Record the decision criteria and tie-breaks before inspecting new
    candidate outcomes; the frozen protocol below defines the selection rule.
3. Hold data versions, accounting availability, costs and halt policy constant
    across development comparisons. Judge candidates using the halt=resume
    runs under the frozen rule below; halt=none runs remain diagnostics only.
    This does not alter the live/manual rule. Keep the market filter on for candidates.
   If the delisted-company supplement changes development coverage, rerun all
   finalists, the baseline and eligible benchmark on that completed data before
   freezing the choice. Preserve earlier approximate results as historical runs.
4. **Do not run or inspect 2021–2024 candidate validation until both the KRX
   download and delisted-company gap fill are complete and coverage is
   reconciled.** Data-completeness checks may occur without inspecting candidate
   performance. Document remaining unmatched identities, empty fundamentals,
   revision/classification limitations and suspension assumptions.
5. Before opening validation results, freeze one selected candidate, its exact
   parameters, implementation version, data version, costs, halt policy, common
   benchmark definitions, dates and startup/warm-up rules, and the validation
   success/failure criteria. Pre-2021 observations may supply indicator warm-up
   but must not become validation returns. Record why the candidate was chosen
   using only 2016–2020 evidence. Retaining the current default is a valid outcome
   if candidates are not convincing.
6. **Validate the frozen choice once on 2021-01-01 through 2024-12-31**, using
   actual market sessions within those bounds. Evaluate the predeclared baseline
   and benchmarks alongside it, without ranking the discarded candidates on this
   period. Record the result whether favorable or unfavorable. Do not retune on
   2021–2024 and call a repeat test out-of-sample; further changes would need a
   new research record and fresh untouched validation data.
7. Stop for review before adopting any change. This register authorizes no
   default-rule modification, new data acquisition or API request.

## Execution status

### Frozen design comparison protocol — 2026-10-20

This protocol is recorded before inspecting any new candidate outcomes. The
runner is `python -m scripts.compare_registered_candidates` and accepts only
**2016-07 through 2020-12**. The data-readiness/assembler range may extend to
**2024-12**, in data-only mode. Do not run a 2021–2024 strategy or inspect its
returns at this stage.

- Exactly twelve primary jobs: baseline, T-Q, T-40, T-50, Q-OA and W-532,
  each under halt=none and halt=resume. No combined candidates, positive
  operating-income filter or filter-off variants in this matrix.
- Market filter stays on with a 200-session MA and below-MA exposure 0.5.
  All default costs, entry rank 20, sector and position caps remain fixed.
- Initialize 1,000,000 KRW cash on the first July 2016 market session. Use
  400 calendar days of pre-start data and require 253 sessions for a snapshot.
  Monthly entry signals begin July month-end; T-Q starts September month-end,
  executes next-session open and has no added initial monthly signal. Final
  December 2020 close has no new signal without a next session in the window.
- Daily risk observations, sale retries, suspension conversion and the existing
  monthly halt-resume recovery checks continue for T-Q; allocation/market-filter
  scaling only occurs at quarter ends. Judge using halt=resume runs;
  halt=none runs remain diagnostics only.
- Q-OA operating income and its asset denominator must be from the same full
  twelve-month report/basis. During transition-period income fallback, use
  that income report's assets for quality; latest balance-sheet values can
  still supply PBR. No mixing a prior full-year numerator with transition
  assets, annualization of short periods, or optional-value backfill.
- Add two clearly labeled monthly eligible equal-weight references, one per
  halt policy, with the same costs, cash start and market filter. They spread
  1/N across eligible stocks and do not impose top-20/retention or sector caps;
  these are references, not additional selectable candidates. Compare the
  candidate matrix to KOSPI200 and KOSDAQ price buy-and-hold as well.
- Save every run and partial-progress table. Report net CAGR, lifetime MDD,
  Sharpe, Sortino, annual turnover, trades, mean exposure and calendar-year
  returns (2016 partial). No gross-return-only selection.
- Selection rule amended before the user or agent inspects the running design
  comparison's results; this supersedes the earlier halt=none/CAGR review screen:
  - Judge using the **halt=resume** runs.
  - Primary metric: **net Sharpe ratio over 2016-07 through 2020-12**.
  - Constraint: lifetime **MDD must not be more than 5 percentage points worse
    than the halt=resume baseline**. With the runner's negative signed MDD,
    require `candidate_mdd >= baseline_mdd - 0.05` (for example, a baseline
    MDD of -30% permits a candidate MDD down to -35%).
  - If two eligible candidates are within **0.05 Sharpe**, choose the simpler
    one with lower turnover.
  - If no eligible candidate beats the baseline's net Sharpe, **keep the baseline**.
  - Freeze the chosen configuration, including halt=resume and the recorded
    data/code/cost/startup settings, for **one validation run on 2021–2024**.
    Do not tune or rank discarded candidates on validation results. Stop for
    review before validation, following the one-time validation protocol above.
- The runner writes a protocol/configuration/data/code fingerprint before the
  first job. Financial JSON is SHA-256 hashed; market/cache source identity
  uses path, size and modification-time metadata (not every price-file byte).
  Current KSIC is still a forward-looking proxy. Gap companies without a cached
  company overview remain unknown/excluded; no classification requests are
  authorized. Missing accounts, 30 held identities and the excluded 082660
  FY2024 item remain disclosed coverage limitations.

The selection amendment above was recorded before viewing results (commit
`b152640`). The completed design attempt is reviewed below. Earlier approximate
results remain historical evidence only; production strategy defaults remain
unchanged and no 2021–2024 validation result has been opened.

### Design selection and configuration freeze — 2026-10-20

Source: `outputs/registered_design_2016-07_2020-12_attempt2/comparison.csv`
and its fourteen saved runs. The interrupted first attempt is excluded from
this decision. All completed runs share 1,106 sessions from 2016-07-01 through
2020-12-30 and one protocol/data identity. This is development evidence only.

Apply the existing rule, without changes:

1. Use only **halt=resume** baseline and selectable candidates. Halt=none is
   diagnostic. Eligible equal-weight and index portfolios are references only.
2. Resume baseline Sharpe is **0.09462845365531666**; signed MDD is
   **-0.23577808852290338**. The permitted MDD floor is therefore
   **-0.2857780885229034** (−28.5778088523%).
3. T-Q, T-40, T-50 and W-532 pass that MDD constraint. Q-OA fails:
   MDD −29.3459921176%, worse than baseline by 5.7681832653 percentage points.
4. **W-532 is the only eligible candidate beating baseline Sharpe**, at
   **0.4110738567195005**. Its MDD is **−25.5662207001%**, worse than baseline
   by only **1.9884118479 percentage points**. All other candidates' Sharpes
   are below baseline. Thus the keep-baseline condition does not apply.
5. The 0.05 Sharpe simplicity/lower-turnover tie-break is not triggered:
   no other qualifying baseline-beating candidate is within 0.05 of W-532.
   Even the highest-Sharpe alternative, T-50, is 0.3698761086 below W-532.
   Do not use stronger halt=none T-40/T-50 results to override this rule.
6. **Freeze W-532 with halt=resume for one 2021–2024 validation.** No validation
   is run or authorized by this result review, and no default is adopted.

Frozen configuration (the exact JSON in `runs/resume_W-532.json` is authoritative):

| Setting | Frozen value |
| --- | --- |
| Value / quality / momentum weights | **0.50 / 0.30 / 0.20** |
| Within value: inverse-PER / inverse-PBR | 0.50 / 0.50 |
| Quality | PBR/PER (`pbr_per`); not Q-OA |
| Halt policy | **resume**, existing 63-session/dual-index recovery policy |
| Rebalance | Monthly |
| Entry / retention ranks | 20 / 30 |
| Positions / base weight / sector cap | 20 / 5% / 30% |
| Market filter | On; own-market 200-session MA; below-MA exposure 0.5 |
| Liquidity / bottom-cap exclusion | 1 billion KRW 20-session mean / 30% |
| Data / extra fundamental lag | approximate / 0 days |
| Commission / sell tax / slippage | 15 / 30 / 10 basis points |
| Warning / halt drawdown | 15% / 20% |
| Untradable conversion / recovery | 20 sessions / 0% |
| Positive-operating-income / holding-company exclusions | false / false |
| Accounting diagnostic ratio | 3.0; flags only |
| Initial cash | 1,000,000 KRW |
| Fundamental policy | Original filing availability with latest corrected values; short-period income fallback; same-report quality assets |
| Classification | Cached current KSIC proxy, unchanged unknown-data exclusions |

Frozen evidence identity:

```text
comparison.csv SHA-256:
21ab626aaee9454a1ec3ffc9320815b06780265b7e43dce4902a98631d8ef803
protocol SHA-256:
83312bdc145eea01274c978c91d13a54fab35ff8ade38d147fd1dccec8639d4e
resume_W-532.json SHA-256:
95a6462ce2c365ebe519c81c3fa81baf44ece77e07401687069c8eca8a43728e
financials SHA-256:
53a8842511dc9e809ceb80b9471e49801834ce6647d61833794790b963788a22
source metadata SHA-256:
4f1b72b5b18ec894875a9d07f2e2713403a0e029d7535317159bf6ea5e856d98
code SHA-256:
fa5fc6ae9b9f4a38ba70a92960f68df35074043f313963503a4043bd5bda36d7
```

Machine-readable validation freeze (configuration identity excludes performance
results; adding launcher tests does not change the frozen engine):

```json
{
  "freeze_type": "W-532-validation-v1",
  "configuration_sha256": "79d40e068e28f43a2a39ec08e104f3d7e9bc085c7144571a98b7d45d32a0cfaf",
  "winner_artifact_sha256": "95a6462ce2c365ebe519c81c3fa81baf44ece77e07401687069c8eca8a43728e",
  "design_protocol_sha256": "83312bdc145eea01274c978c91d13a54fab35ff8ade38d147fd1dccec8639d4e",
  "financials_sha256": "53a8842511dc9e809ceb80b9471e49801834ce6647d61833794790b963788a22",
  "source_metadata_sha256": "4f1b72b5b18ec894875a9d07f2e2713403a0e029d7535317159bf6ea5e856d98",
  "source_file_count": 14705,
  "engine_code_sha256": "fa5fc6ae9b9f4a38ba70a92960f68df35074043f313963503a4043bd5bda36d7",
  "start": "2021-01",
  "end": "2024-12"
}
```

The source fingerprint covers 14,705 files by path/size/mtime, not a byte hash
of every price file. These fingerprints matched the current files at review.
The saved run manifest's older “none for selection” description is stale;
the pre-results rule amendment above controls this decision. Saved artifacts
were not edited to conceal that discrepancy.

Validation remains one untouched run on **2021-01-01 through 2024-12-31**,
starting fresh in 1,000,000 KRW cash with 400 calendar days of indicator warm-up
and at least 253 sessions for snapshots, using monthly close signals and next
session opens. Pre-2021 data supplies warm-up only, not validation returns.
Evaluate the predeclared baseline and references alongside the frozen choice,
without reranking discarded candidates or retuning. The validation criteria
pre-registered below now define success/failure. Execution still waits for review.

The chosen run's design CAGR is **4.8425569529%**, MDD **−25.5662207001%**,
Sharpe **0.4110738567**, Sortino **0.5634112777**, annual turnover **1.9707211310**,
860 trades and 448 halted end-of-session observations. This is a rule-based
design selection, not evidence of out-of-sample success or statistical
significance. No validation performance, rule modification or live adoption
has occurred. Full side-by-side results and caveats are in the agent report.

### One-time validation judgment — pre-registered before execution

The selected configuration remains **W-532 with halt=resume**. Evaluate once
over **2021-01 through 2024-12** (actual market sessions within January 1,
2021–December 31, 2024), with the frozen data, costs, accounting, market filter,
cash initialization and warm-up conventions. No validation results have been
run or viewed when recording these criteria.

**Primary criteria — determine pass/fail. Pass only if all three hold:**

1. W-532/resume **net Sharpe is strictly higher** than the eligible equal-weight
   portfolio with halt=resume, over the identical validation sessions. The
   reference retains its registered costs, market filter and cash start.
2. W-532/resume net Sharpe is **at least 0.8 times KOSPI200's Sharpe**, **OR**
   its MDD is **at least 10 percentage points smaller** than KOSPI200's MDD.
   Use the existing price buy-and-hold KOSPI200 reference and existing metric
   definitions. With negative signed MDD, the second branch is
   `W532_mdd >= KOSPI200_mdd + 0.10`. This is an absolute percentage-point
   reduction in drawdown magnitude, not a 10% relative improvement. Apply the
   `0.8 * Sharpe` threshold literally, including if the index Sharpe is negative.
3. W-532/resume **net CAGR is strictly positive**.

Equivalently:

```text
PASS = (W532_resume_Sharpe > eligible_equal_weight_resume_Sharpe)
   AND ((W532_resume_Sharpe >= 0.8 * KOSPI200_Sharpe)
        OR (W532_resume_MDD >= KOSPI200_MDD + 0.10))
   AND (W532_resume_net_CAGR > 0)
```

If any required primary metric is missing/nonfinite, the evaluation is
**BLOCKED**, never a pass. Report each inequality and both branches of criterion
2 explicitly. No post-result changes to thresholds, benchmarks, costs, or halt
policy are permitted.

**Secondary diagnostics — report only; never change the choice or pass/fail:**

- W-532 with **halt=none**.
- The frozen baseline with **halt=resume and halt=none**.
- T-50 with **halt=none**, as the specifically authorized diagnostic comparator;
  it is not an alternative validation selection or a reopening of the search.
- KOSPI200 and KOSDAQ price references, and year-by-year returns for all reported
  portfolios/references.
- Sensitivity of the design-period result to halt/resume timing: report saved
  W-532 and T-50 none-versus-resume differences, halted exposure/days and the
  available halt/resume event evidence. These comparisons include changes in
  holdings, cash and execution paths, so do not claim a pure causal timing
  contribution or reinterpret them as another selection rule.

**Pre-validation observation:** W-532 won the design selection under
**halt=resume** (Sharpe 0.4111), while **T-50 led the selectable candidates under
halt=none** (Sharpe 0.3821 versus W-532 0.3416). W-532's resume Sharpe was 0.0695
higher than its no-halt Sharpe; T-50's resume Sharpe was 0.3409 lower than its
no-halt Sharpe. The choice may therefore be sensitive to halt/resume timing.
This observation does not unfreeze W-532, justify retuning, or alter any primary
validation criterion.

The single validation launcher is `python -m scripts.validate_frozen_candidate`.
It schedules six portfolios: W-532/resume (primary), eligible equal-weight/resume
(primary reference), W-532/none, baseline/resume, baseline/none and T-50/none.
Index references are taken from the same validation sessions. It preserves the
design-period lock in the candidate-search runner and refuses to overwrite an
existing validation directory. It was not executed or tested in the original
preregistration turn. Subsequent verification used temporary fake-data tests
and the read-only dry run described below; **real validation remains unrun**.

No other discarded candidates are run on validation. Record the result whether
pass or fail, keep the chosen configuration frozen, and stop for review before
adoption. This preregistration does not authorize execution in this turn.

Single command, from the repository root, for the user to run after review:

```bat
.venv\Scripts\python.exe -m scripts.validate_frozen_candidate --design-runs outputs\registered_design_2016-07_2020-12_attempt2\runs --output-dir outputs\frozen_validation_2021_2024
```

### Launcher verification before real validation

The dry-run form appends `--dry-run` to the command above. Both modes read the
machine-readable freeze in this register, check the exact signed design
artifact and canonical W-532/resume configuration fingerprint, verify baseline
and diagnostic configurations, and require frozen data/source identities.
The strategy engine/source digest is checked separately from the new launcher.

The actual read-only dry run passed: **48 months, 983 market sessions and 270
pre-start warm-up sessions**, with required local index/stock daily files and
monthly snapshots present. It printed the six planned portfolios and references,
without constructing `BulkCachedData`, scoring, trading, computing validation
returns or creating the real validation output directory. The presence audit
does not replace full daily-row parsing and snapshot checks at real execution.

Temporary fake-data tests cover frozen-config loading/rejection, fingerprint
drift, exact date window, read-only dry-run behavior, known pass/fail boundary
cases and missing metrics, missing data, and output-directory refusal. Real
execution checks the output directory before preflight and uses an atomic
exclusive directory creation before loading portfolios; an existing directory,
even empty or from an interrupted run, blocks another invocation for review.
No `--force` or silent overwrite option exists. At launcher verification time,
the real validation had not been run. The subsequent one-time result is
recorded below; the preregistration and its thresholds remain unchanged.

### One-time validation result — 2026-10-20: FAIL

Source: `outputs/frozen_validation_2021_2024/`. The user completed the single
registered validation. Six saved portfolios cover the same **983 sessions,
2021-01-04 through 2024-12-30**. This review reads saved artifacts only; no
strategy rerun or retuning occurs.

**Primary W-532/resume result:** net CAGR **−5.3653802249%**, lifetime MDD
**−35.5729951338%**, net Sharpe **−0.3799197571**, Sortino **−0.5195340442**,
and cumulative net return **−19.7346648847%**.

Apply the preregistered inequalities exactly, including the literal multiple
of a negative index Sharpe:

| Primary condition | Numbers | Outcome |
| --- | --- | --- |
| Sharpe strictly above eligible equal-weight/resume | −0.3799197571 > −0.2595094790 | **False** |
| Sharpe at least 0.8 × KOSPI200 Sharpe | −0.3799197571 >= 0.8 × (−0.2267628118) = −0.1814102494 | **False** |
| OR MDD magnitude at least 10 pp smaller than KOSPI200 | Signed MDD: −0.3557299513 >= −0.3611262489 + 0.10 = −0.2611262489 | **False** |
| Net CAGR strictly positive | −0.0536538022 > 0 | **False** |

Criterion 2 fails because both OR branches fail. All three primary criteria
fail, so the final preregistered verdict is **FAIL**, matching saved
`judgment.json`. W-532's drawdown magnitude is only **0.5396297526 percentage
points smaller** than KOSPI200's, rather than the required ten. No threshold,
benchmark, halt policy or chosen configuration is changed to reinterpret this.

Secondary diagnostics are **report-only and do not affect the verdict**:

| Portfolio/reference | Net CAGR / index price CAGR | MDD | Sharpe |
| --- | ---: | ---: | ---: |
| Eligible equal-weight/resume | −4.2639% | −32.6242% | −0.2595 |
| W-532/none | −9.8941% | −49.2469% | −0.5332 |
| Baseline/resume | −8.8551% | −45.8630% | −0.5779 |
| Baseline/none | −11.2848% | −51.3910% | −0.5944 |
| T-50/none | −11.9223% | −52.4244% | −0.6463 |
| KOSPI200 price buy-and-hold | −5.5989% | −36.1126% | −0.2268 |
| KOSDAQ price buy-and-hold | −8.7655% | −40.8481% | −0.2806 |

In validation, resume versus none improves W-532 CAGR by **4.5287558296 pp**,
Sharpe by **0.1532783853** and signed MDD by **13.6738963863 pp**, but does not
make the primary result pass. W-532/resume has 540 halted observations and
43.81% mean exposure versus 72.55% with halt=none. Saved events show halts on
2022-01-27 and 2024-01-22, with automatic resumption on 2023-05-02.

Design timing sensitivity remains as preregistered: W-532 resume-minus-none
Sharpe **+0.0694785167**, T-50 **−0.3409168483**; respective CAGR differences
**+0.3230608891 pp** and **−5.8109471748 pp**. Both resumed on 2020-08-03,
after design halts on 2018-10-11 (W-532) and 2018-10-26 (T-50). These are
different holdings/exposure/execution paths, not an isolated causal timing
estimate. T-50's diagnostic validation result does not reopen selection.

All primary metrics and secondary yearly returns were recalculated from saved
equity/index curves; trades, turnover, halted counts, configuration/data
identity and the frozen engine digest were reconciled. The primary computation
matches saved judgment exactly. Full tables, year-by-year returns, timing
events and limitations are in `docs/agent_reports/latest.md` and the timestamped
validation review report.

Evidence SHA-256:

```text
validation comparison.csv:
411e0e8198f79564203469a03be28216d0698b69299dc8272cd375dc66f703bb
validation judgment.json:
45e0450d60df88f2716159a14325b68c81f9b54ed59497a7ffaa178ff54d9bb8
validation W-532_resume.json:
85c84de137c324f883df05f61bc519a5bd8ace436e4e8e451a6c3aa1417a0773
```

The one-time validation is now consumed. The chosen configuration and original
criteria remain frozen as the research record; this failed validation authorizes
no adoption, replacement, new candidate search, rerun or rule change. No changes
are proposed from these outcomes. Stopped for review.
