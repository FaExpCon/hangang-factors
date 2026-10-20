# Hangang Factors

Point-in-time research on Korean equity factors with official KRX/OpenDART data — pre-registered, survivorship-adjusted, and honestly failed.

Public repository: [FaExpCon/hangang-factors](https://github.com/FaExpCon/hangang-factors).
The private development folder remains `C:\Users\user\kr-quant`; exported Python
imports remain `kr_quant` (the distribution metadata also remains `kr-quant`).

## Clean public snapshot

This project publishes source, synthetic tests, curated documentation and derived
research figures. It does not distribute provider datasets or trading records.

## Source-repository cleanup — user commands only

From `C:\Users\user\kr-quant` in Windows cmd.exe:

```bat
git ls-files -- cache/opendart
git rm -r --cached -- cache/opendart
git ls-files -- cache/opendart
git check-ignore --no-index cache/opendart/corpCode.json
.venv\Scripts\python.exe -c "from pathlib import Path; import json; p=Path('cache/opendart/corpCode.json'); print('Local cache retained:', p.is_file()); print('Corporation entries:', len(json.loads(p.read_text(encoding='utf-8'))))"
```

The first inventory currently contains 616 files. `git rm --cached` changes the
index only and preserves local files. The second `git ls-files` should print
nothing. Existing collection/assembly code continues to use the same disk cache
paths. Ignoring files does not remove previous commits: **do not publish this
source repository's existing history unchanged**. No history rewrite is needed
for the clean snapshot below.

## Exact public manifest

[`PUBLIC_EXPORT_MANIFEST.txt`](PUBLIC_EXPORT_MANIFEST.txt) is the complete explicit
file list. New files are not automatically included by a directory wildcard.
The exporter includes that manifest so recipients can inspect the intended scope.
The current Hangang Factors snapshot contains **80 explicitly listed files**.

It excludes `.git`, `.env`, all `data/`, `outputs/`, `cache/`, raw directories,
provider responses, agent reports/incident archives, gate ledgers, legacy status
documents, virtual environments, build artifacts and logs. Only three approved
derived PNG figures are permitted as binary assets. Code/tests can contain API
schema examples and synthetic fixtures; downloaded response documents are not
permitted. The source repository's incident narrative remains in the curated
research report, without the raw incident evidence.

`tests/test_cache_reading.py` is deliberately excluded: it depends on a real
private February 2024 cache and cannot pass in a data-free public checkout.
Synthetic cache/price tests remain included. This exclusion changes only the
public snapshot contents, not the original test or research behavior.

The public manifest also excludes the disabled bulk-download script, unused
pykrx assembler draft and its tests, and the three early one-off scripts.
`src/kr_quant/bulk.py` and `tests/test_bulk.py` are **retained**: exported
`data.py` imports its `VERSION` for legacy cache reads, and `__main__.py` imports
the bulk classes. Removing it would leave unresolved imports. This is a
dependency exception, not authorization to run legacy collection. Its history
is explained in the research report without links to omitted files. The
package, private code and private test files are not renamed or deleted.

## Export and initialize — not executed by the agent

Read-only validation and exact list (does not create the destination):

```bat
.venv\Scripts\python.exe scripts\export_public_repo.py --destination C:\Users\user\hangang-factors --list
```

Copy the approved snapshot after review:

```bat
.venv\Scripts\python.exe scripts\export_public_repo.py --destination C:\Users\user\hangang-factors
```

The destination must be a new folder outside the source repository; it must not
exist yet. Linked/reparse source files and linked destination ancestry are
rejected. All files are scanned before the destination is created and again
before each write. A modified source, forbidden path/type, suspected credential
or raw-data pattern aborts the export. If a later I/O failure leaves a partial
directory, do not publish it; inspect it and choose a new destination after
review. No cleanup or overwrite is attempted automatically.

The default destination, when `--destination` is omitted, is
`C:\Users\user\hangang-factors`. Re-export means exporting the reviewed source
again to this **new** destination. An existing destination is never cleared,
overwritten or merged; use another new folder or review it yourself first.

The script never calls Git. After inspecting the exported file tree yourself,
you may initialize its **new, independent** history:

```bat
git -C C:\Users\user\hangang-factors init -b main
git -C C:\Users\user\hangang-factors remote add origin https://github.com/FaExpCon/hangang-factors.git
git -C C:\Users\user\hangang-factors status --short
```

The remote command records a URL locally; it does not create a GitHub repository
or contact GitHub. Create an empty `FaExpCon/hangang-factors` on GitHub separately
when ready. No staging, commit, remote or push is performed by the exporter or this task.
Review files, provider redistribution terms and the three PNGs before publishing.
The source repository's `.env.example` contains only placeholder keys.

## Checks and limitations

The exporter uses an allowlist plus forbidden path/type rules, private-key/token/
credential patterns, exact configured credential matching, provider-payload
patterns and restricted PNG metadata. It prints file paths only when reporting
findings; secret values are never printed. It must be run with no concurrent
writers modifying source/destination trees; it is not a filesystem sandbox.
No finite pattern list can establish the absence of every possible secret,
personal detail or provider-data fragment. Human publication review is still
required. Scan Git hosting assets separately; the clean export does not remove
old remote history, releases, reflogs or backups.

```bat
.venv\Scripts\python.exe -m pytest tests\test_export_public_repo.py -q
```

Exporter tests use temporary fake source/destination trees only. They do not
export the real repository or create Git repositories.
