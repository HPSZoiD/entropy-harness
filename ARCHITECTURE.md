# codepractice Pipeline — Architecture

A local-first pipeline that turns phone photos of codepractice pages into solved,
sandbox-verified answers. It combines a local vision model (LM Studio) for OCR
and judging with a hosted Groq model for fast solving, then verifies every code
answer by executing it before accepting it.

The presentation layer (`index.html`) is intentionally not part of this
repository. This document describes the data pipeline only.

---

## 1. High-level flow

```
questions/*.jpg
      |
      v
[Stage 1] Local VLM OCR  (LM Studio)
      |  extractions.jsonl
      v
  task queue
      |
      v
[Stage 2] Groq solve  (parallel workers, one per API key)
      |         |
      |         +--> code: local sandbox execution + stdout verification
      |         +--> mcq : mcq_proposals.jsonl
      v
 solutions.jsonl            triage.jsonl
      ^                          |
      |                          v
[Stage 3] Local judge      [Repair pool] Groq retry loop
      |                          |
      +--------------------------+
      v
 run_report.json  -->  build_site_data.py  -->  site_data.json
```

Two runtime roles are used:

- **Local model** (LM Studio, OpenAI-compatible API): image OCR, MCQ judging,
  and optional standalone solving. It must run serially because it is the GPU
  bottleneck.
- **Groq model** (hosted, OpenAI-compatible API): high-throughput code/MCQ
  solving and test generation. Parallelized across multiple API keys.

---

## 2. Components

| File | Role |
| --- | --- |
| `main.py` | Production orchestrator. Loads the stage modules, starts workers, manages queues, retries, and writes the run report. |
| `Stage1.py` | Extraction stage. Encodes each image, calls the local vision model, parses and normalizes the JSON result. Can also run standalone. |
| `groq-Stage2.py` | Groq solving stage. Prompts, JSON salvage, mode detection, sandbox execution, stdout comparison, and the standalone batch runner. |
| `judge_mcqs.py` | Local MCQ verifier. Reads proposals, asks the local model to confirm or reject, routes to solutions or triage. Can run standalone. |
| `Stage2.py` | Standalone local-only solver (LM Studio). Same prompts/verification as the Groq stage but serial, with retries. Not used by `main.py`. |
| `build_site_data.py` | Merges extraction and solution ledgers into `site_data.json` for a reference site. |

`main.py` imports the other three modules dynamically with
`importlib`, so file names are resolved relative to the script directory and the
pipeline can be launched from anywhere.

---

## 3. Stage 1 — Extraction (`Stage1.py`)

Input is one image at a time from `questions/` (`.png`, `.jpg`, `.jpeg`,
`.webp`, `.bmp`). Create this directory and add your own screenshots before
running; no images are included in the repository.

1. **Preprocessing** — the image is converted to RGB and downscaled to fit a
   pixel budget (`PIXEL_BUDGET = 1_500_000`) to keep vision-token usage low, then
   JPEG-encoded and base64-encoded as a data URI.
2. **Prompt** — the model is instructed to return a single JSON object and
   nothing else. The schema is:

   ```json
   {
     "qid": "16.1.2",
     "title": "Understanding 'if construct",
     "type": "code | mcq | other",
     "left_panel": "all visible text, verbatim",
     "options": ["..."],
     "filename": "Ifcon01.py",
     "starter_code": "editor contents, verbatim",
     "unclear": ["..."]
   }
   ```

3. **Salvage** — the reply is parsed with `try_parse()`, which strips markdown
   fences and extracts the outermost `{...}`. If the content channel is empty or
   malformed, the reasoning channel is tried. If both fail, one "nudge" retry is
   made that demands the JSON on the first token.
4. **Normalization** — missing fields get defaults. If options exist but the
   type is not `code`, the record is reclassified as `mcq`.
5. **Deduplication** — two guards exist in the orchestrator: an MD5 hash of the
   image bytes rejects identical files, and a seen-`qid` set rejects repeated
   question IDs. Records whose `qid` is already in `solutions.jsonl` are skipped
   entirely.
6. **Ledger** — every attempt is appended to `extractions.jsonl`; failures are
   also appended to `failed_extractions.jsonl`.

Only `code` and `mcq` records enter the solving queue; `other` records are
counted and skipped.

---

## 4. Stage 2 — Solving (`groq-Stage2.py`)

### Code path

1. `detect_mode()` inspects the starter code. A single placeholder comment with
   no control-flow skeleton means `fragment` (return only the replacement lines);
   otherwise `full` (return the complete program).
2. The solver is told to preserve locked lines, respect indentation context, and
   match sample stdout exactly, including input-prompt echoes.
3. The reply is parsed with `try_parse()` and, if the JSON was truncated,
   `salvage()` recovers the `code` and `tests` fields with regex + raw JSON
   decoding.
4. If the model did not produce tests, a second `TESTGEN` call derives them from
   the problem statement and console samples.
5. The candidate is executed in a temporary file with a 5-second timeout. Each
   test compares captured stdout against expected stdout. Three comparison modes
   are supported:
   - `strict` — exact line match,
   - `echo` — the program echoed input-prompt prefixes that the sample includes,
   - `transcript` — the sample was written as an interactive console transcript,
     so the typed stdin value is stripped and the prompt merged with the next
     line.
6. For `fragment` mode, the code is first spliced into the starter at the
   placeholder; if that fails, the raw fragment is also tried as a full program.
7. Pass -> `solutions.jsonl`. Fail -> `triage.jsonl`.

### MCQ path

The model must return `{"answer": "...", "why": "..."}`. The answer is matched
against the options with normalized exact match first, then substring matching.
A valid pick becomes an entry in `mcq_proposals.jsonl` and is enqueued for the
local judge. No valid pick goes straight to triage.

---

## 5. Stage 3 — MCQ judging (`judge_mcqs.py`)

The local model receives the question context, options, and the Groq answer and
must return:

```json
{ "agree": true, "correct_option": "exact option text" }
```

- `agree: true` -> written to `solutions.jsonl` with `status: "confirmed"`.
- Otherwise -> written to `triage.jsonl` as a conflict, with the judge's guess.

In `main.py`, judging runs on the same local model after extraction completes,
so vision and text workloads never contend for the GPU.

---

## 6. Orchesteration and concurrency (`main.py`)

`main.py` is a conductor only; stage logic stays in the stage modules.

- One local thread handles extraction, then drains the judge queue.
- One Groq thread per API key consumes the shared task queue.
- `KEY_SLEEP = 20` seconds is enforced between calls per key to respect the
  token-per-minute limit.
- HTTP 429 / rate-limit errors trigger a 60-second backoff and retry inside
  `groq_call()`.
- After the solve queue drains, a repair phase re-runs failed code tasks up to
  `REPAIR_ATTEMPTS = 3` times, feeding the failing test output back to the model.
- Writes to ledgers are serialized by a lock. `solutions.jsonl` and
  `mcq_proposals.jsonl` also reject duplicate `qid` writes.
- The run ends by writing `run_report.json` with wall time, counters, extracted
  count, unique solved count, and unresolved items.

Unresolved items are never dropped silently; they remain in `triage.jsonl`.

---

## 7. Data contracts

All ledgers are JSON Lines, append-only, UTF-8, one object per line. They are
created at runtime and are not included in the repository.

### `extractions.jsonl`

```json
{
  "file": "IMG_20261024_225038.jpg",
  "seconds": 21.8,
  "ok": true,
  "data": {
    "qid": "16.1.2",
    "title": "Understanding 'if construct",
    "type": "code",
    "left_panel": "...",
    "options": [],
    "filename": "Ifcon01.py",
    "starter_code": "num = int(input(\"num: \"))\n#write your logic here",
    "unclear": []
  }
}
```

Standalone `Stage1.py` also records `size`, `img_tokens`, and
`reasoning_chars`; failed records add `raw`.

### `solutions.jsonl`

Code solution:

```json
{
  "file": "IMG_20261024_225038.jpg",
  "qid": "16.1.2",
  "type": "code",
  "status": "pass",
  "code": "if num % 3 == 0:\n    print(\"divisible by 3\")\nprint(\"End of program\")",
  "tests": [{ "stdin": "66\n", "stdout": "num: divisible by 3\nEnd of program\n" }],
  "filename": "Ifcon01.py"
}
```

MCQ solution:

```json
{
  "file": "IMG_20261024_225022.jpg",
  "qid": "16.1.1",
  "type": "mcq",
  "answer": "Control Flow Statements",
  "status": "confirmed",
  "why": "..."
}
```

Repaired entries add `"repaired": true`.

### `mcq_proposals.jsonl`

```json
{
  "file": "IMG_20261024_225022.jpg",
  "qid": "16.1.1",
  "answer": "Control Flow Statements",
  "left_panel": "...",
  "options": ["..."],
  "why": "..."
}
```

### `triage.jsonl`

Holds items that could not be auto-accepted: `code` failures with test results,
unparseable replies, and MCQ conflicts (`groq_answer` vs `ornith_guess`).

### `failed_extractions.jsonl`

Same shape as an extraction record, written when OCR parsing fails.

### `run_report.json`

```json
{
  "wall_seconds": 3.8,
  "stats": { "extract_ok": 0, "code_pass": 0, "mcq_conf": 0, "...": 0 },
  "extracted": 24,
  "solved_unique": 24,
  "unresolved": []
}
```

### `site_data.json`

Produced by `build_site_data.py`. It merges extraction records by `qid`
(concatenating continuation text, keeping the longest starter code), attaches
the matching solution, sorts by dotted question number, and writes:

```json
{
  "generated": "2026-10-25 03:12",
  "items": [
    {
      "qid": "16.1.1",
      "title": "...",
      "type": "mcq",
      "left_panel": "...",
      "options": ["..."],
      "starter_code": "",
      "filename": "",
      "solution": { "kind": "mcq", "answer": "...", "status": "confirmed" }
    }
  ]
}
```

Code solutions in `site_data.json` use `kind: "code"` with `code`, `tests`, and
`status`.

---

## 8. Configuration

Configuration is intentionally minimal. There is no `.env` file and no secrets
are stored in the repository.

### Orchestrator (`main.py` / `groq-Stage2.py`)

| Setting | Value / source |
| --- | --- |
| LM Studio endpoint | `http://localhost:1234/v1` |
| LM Studio model | first loaded model (or set `MODEL` in `Stage1.py`) |
| Groq endpoint | `https://api.groq.com/openai/v1` |
| Groq model | `openai/gpt-oss-120b` (`MODEL` in `groq-Stage2.py`) |
| Groq keys | `GROQ_API_KEYS` environment variable, comma-separated |
| Per-key call spacing | `KEY_SLEEP = 20` seconds |
| Repair attempts | `REPAIR_ATTEMPTS = 3` |
| Vision pixel budget | `PIXEL_BUDGET = 1_500_000` |

Set keys in the current shell only:

```powershell
$env:GROQ_API_KEYS = "<key1>,<key2>,<key3>"
```

No key has a default and the orchestrator exits with a message if none are
provided.

### Standalone local solver (`Stage2.py`)

| Setting | Environment variable | Default |
| --- | --- | --- |
| Endpoint | `SOLVER_BASE_URL` | `http://localhost:1234/v1` |
| Key | `SOLVER_API_KEY` | `lm-studio` |
| Model | `SOLVER_MODEL` | first loaded model |

---

## 9. Running

Dependencies: `openai`, `pillow`. A local LM Studio server must be running with
a vision-capable model loaded.

Full pipeline:

```powershell
pip install openai pillow
$env:GROQ_API_KEYS = "<key1>,<key2>,<key3>"
python main.py --reset      # wipe ledgers, then run
python build_site_data.py    # optional: build site_data.json
```

Without `--reset`, existing ledgers are resumed: already extracted or solved
files are skipped and pending extraction records are re-queued.

Standalone stages (each is resumable from its ledger):

```powershell
python Stage1.py             # OCR only -> extractions.jsonl
python groq-Stage2.py        # solve -> solutions.jsonl / triage.jsonl / proposals
python judge_mcqs.py         # verify MCQ proposals locally
python Stage2.py             # local-only solve with retries
```

---

## 10. Failure handling

- Malformed model output: parse content, then reasoning, then a one-shot nudge.
- Truncated JSON: regex salvage of the `code`/`tests` fields.
- Code mismatch: retry through the repair pool with failing-test feedback.
- Rate limits: 60-second backoff (orchestrator) or per-key pacing.
- Exhausted retries: item stays in `triage.jsonl` and is listed in the report.
- Duplicate images or question IDs: skipped and counted.
- Extraction failure: recorded twice (general ledger and failure ledger) with raw
  model output for diagnosis.

---

## 11. Repository layout

```
repo/
|-- main.py                 orchestrator
|-- Stage1.py               local OCR extraction
|-- groq-Stage2.py          Groq solving + sandbox verification
|-- judge_mcqs.py           local MCQ judge
|-- Stage2.py               standalone local solver
|-- build_site_data.py      ledger -> site_data.json
`-- ARCHITECTURE.md         this document
```

Runtime inputs and outputs (`questions/`, `*.jsonl`, `run_report.json`,
`site_data.json`) are created locally and are not part of the repository.

---

## 12. Security and privacy notes

- No API keys, tokens, or credentials are stored in this repository. Keys are
  supplied at runtime through `GROQ_API_KEYS`; the local model's `lm-studio` key
  is a placeholder required by the OpenAI client.
- No `.env` loader or `.env` file is used.
- Input screenshots and the generated ledgers/`site_data.json` may contain course
  material. Treat them as private data and review them before sharing.
- The sandbox runs generated Python with a 5-second timeout in a temporary file.
  It is a correctness check, not a security sandbox; do not run untrusted input
  on a sensitive machine.
