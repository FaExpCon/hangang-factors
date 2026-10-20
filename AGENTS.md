# Agent Rules for kr-quant

## Data API Compliance

**Mandatory:** Use only official APIs when collecting Korean market data:

- **KRX Open API** for stock prices, trading value, market cap, and indices
- **OpenDART API** for financial statements and disclosures

**Prohibited:**
- Never call `data.krx.co.kr` (KRX Data Marketplace), including its website endpoints; use only the approved KRX Open API host `data-dbg.krx.co.kr` for KRX data.
- Automated scraping or bulk collection from KRX websites via pykrx or similar libraries
- Working around rate limits or IP blocks
- Any data collection method that violates provider Terms of Service

**Required practices:**
- KRX Open API: self-imposed maximum 2,500 request attempts per Korean calendar day and at least 1 second between attempts across local processes sharing the cache. The provider's actual daily limit is still unconfirmed; never treat this ceiling as provider authorization to exceed a lower documented limit.
- Stay well within each API's documented daily call limits
- Implement request throttling and checkpointing
- Cache all responses to minimize redundant calls
- Document the actual limits obtained from official provider documentation in code comments

**Rationale:** Automated bulk collection from KRX Data Marketplace violates their Terms of Service and can result in IP blocking. This project must comply with all data provider terms.

## Implementation Notes

- The original pykrx bulk downloader (`scripts/download_universe.py`) is disabled and preserved only as reference code
- New downloaders must be designed around official API limits before implementation
- Agent should verify compliance requirements with the user before accessing any new data source

## Working Practices

**Security:**
- Never print, log, or commit secrets
- Read credentials and API keys from `.env` only
- `.env` is gitignored; verify this stays true

**Development process:**
- Work in small stages
- After each stage, run the full test suite and stop for review
- Never claim something is implemented unless a test covers it
- Tests and simulated runs must use temporary directories. They must never write to `outputs/` or `data/cache/` (including the Windows paths `outputs\` and `data\cache\`). Mocked or synthetic responses must never be merged into real output files.
- Any authorized run against real outputs, including a cache-only production re-merge or restore, must be stated explicitly in the final report with its paths and effects. A simulation is never evidence of production completeness.

**Long-running commands:**
- For commands that take longer than about 2 minutes (bulk downloads, full backtests), don't run them yourself
- Give the exact Windows `cmd.exe` command instead, so the user can run it in a separate window

**Verification:**
- Before finishing any task, list anything you could not verify yourself
- State what was tested and what requires user verification or longer-running commands

**Final reports:**
- When finishing a task and stopping for review, write the full final report to `docs/agent_reports/latest.md` (overwrite) and append a copy to `docs/agent_reports/YYYY-MM-DD_HHMM.md` using the local completion date/time. If that timestamped file exists, append rather than replace it.
- Never include secrets in either report.
