<div align="center">
  <h1>CODEPRACTICE // AUTOMATED QA PIPELINE</h1>
  <p><code>HPS LABS // INTERNAL REFERENCE TOOL</code></p>
  <p>A local-first, multi-tier AI swarm for sandbox-verified code generation and consensus-graded MCQs.</p>

  <p>
    <code>[ LOCAL-FIRST ]</code>
    <code>[ SANDBOX-VERIFIED ]</code>
    <code>[ ZERO-DB ]</code>
    <code>[ RESUMABLE ]</code>
  </p>
</div>

> **Doctrine:** the model proposes, the sandbox disposes. Nothing is accepted because it "looks right" - it is accepted because it executes and matches the transcript.

---

### CURRENT SNAPSHOT

Snapshot from the latest local ledger. This is a working reference set, not a fresh benchmark.

| Metric | Result |
| :--- | ---: |
| OCR records | `55` |
| Reference items | `54` |
| Verified solutions | `51` |
| Code solutions (`pass`) | `43` |
| MCQ solutions (`confirmed`) | `8` |
| Items without a verified solution | `3` |
| Triage records | `24` |
| Failed extractions | `2` |

The three unsolved reference items are `26.2.5` (MCQ conflict), `29.1.1`, and `36.1.1` (source items classified as `other`, not code or MCQ). Duplicate triage rows can appear after a re-solve attempt; the unresolved set is de-duplicated by QID.

---

### SYSTEM ARCHITECTURE

Four tiers plus two offline passes. One producer, one fan-out, one verifier, one judge, one repair pool, one forensic re-checker, and one deterministic arbiter.

```text
questions/*.jpg  (local screenshots)
      |
      v
TIER 1 // OPTICAL
Local VLM via LM Studio (OpenAI-compatible API)
pixel budget -> temp 0.0 -> JSON-only prompt
emit -> extractions.jsonl / failed_extractions.jsonl
      |
      v
TIER 2 // COMPUTE SWARM
Groq 120B, one worker per API key, 20s/key pacing
skeleton detection -> JSON salvage -> test generation
emit -> solutions.jsonl / triage.jsonl / mcq_proposals.jsonl
      |
      +---------------------------+
      |                           |
      v                           v
TIER 3 // QA SANDBOX          TIER 4 // CONSENSUS
local subprocess, 5s timeout   local blind judge
compile gate                  confirm | conflict
strict / echo / transcript    sentinel handling
/ loose comparison
      |                           |
      +------------+--------------+
                   |
                   v
            REPAIR POOL (3 attempts, failing diffs replayed)
                   |
                   v
      reverify.py (targeted harness re-check)
      mcq_arbiter.py (deterministic interpreter arbitration)
                   |
                   v
      run_report.json -> build_site_data.py -> site_data.json
                   |
                   v
      index_auto.html (auto-loads site_data.json)
```

**Worker topology**

- `1x` local extraction thread - serial by design; the vision model owns the GPU.
- `Nx` Groq workers - one per API key; they share a single task queue.
- `1x` local judge - runs after extraction so vision and grading never contend for VRAM.
- `1x` repair pool - re-runs failed code tasks after the solve queue drains.

**Design constraints**

- `temperature = 0.0` everywhere. Deterministic output is a requirement, not a preference.
- No retry storms: a failed attempt becomes ledger state, not an infinite loop.
- No DB: append-only JSONL is the source of truth and the resume cursor.
- No screenshots leave the machine; only extracted text goes to the cloud.

---

### EXECUTION WORKFLOW

**Phase 0 - Boot & Resume**

1. Load `Stage1`, `groq-Stage2`, and `judge_mcqs` via `importlib`.
2. Read existing ledgers and reconstruct state: extracted files, solved QIDs, triage items.
3. Skip anything already solved; re-queue pending code/MCQ records.
4. Abort early if `GROQ_API_KEYS` is empty.

**Phase 1 - Optical Extraction (serial)**

1. Hash each image (MD5) and reject byte-identical duplicates.
2. Downscale to the pixel budget, JPEG-encode, base64 data-URI.
3. Call the local VLM with a JSON-only system prompt.
4. Parse content, then reasoning; one "nudge" retry if both fail.
5. Normalize defaults, dedupe options, and reclassify label-lists as MCQs.
6. Append the record to `extractions.jsonl`; failures also hit `failed_extractions.jsonl`.
7. Push `code` / `mcq` records into the shared task queue.

**Phase 2 - Solve Fan-Out (parallel)**

1. A Groq worker claims a record from the queue.
2. Code: detect `fragment` vs `full` mode, generate code + tests.
3. MCQ: return every correct option verbatim; normalize and match against the option list.
4. Flag broken starters before sending the payload (`starter_broken: true`).
5. Salvage truncated JSON if needed; generate missing tests with a second call.
6. Pace each key by `KEY_SLEEP = 20s`; back off 60s on HTTP 429.

**Phase 3 - Sandbox Verification (local)**

1. Build candidate variants: spliced, hint-stripped, hint-residue, standalone.
2. Compile before execution. A `SyntaxError` fails immediately.
3. Execute each viable candidate in a temporary `.py` file with a 5-second timeout.
4. Compare stdout using the strict -> echo -> transcript -> loose ladder.
5. Pass -> `solutions.jsonl`; fail -> `triage.jsonl`.

**Phase 4 - MCQ Consensus (local)**

1. Proposals are queued only after their answer matched an option.
2. The local judge returns `agree` plus its own correct option set.
3. Missing judge/proposer answers are treated as absent data, not automatic disagreement.
4. Agree -> `solutions.jsonl` (`status: "confirmed"`).
5. Disagree -> `triage.jsonl` (`status: "conflict"`).

**Phase 5 - Repair Pool**

1. Collect triage items whose file has no solution.
2. Replay the failing test diffs to the model, up to 3 attempts.
3. First passing attempt is written to `solutions.jsonl` with `"repaired": true`.
4. Exhausted attempts stay in triage and are counted in the report.

**Phase 6 - Targeted Re-verification**

`reverify.py` re-checks old triage code artifacts against the current harness without re-extracting or re-solving the queue. Tests are taken from the triage record, or reconstructed from stored results. Passing items are appended to `solutions.jsonl` with `harness_fix`, `tests_corrected`, or `referee_code` provenance. A still-failing item gets exactly one Groq referee call.

**Phase 7 - Deterministic MCQ Arbitration**

`mcq_arbiter.py` handles conflicts without an LLM. It extracts falsifiable claims such as `print(expr) returns X`, evaluates them in a 5-second subprocess, and accepts a side only if it contains every verifiable truth and no falsified option. Unfalsifiable or split conflicts stay for human review.

**Phase 8 - Report & Reference Build**

1. Write `run_report.json` with wall time, counters, solved count, and unresolved entries.
2. Optionally run `build_site_data.py` to merge ledgers into `site_data.json`.
3. Host `index_auto.html` and `site_data.json` together for a zero-backend reference site.

---

### VERIFICATION ENGINE

The sandbox is the only authority. Model confidence is irrelevant.

**Safety guarantees**

- Empty test lists always fail; there is no vacuous `all([])` pass.
- Every candidate is compiled before it is executed.
- Each test runs in a fresh temporary file, deleted in a `finally` block.
- Non-zero exit codes never pass, regardless of stdout.
- Stderr is captured (last 400 chars) and fed back to the repair prompt.
- Hidden-test discipline: prompts forbid hardcoding sample inputs.
- Tests are derived from visible samples; if absent, they are generated from Input/Output Format bullets.

**Compare ladder**

| Compare mode | Trigger | Behavior |
| :--- | :--- | :--- |
| `strict` | exact stdout lines | Right-stripped line lists are equal. |
| `echo` | prompt prefixes in sample output | Tolerates `input("prompt: ")` echoes that the platform injects. |
| `transcript` | sample written as a console session | Strips echoed stdin values and merges bare prompts with the next output line. |
| `loose` | leading whitespace differs only | Left-stripped lines are equal; flagged via `"loose": true`. |
| `fragment fallback` | spliced fragment fails | Re-runs the raw candidate as a standalone program. |

**Hint-line policy**

`HINT_RE` detects comprehension-shaped placeholder lines, but it is compile-gated: a line is stripped only when the regex matches and the line does not compile. Valid comprehensions are never touched. The same predicate is used during splicing, residue removal, and re-verification.

---

### DUPLICATE IMMUNITY (4 GATES)

Double-solving is treated as queue poisoning, not a minor inefficiency.

| Gate | Scope | Mechanism |
| :--- | :--- | :--- |
| **1. Content hash** | Image bytes | MD5 of the file; identical images are skipped before OCR. |
| **2. QID registry** | Semantic identity | A seen-`qid` map rejects repeats and records `duplicate_of`. |
| **3. Ledger resume** | Persisted state | Files already extracted or solved are skipped on restart. |
| **4. Atomic write guard** | Concurrent workers | A write lock plus an existence check on `qid` prevents duplicate solution/proposal rows. |

A duplicate that slips past the hash gate is stopped by the QID gate. A duplicate that races two workers is stopped by the write guard.

---

### RESILIENCE MATRIX

| Failure | Handler | Destination |
| :--- | :--- | :--- |
| Malformed model JSON | parse content -> parse reasoning -> one-shot nudge | retry / failure ledger |
| Truncated JSON | regex salvage of `code` and `tests` | candidate retained |
| Missing tests | dedicated test-generation call | same task |
| Code fails sandbox | repair pool with failing diffs (3 attempts) | solutions or triage |
| Broken or hint-only starter | compile gate + hint-stripped and residue variants | solutions or human review |
| HTTP 429 | 60-second backoff | same task |
| Rate pressure | fixed 20-second per-key pacing | same task |
| Duplicate image / QID | gates 1-3 | skipped + counted |
| OCR failure | recorded with raw output | `failed_extractions.jsonl` |
| Unparseable MCQ | no option match | `triage.jsonl` |
| MCQ conflict | deterministic arbiter, then human review | solutions or triage |
| Critical error | exception isolation per worker | run continues |

---

### CORE CAPABILITIES

- **Zero-Shot Skeleton Detection:** Auto-detects `match/case`, `if/elif`, and fragment vs. full-program modes from the locked starter code.
- **One Verification Engine:** `Stage2.py`, `main.py`, and `reverify.py` all delegate to the same `groq-Stage2.verify` contract.
- **Echo-Aware Sandbox:** Tolerates platform-specific `input("prompt: ")` echoes without weakening the strict diff.
- **Compile-Gated Hint Repair:** Broken placeholder lines are stripped, but valid comprehensions survive untouched.
- **Fire-and-Triage Routing:** Most items pass in one shot; edge cases enter a bounded feedback-loop repair pool.
- **Targeted Re-verification:** Old triage artifacts can be re-scored against a fixed harness without paying for extraction again.
- **Deterministic MCQ Arbitration:** Falsifiable options are evaluated by the interpreter before any human review.
- **Static Reference Node:** `build_site_data.py` compiles all ledgers into one `site_data.json`; `index_auto.html` loads it automatically.
- **4-Gate Duplicate Immunity:** Content hashes, QID semantics, resume cursors, and atomic ledger guards prevent double-solving and queue poisoning.
- **Fully Resumable:** Kill the process at any point; the next run reconstructs state from the ledgers and continues.
- **No Infrastructure:** No database, no queue service, no scheduler, no `.env` loader, no telemetry.

---

### LEDGER CONTRACTS

All state is append-only JSONL. No databases. No telemetry. These files are created at runtime and are not shipped in the repository.

| File | Role |
| :--- | :--- |
| `extractions.jsonl` | Raw OCR output: `qid`, `title`, `type`, `left_panel`, `options`, `starter_code`, `filename`. |
| `solutions.jsonl` | Verified payloads: final `code` + `tests`, or a confirmed MCQ `answer`. |
| `mcq_proposals.jsonl` | Groq's MCQ picks awaiting local consensus. |
| `triage.jsonl` | Repair pool: failed sandbox runs, unparseable replies, MCQ conflicts. |
| `failed_extractions.jsonl` | OCR failures with raw model output for diagnosis. |
| `run_report.json` | Final telemetry: wall time, counters, solved count, unresolved index. |
| `site_data.json` | Merged, QID-sorted reference dataset produced by `build_site_data.py`. |

**Status taxonomy**

| Status | Meaning |
| :--- | :--- |
| `pass` | Code executed and matched every generated test. |
| `confirmed` | MCQ answer survived local consensus. |
| `conflict` | Judge disagreed with the cloud answer. |
| `fail` | Code failed sandbox verification. |
| `unresolved` | Exhausted retries or no derived solution; retained for review. |

Optional provenance fields include `repaired`, `harness_fix`, `tests_corrected`, `referee_code`, `modes`, `judge_missing`, `proposer_missing`, and `arbiter`.

Full field-level schemas live in [`ARCHITECTURE.md`](./ARCHITECTURE.md).

---

### DEPLOYMENT

**Prerequisites**

- Python 3.10+ (tested on Python 3.14)
- LM Studio serving a vision-capable model on port `1234`
- One or more Groq API keys for the compute swarm

**Execution**

```bash
# 1. Install dependencies
pip install openai pillow

# 2. Inject cloud keys (PowerShell)
$env:GROQ_API_KEYS = "key1,key2,key3,key4,key5"
# OR (Bash/Zsh)
export GROQ_API_KEYS="key1,key2,key3,key4,key5"

# 3. Drop your screenshots into /questions

# 4. Run the orchestrator (resumes from existing ledgers)
python main.py

# 5. Re-check old triage code artifacts (optional)
python reverify.py

# 6. Arbitrate MCQ conflicts deterministically (optional)
python mcq_arbiter.py

# 7. Build the reference node data
python build_site_data.py
```

`python main.py --reset` wipes every ledger and starts from zero. It is destructive; use it only when intended.

**Standalone stages** - each resumes from its own ledger:

```bash
python Stage1.py        # OCR only
python groq-Stage2.py   # cloud solve only
python judge_mcqs.py    # local MCQ consensus only
python Stage2.py        # local-only solver
```

**Reference site**

`index_auto.html` fetches `site_data.json` from the same folder. Because browsers block `fetch` over `file://`, serve the folder over HTTP:

```bash
python -m http.server 8000
# open http://localhost:8000/index_auto.html
```

For a hosted deployment, place `index_auto.html` (or a copy named `index.html`) and `site_data.json` in the same web root.

**Resume semantics**

- Without `--reset`, the orchestrator is fully resumable.
- Extracted and solved files are skipped.
- Pending extraction records are re-seeded into the solve queue.
- `--reset` wipes every ledger and starts from zero.

**Rate-limit math**

- Groq free-tier budget is treated as ~8k TPM.
- A request is estimated at ~2.5k tokens.
- That is ~3 requests/minute/key.
- `KEY_SLEEP = 20` keeps each key at ~3 requests/minute.

---

### REPOSITORY LAYOUT

```text
entropy-harness/
  main.py                 orchestrator and worker lifecycle
  Stage1.py               local VLM extraction + normalization
  groq-Stage2.py          Groq solve, salvage, testgen, sandbox compare
  judge_mcqs.py           local blind MCQ consensus
  Stage2.py               standalone local-only solver
  reverify.py             targeted re-check of old triage artifacts
  mcq_arbiter.py          deterministic interpreter arbitration
  build_site_data.py      ledger merge -> site_data.json
  index_auto.html         static reference front end (auto-loads site_data.json)
  ARCHITECTURE.md         detailed tier + schema documentation
  README.md               this document
```

Runtime artifacts (`questions/`, `*.jsonl`, `run_report.json`, `site_data.json`) are generated locally and excluded from the repository.

---

### OPERATIONAL SECURITY & DATA PRIVACY

- **Local-First Vision:** Image processing never leaves the local GPU. No screenshots are uploaded to cloud APIs.
- **Text-Only Cloud Burst:** Only extracted text payloads (JSON) are sent to the Groq swarm.
- **No Secrets In Tree:** Keys are read from `GROQ_API_KEYS` at runtime. No credential is committed.
- **No Persistent State:** Ledgers are local JSONL files. No external logging, no analytics, no tracking.
- **Sandbox Scope:** The 5-second subprocess is a correctness harness, not a security sandbox. Do not run untrusted input on a sensitive host.
- **Private Data:** Input screenshots and generated ledgers may contain course material. Review before sharing.
- **Scope:** This pipeline is an automated QA harness and study reference tool for standard LMS practice modules. Generated code is sandbox-verified for correctness against standard computer science curricula.

---

### ENGINEERING NOTES

- **Why serial OCR:** LM Studio exposes one GPU-backed model; parallel vision calls just queue behind the GPU and inflate latency.
- **Why cloud for solving:** The 120B model produces syntactically valid Python far more often than the local 9B, and the sandbox catches the rest.
- **Why salvage exists:** Reasoning models occasionally truncate JSON mid-string; recovering `code` beats discarding a long generation.
- **Why triage instead of retry-forever:** A bounded repair budget prevents a single bad question from stalling the entire queue.
- **Why JSONL:** Append-only writes are atomic enough for this workload, human-readable, and trivially resumable.
- **Why two offline passes:** Re-verification recovers items after a harness fix; deterministic arbitration resolves verifiable MCQ conflicts without spending another LLM call.
- **Why one verifier:** `Stage2.py` and `reverify.py` cannot drift from the production comparison rules because they import the same implementation.

---

<div align="center">
  <code>BUILT FOR A SAFER DEADLINE.</code>
</div>
