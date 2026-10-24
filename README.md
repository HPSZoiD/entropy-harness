<div align="center">
  <h1>C0deT4ntr4 // AUTOMATED QA PIPELINE</h1>
  <p><code>HPS LABS // INTERNAL REFERENCE TOOL</code></p>
  <p>A local-first, multi-tier AI swarm for sandbox-verified code generation and consensus-graded MCQs.</p>

  <p>
    <code>[ LOCAL-FIRST ]</code>
    <code>[ SANDBOX-VERIFIED ]</code>
    <code>[ ZERO-DB ]</code>
    <code>[ 47/47 PASS ]</code>
  </p>
</div>

> **Doctrine:** the model proposes, the sandbox disposes. Nothing is accepted because it "looks right" — it is accepted because it executes and matches the transcript.

---

### 📊 PRODUCTION TELEMETRY
*Execution log from a 24-question unit backlog. Zero human intervention.*

```text
[  709.5s] ============================================================
[  709.5s] WALL 709.5s | extracted 24 | solved 24 | unresolved 0
[  709.5s] {"extract_ok": 13, "extract_fail": 0, "skip_other": 0, "groq_calls": 24, "e429": 0,
           "tests_run": 47, "tests_pass": 47, "code_pass": 22, "code_triage": 0,
           "repaired": 0, "repair_fail": 0, "mcq_prop": 2, "mcq_conf": 2, "mcq_conflict": 0}
```

| Metric | Result |
| :--- | ---: |
| Wall clock | `709.5 s` |
| Unique questions | `24 / 24` |
| Sandbox tests | `47 / 47` passed |
| Code solved first pass | `22 / 22` |
| Repairs required | `0` |
| MCQ consensus | `2 / 2` confirmed |
| Triage backlog | `0` |
| HTTP 429 events | `0` |

**24 unique questions. 47/47 sandbox tests passed on first execution. Zero triage. Zero rate-limit errors.**

Wall time is strictly bound by local OCR throughput (~20s/image). The cloud swarm finishes long before the local queue drains — the bottleneck is the GPU, not the network.

---

### 🏗️ SYSTEM ARCHITECTURE

Four tiers. One producer, one fan-out, one verifier, one judge. No database, no message broker, no orchestration framework.

```text
                    ┌──────────────────────────────────────┐
                    │  questions/*.jpg  (local screenshots)│
                    └───────────────────┬──────────────────┘
                                        │
              ┌─────────────────────────▼─────────────────────────┐
              │  TIER 1 // OPTICAL                                │
              │  Local VLM · LM Studio · OpenAI-compatible API    │
              │  pixel budget · temp 0.0 · JSON-only prompt       │
              │  emit -> extractions.jsonl                        │
              └─────────────────────────┬─────────────────────────┘
                                        │
                             task queue │  (bounded by OCR speed)
                                        │
              ┌─────────────────────────▼─────────────────────────┐
              │  TIER 2 // COMPUTE SWARM                          │
              │  Groq 120B · one worker per key · 20s/key pacing  │
              │  skeleton detection · JSON salvage · testgen      │
              │  emit -> solutions.jsonl / triage.jsonl           │
              └───────┬───────────────────────────────┬───────────┘
                      │ code                          │ mcq
        ┌─────────────▼─────────────┐    ┌────────────▼────────────┐
        │  TIER 3 // QA SANDBOX     │    │  TIER 4 // CONSENSUS    │
        │  local subprocess · 5s    │    │  local judge · blind    │
        │  strict / echo /          │    │  confirm | conflict     │
        │  transcript comparison    │    │                         │
        └─────────────┬─────────────┘    └────────────┬────────────┘
                      │                               │
             pass ────┤                               ├──── confirmed
             fail ────┤                               └──── conflict
                      ▼
              ┌───────────────┐
              │  REPAIR POOL  │  3 attempts · failing diff replayed
              └───────┬───────┘
                      │
                      ▼
              run_report.json  ──►  build_site_data.py  ──►  site_data.json
```

**Worker topology**

- `1x` local extraction thread — serial by design; the vision model owns the GPU.
- `Nx` Groq workers — one per API key; they share a single task queue.
- `1x` local judge — runs after extraction so vision and grading never contend for VRAM.
- `1x` repair pool — re-runs failed code tasks after the solve queue drains.

**Design constraints**

- `temperature = 0.0` everywhere. Deterministic output is a requirement, not a preference.
- No retry storms: a failed attempt becomes ledger state, not a loop.
- No DB: append-only JSONL is the source of truth and the resume cursor.
- No screenshots leave the machine; only extracted text goes to the cloud.

---

### 🔁 EXECUTION WORKFLOW

**Phase 0 — Boot & Resume**

1. Load `Stage1`, `groq-Stage2`, and `judge_mcqs` via `importlib`.
2. Read existing ledgers and reconstruct state: extracted files, solved QIDs, triage items.
3. Skip anything already solved; re-queue pending code/MCQ records.
4. Abort early if no `GROQ_API_KEYS` are present.

**Phase 1 — Optical Extraction (serial)**

1. Hash each image (MD5) and reject byte-identical duplicates.
2. Downscale to the pixel budget, JPEG-encode, base64 data-URI.
3. Call the local VLM with a JSON-only system prompt.
4. Parse content, then reasoning; one "nudge" retry if both fail.
5. Normalize defaults and reclassify label-lists as MCQs.
6. Append the record to `extractions.jsonl`; failures also hit `failed_extractions.jsonl`.
7. Push `code` / `mcq` records into the shared task queue.

**Phase 2 — Solve Fan-Out (parallel)**

1. A Groq worker claims a record from the queue.
2. Code: detect `fragment` vs `full` mode, generate code + tests.
3. MCQ: pick one option verbatim; normalize and match against the option list.
4. Salvage truncated JSON if needed; generate missing tests with a second call.
5. Pace each key by `KEY_SLEEP = 20s`; back off 60s on HTTP 429.

**Phase 3 — Sandbox Verification (local)**

1. Splice the fragment into the starter code at the placeholder.
2. Execute in a temporary `.py` file with a 5-second timeout.
3. Compare stdout against expected using strict, echo-aware, or transcript-aware logic.
4. Pass -> `solutions.jsonl`; fail -> `triage.jsonl`.
5. In fragment mode, if the spliced run fails, the raw fragment is also executed as a full program.

**Phase 4 — MCQ Consensus (local)**

1. Proposals are queued only after their answer matched an option.
2. The local judge returns `agree` plus its own `correct_option`.
3. Agree -> `solutions.jsonl` (`status: "confirmed"`).
4. Disagree -> `triage.jsonl` (`status: "conflict"`).

**Phase 5 — Repair Pool**

1. Collect triage items whose file has no solution.
2. Replay the failing test diffs to the model, up to 3 attempts.
3. First passing attempt is written to `solutions.jsonl` with `"repaired": true`.
4. Exhausted attempts stay in triage and are counted in the report.

**Phase 6 — Report & Reference Build**

1. Write `run_report.json` with wall time, counters, solved count, unresolved list.
2. Optionally run `build_site_data.py` to merge ledgers into `site_data.json`.

---

### 🧪 VERIFICATION ENGINE

The sandbox is the only authority. Model confidence is irrelevant.

| Compare mode | Trigger | Behavior |
| :--- | :--- | :--- |
| `strict` | exact stdout lines | Character-for-character match. |
| `echo` | prompt prefixes in sample output | Tolerates `input("prompt: ")` echoes that the platform injects. |
| `transcript` | sample written as a console session | Strips the typed stdin value and merges the prompt with the next line. |
| `fragment fallback` | spliced fragment fails | Re-runs the raw fragment as a standalone program. |

Additional guarantees:

- Each test runs in a fresh temporary file, deleted in a `finally` block.
- Non-zero exit codes never pass, regardless of stdout.
- Stderr is captured (last 400 chars) and fed back to the repair prompt.
- Hidden-test discipline: prompts forbid hardcoding sample inputs.
- Tests are derived from visible samples; if absent, they are generated from the Input/Output Format bullets.

---

### 🧬 DUPLICATE IMMUNITY (4 GATES)

Double-solving is treated as queue poisoning, not a minor inefficiency.

| Gate | Scope | Mechanism |
| :--- | :--- | :--- |
| **1. Content hash** | Image bytes | MD5 of the file; identical images are skipped before OCR. |
| **2. QID registry** | Semantic identity | A seen-`qid` map rejects repeats and records `duplicate_of`. |
| **3. Ledger resume** | Persisted state | Files already extracted or solved are skipped on restart. |
| **4. Atomic write guard** | Concurrent workers | A write lock plus an existence check on `qid` prevents duplicate ledger rows. |

A duplicate that slips past the hash gate is stopped by the QID gate. A duplicate that races two workers is stopped by the write guard.

---

### 🛡️ RESILIENCE MATRIX

| Failure | Handler | Destination |
| :--- | :--- | :--- |
| Malformed model JSON | parse content -> parse reasoning -> one-shot nudge | retry / failure ledger |
| Truncated JSON | regex salvage of `code` and `tests` | candidate retained |
| Missing tests | dedicated test-generation call | same task |
| Code fails sandbox | repair pool with failing diffs (3 attempts) | solutions or triage |
| HTTP 429 | 60-second backoff | same task |
| Rate pressure | fixed 20-second per-key pacing | same task |
| Duplicate image / QID | gates 1-3 | skipped + counted |
| OCR failure | recorded with raw output | `failed_extractions.jsonl` |
| Unparseable MCQ | no option match | `triage.jsonl` |
| Critical error | exception isolation per worker | run continues |

---

### ⚙️ CORE CAPABILITIES

- **Zero-Shot Skeleton Detection:** Auto-detects `match/case`, `if/elif`, and fragment vs. full-program modes from the locked starter code.
- **Echo-Aware Sandbox:** Tolerates platform-specific `input("prompt: ")` echoes without weakening the strict diff.
- **Fire-and-Triage Routing:** Most items pass in one shot; edge cases are routed to a feedback-loop repair pool with failing test diffs.
- **Static Reference Node:** `build_site_data.py` compiles all ledgers into a single `site_data.json` snapshot sized for a zero-backend dashboard and instant client-side search.
- **4-Gate Duplicate Immunity:** Content hashes, QID semantics, resume cursors, and atomic ledger guards prevent double-solving and queue poisoning.
- **Fully Resumable:** Kill the process at any point; the next run reconstructs state from the ledgers and continues.
- **No Infrastructure:** No database, no queue service, no scheduler, no `.env` loader, no telemetry.

---

### 📂 LEDGER CONTRACTS

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
| `pass` | Code executed and matched all generated tests. |
| `confirmed` | MCQ answer survived local consensus. |
| `conflict` | Judge disagreed with the cloud answer. |
| `fail` | Code failed sandbox verification. |
| `unresolved` | Exhausted retries; retained for manual review. |

Full field-level schemas live in [`ARCHITECTURE.md`](./ARCHITECTURE.md).

---

### 🚀 DEPLOYMENT

**Prerequisites**

- Python 3.10+
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

# 4. Run the orchestrator
python main.py --reset

# 5. Build the reference node data (optional)
python build_site_data.py
```

**Standalone stages** — each resume from its own ledger:

```bash
python Stage1.py        # OCR only
python groq-Stage2.py   # cloud solve only
python judge_mcqs.py    # local MCQ consensus only
python Stage2.py        # local-only solver with retries
```

**Resume semantics**

- Without `--reset`, the orchestrator is fully resumable.
- Extracted and solved files are skipped.
- Pending extraction records are re-seeded into the solve queue.
- `--reset` wipes every ledger and starts from zero.

**Rate-limit math**

- Groq free-tier budget is treated as ~8k TPM.
- A request is estimated at ~2.5k tokens.
- That is ~3 requests/minute/key.
- `KEY_SLEEP = 20` keeps each key at ~3 requests/minute — no 429s in the reference run.

---

### 📁 REPOSITORY LAYOUT

```text
repo/
├── main.py                 orchestrator and worker lifecycle
├── Stage1.py               local VLM extraction + normalization
├── groq-Stage2.py          Groq solve, salvage, testgen, sandbox compare
├── judge_mcqs.py           local blind MCQ consensus
├── Stage2.py               standalone local-only solver
├── build_site_data.py      ledger merge -> site_data.json
├── ARCHITECTURE.md         detailed tier + schema documentation
└── README.md               this document
```

Runtime artifacts (`questions/`, `*.jsonl`, `run_report.json`, `site_data.json`) are generated locally and excluded from the repository.

---

### 🛡️ OPERATIONAL SECURITY & DATA PRIVACY

- **Local-First Vision:** Image processing never leaves the local GPU. No screenshots are uploaded to cloud APIs.
- **Text-Only Cloud Burst:** Only extracted text payloads (JSON) are sent to the Groq swarm.
- **No Persistent State:** Ledgers are local JSONL files. No external logging, no analytics, no tracking.
- **No Secrets In Tree:** Keys are runtime-only via `GROQ_API_KEYS`; there is no `.env` file and no committed credential.
- **Sandbox Scope:** The 5-second subprocess is a correctness harness, not a security sandbox. Do not run untrusted input on a sensitive host.
- **Private Data:** Input screenshots and generated ledgers may contain course material. Review before sharing.
- **Scope:** This pipeline is an automated QA harness and study reference tool for standard LMS practice modules. Generated code is sandbox-verified for correctness against standard computer science curricula.

---

### 🧠 ENGINEERING NOTES

- **Why serial OCR:** LM Studio exposes one GPU-backed model; parallel vision calls just queue behind the GPU and inflate latency.
- **Why cloud for solving:** The 120B model produces syntactically valid Python far more often than the local 9B, and the sandbox catches the rest.
- **Why salvage exists:** Reasoning models occasionally truncate JSON mid-string; recovering `code` beats discarding a 40-second generation.
- **Why triage instead of retry-forever:** A bounded repair budget prevents a single bad question from stalling the entire queue.
- **Why JSONL:** Append-only writes are atomic enough for this workload, human-readable, and trivially resumable.

---
<div align="center">
  <code>BUILT FOR A SAFER DEADLINE.</code>
</div>
