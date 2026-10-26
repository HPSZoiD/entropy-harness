# CodePractice Pipeline Architecture

Flat, file-based pipeline for extracting CodePractice questions from phone photos, solving them with remote/local LLMs, verifying code against reconstructed console tests, judging MCQs, and repairing or re-verifying failures. There is no package, database, or service layer: every stage is a script plus append-only JSONL ledgers.

---

## 1. High-level flow

```
questions/*.jpg
      |
      v
 [Stage1.py]  OCR via LM Studio VLM
      |
      +--> extractions.jsonl  (ok records)
      +--> failed_extractions.jsonl
      |
      v
 [main.py]  conductor
      |
      +---------------------------+
      |                           |
      v                           v
 [groq-Stage2.py]            [judge_mcqs.py]
  Groq solve + verify         local LM Studio judge
      |                           |
      +--> solutions.jsonl <------+
      +--> triage.jsonl
      +--> mcq_proposals.jsonl
      |
      v
 [repair pool in main.py]  retries unresolved code with feedback
      |
      v
 [reverify.py]   targeted re-run of old triage artifacts on fixed harness
 [mcq_arbiter.py] deterministic interpreter arbitration of MCQ conflicts
      |
      v
 [build_site_data.py] -> site_data.json -> index.html
```

Two verification paths share one contract:

- **Live path:** `main.py` -> `groq-Stage2.verify`
- **Standalone local solver:** `Stage2.py` -> delegates to `groq-Stage2.verify`
- **Forensic re-check:** `reverify.py` -> `groq-Stage2.verify`

There is exactly one implementation of verification. `Stage2.py` intentionally contains no private copy.

---

## 2. File map

| File | Role |
|---|---|
| `Stage1.py` | Photo -> structured JSON. VLM extraction prompt, normalization, option dedupe, ledger writes. |
| `groq-Stage2.py` | Primary solver. Groq API keys, prompts, mode detection, splicing, sandbox execution, test comparison, hint handling, referee prompt. |
| `main.py` | Production orchestrator. Extraction thread, parallel Groq workers, serialized local judge drain, repair pool, stats/report. |
| `judge_mcqs.py` | Standalone local-MCQ judge for `mcq_proposals.jsonl`; mirrors main.py sentinel logic. |
| `Stage2.py` | Standalone local/LM Studio solver. Loads `groq-Stage2.py` via importlib and delegates `verify` to it. |
| `reverify.py` | Re-runs stored triage code artifacts against the fixed harness. One Groq referee call per still-failing item. |
| `mcq_arbiter.py` | Deterministic interpreter arbitration for MCQ conflicts. No LLM calls. |
| `build_site_data.py` | Merges extractions + solutions into `site_data.json` for `index.html`. |
| `index.html` | Static reference site front end. |
| `run_report.json` | Last `main.py` run summary. |
| `questions/` | Source photos. Never modified by stages. |
| `U2/` | Additional images/archive material. |

Hyphenated module names (`groq-Stage2.py`) are loaded with `importlib.util.spec_from_file_location`, not normal imports.

---

## 3. Stage 1 — extraction

`Stage1.py` sends each image to a local vision model through LM Studio (`http://localhost:1234/v1`).

Output schema:

```json
{
  "file": "IMG_....jpg",
  "seconds": 0.0,
  "ok": true,
  "data": {
    "qid": "33.1.2",
    "title": "...",
    "type": "code|mcq|other",
    "left_panel": "...",
    "options": ["..."],
    "filename": "DictTuple.py",
    "starter_code": "...",
    "unclear": ["..."]
  }
}
```

Key behaviors:

- Image preprocessing resizes to a pixel budget and encodes JPEG.
- JSON is salvaged from either `content` or the model's reasoning channel.
- One retry with a "zero deliberation" nudge.
- `normalize()` fills defaults, promotes label lists to `mcq`, and **dedupes options order-preservingly with exact-string comparison**.
- Successful records go to `extractions.jsonl`; failures go to `failed_extractions.jsonl`.

OCR is intentionally verbatim. It does not fix platform bugs or authoring mistakes.

---

## 4. Stage 2 — solving and verification

### 4.1 Mode detection

`detect_mode(starter)`:

- `fragment` when there is exactly one placeholder comment line and no skeleton markers.
- `full` otherwise (multiple placeholders, `match`/`case`, or no starter).

`splice(starter, code)` replaces the placeholder comment run with returned code and preserves indentation context.

### 4.2 Verification contract

`verify(starter, code, tests, mode, panel="")` is the single choke point.

**Empty-test guard (F2):**

```python
if not tests:
    return False, [{"stdin": "", "expected": "", "got": "",
                    "stderr": "no tests derived", "ok": False}]
```

No caller can ever get a vacuous pass from `all([]) == True`.

**Candidate variants:**

| Variant | When built | Purpose |
|---|---|---|
| `full` | mode is full | Candidate is the complete program. |
| `spliced` | mode is fragment | Candidate inserted at the first placeholder comment. |
| `hint-stripped` | non-full | Starter hint lines removed before splicing. |
| `hint-residue` | non-full and residue remains | Broken hints still present after splicing are removed from the merged source. |
| `standalone` | non-full and candidate differs | Legacy fallback: run the candidate alone. |

Each variant is compiled before execution. A `SyntaxError` fails fast with the compiler text in `stderr`; the 5-second sandbox timeout is not burned.

The first variant that compiles and passes every test wins. Each result records its `variant` and `compare` mode.

### 4.3 Compare ladder

Both actual and expected output are CRLF-normalized and right-stripped before comparison.

1. **`strict`** — right-stripped line lists are equal.
2. **`echo`** — same line count; differing lines are explainable as an echoed prompt prefix present in the left panel.
3. **`transcript`** — `canon_lines(actual, stdin) == canon_lines(expected, stdin)`.
4. **`loose`** — left-stripped line lists are equal. Leading-whitespace tolerance only; flagged via `"loose": true`.
5. Otherwise `None` — fail.

`canon_lines`:

- Drops pure echoed-input lines.
- Strips stdin values from the end of prompt lines (`"num: 12"` -> `"num: "`).
- Glues runs of bare prompt lines to the next output line.
- Normalizes `:` spacing and right-strips.

The operation is symmetric on actual and expected output, so it cannot turn a mismatch into a pass by only normalizing one side.

### 4.4 Hint-line policy (F1)

```python
HINT_RE = re.compile(r"^\s*\w+\s*=\s*\{[^}]*\bfor\b[^}]*\}\s*$")

def _line_compiles(l):
    try:
        compile(l, "<line>", "exec"); return True
    except SyntaxError:
        return False

def is_hint_line(l):
    return bool(HINT_RE.match(l)) and not _line_compiles(l)
```

`HINT_RE` alone is too broad: it matches valid comprehensions such as `{num: num**3 for ...}`. The compile gate makes the predicate universal:

- `{num, for }` -> does not compile -> hint, safe to strip.
- `{num: num**3 for num in range(...)}` -> compiles -> real code, untouchable.

`is_hint_line` is used for starter stripping, post-merge residue detection, and the `reverify.py` starter gate.

### 4.5 Referee

`groq-Stage2.REFEREE` is used only by `reverify.py`, only after every local mode fails. It asks the model to decide whether code or tests are wrong and return corrected tests or corrected code. Budget: at most one referee call per failing item.

---

## 5. Stage 3 — MCQ judging and arbitration

### 5.1 Local judge

`main.py` and `judge_mcqs.py` ask a local LM Studio model to return:

```json
{"agree": true, "correct_options": ["..."]}
```

Confirmed answers go to `solutions.jsonl`; disagreements go to `triage.jsonl` as `status: "conflict"`.

### 5.2 Sentinel handling

```python
SENTINELS = {"unknown", "n/a", "null", "none", ""}
```

Sentinels mean "the judge/proposer had nothing", not "the sides disagree".

- Judge side sentinel + proposer real -> accept proposer, `judge_missing: true`.
- Proposer side sentinel + judge real -> accept judge, `proposer_missing: true`.
- Both sentinel -> do not confirm; fall through to conflict.

Both `main.py` and `judge_mcqs.py` implement this symmetrically.

### 5.3 Interpreter arbiter

`mcq_arbiter.py` is deterministic and LLM-free.

- Handles sentinels first.
- Expands multiline option blobs into individual option lines.
- Parses falsifiable claims such as:
  - `print(expr) returns X`
  - `assignment, print(expr) returns X`
  - `expr is X`
- Evaluates claims in a subprocess with a 5-second timeout and `ast.literal_eval` comparison.
- Computes `truth` and `falsified` sets.
- Accepts a side only if it contains every falsifiable truth and no falsified option.
- If both sides qualify, prefers the smaller set.
- Anything unfalsifiable or split stays for human review.

Accepted arbitrations are appended to `solutions.jsonl` with `"arbiter": "interpreter"`.

---

## 6. Orchestration and repair

`main.py` phases:

1. **Extraction:** strictly serial LM Studio calls; duplicate images and duplicate QIDs rejected.
2. **Groq solve:** N workers, one per API key, pulling from a shared queue.
3. **Judge drain:** after OCR completes, so LM Studio is never double-tasked.
4. **Repair pool:** unresolved code items retried up to `REPAIR_ATTEMPTS` with failure feedback.
5. **Report:** writes `run_report.json` and prints unresolved entries.

Additional invariants:

- `append_json` guards `solutions.jsonl` and `mcq_proposals.jsonl` against duplicate QIDs under a write lock.
- `groq_worker` checks `G2.starter_compiles(starter)` before building the code payload. Broken starters are logged and the payload receives `"starter_broken": true`.
- Groq calls retry on 429 with a 60-second backoff.

---

## 7. Targeted re-verification

`reverify.py` re-checks code artifacts already sitting in `triage.jsonl` without re-extracting or re-running the whole queue.

Important implementation details:

- Tests come from `triage.tests` when present; otherwise they are reconstructed from `triage.results` (`stdin`/`expected`). Older main.py triage records store results only.
- The starter compile gate applies only to non-full modes, because full-mode code replaces the starter entirely.
- A non-full starter that fails to compile but contains a real broken hint is allowed through to the hint-stripped variant.
- A starter that fails to compile with no hint is sent to human review (platform bug or OCR scar).
- Still-failing items get exactly one referee call.
- Passing items are appended to `solutions.jsonl` with provenance flags such as `harness_fix`, `tests_corrected`, or `referee_code`.

---

## 8. Data contracts

### `extractions.jsonl`

```json
{"file": str, "seconds": float, "ok": bool, "data": { ...extraction schema... }}
```

### `solutions.jsonl`

Code pass:

```json
{"file": str, "qid": str, "type": "code", "status": "pass",
 "code": str, "tests": [{"stdin": str, "stdout": str}], "filename": str}
```

MCQ confirmation:

```json
{"file": str, "qid": str, "type": "mcq", "answer": [str],
 "status": "confirmed"}
```

Optional provenance: `repaired`, `harness_fix`, `modes`, `tests_corrected`, `referee_code`, `judge_missing`, `proposer_missing`, `arbiter`.

### `triage.jsonl`

Code failure:

```json
{"file": str, "qid": str, "type": "code", "status": "fail",
 "starter_code": str, "left_panel": str, "code": str,
 "results": [{"stdin": str, "expected": str, "got": str,
              "stderr": str, "ok": bool, "compare": str|null,
              "loose": bool, "variant": str}],
 "raw": str}
```

MCQ conflict:

```json
{"file": str, "qid": str, "type": "mcq", "status": "conflict",
 "groq_answer": [str], "ornith_guess": [str], "options": [str]}
```

### `mcq_proposals.jsonl`

```json
{"file": str, "qid": str, "answer": [str],
 "left_panel": str, "options": [str], "why": str}
```

### `failed_extractions.jsonl`

Same extraction record shape with `ok: false`.

All ledgers are append-only. Do not rewrite historical lines.

---

## 9. Runbook

```powershell
# Full production run (resumes; never wipes ledgers)
python main.py

# Full production run from scratch (destructive; only when intended)
python main.py --reset

# Re-check old triage code artifacts on the fixed harness
python reverify.py

# Arbitrate MCQ conflicts deterministically
python mcq_arbiter.py

# Regenerate the static site data
python build_site_data.py

# Syntax gate for every pipeline file
python -m py_compile main.py Stage1.py Stage2.py groq-Stage2.py judge_mcqs.py reverify.py mcq_arbiter.py
```

External dependencies:

- **LM Studio** at `http://localhost:1234/v1` for OCR and local judging.
- **Groq** at `https://api.groq.com/openai/v1` for solving and refereeing.
- Python 3.14 with `openai` and `pillow`.

`reverify.py` reads `GROQ_API_KEYS` first and falls back to `G2.KEYS[0]`.

---

## 10. Design decisions worth keeping

1. **One verifier.** `groq-Stage2.verify` is the only comparison engine. `Stage2.py` delegates to it.
2. **Compile before execute.** Syntax errors fail instantly and visibly.
3. **Hints are compile-gated.** Broken placeholders are stripped; valid comprehensions are never touched.
4. **No vacuous passes.** Empty tests always fail.
5. **Append-only ledgers.** Corrections are new solution lines, never rewritten history.
6. **Sentinels are missing data, not disagreement.** Judge and proposer are treated symmetrically.
7. **Deterministic before generative.** The interpreter arbiter runs before any human review; the referee is last resort.
8. **One referee call per item.** No retry loops against the Groq budget.
9. **Transcript comparison is symmetric.** Both actual and expected output go through the same canonicalization.
10. **The standalone local solver cannot drift.** It imports the primary verification module.

---

## 11. Current snapshot

As of the last targeted run:

- `extractions.jsonl`: 55 records.
- `solutions.jsonl`: 51 records.
- `triage.jsonl`: 24 records (includes a duplicate 26.2.5 conflict line from a re-solve attempt).
- Unresolved set: `{26.2.5}`.
- 33.1.2 is solved by the repair pool even though the platform starter is genuinely broken (missing comma after the Gilliam tuple in the source image). The repaired solution is a corrected standalone whole program; the starter itself was not patched.
- `IMG_20261025_214806.jpg` is absent from `extractions.jsonl` and failed extraction again during the last run. `failed_extractions.jsonl` contains that failure. It is outside the targeted unresolved set.

---

## 12. Known rough edges

- **Canon suffix stripping** is riskiest on single-character stdin values such as `3`. The transform is symmetric, so it should not create false passes by itself, but a transcript diff on an index-digit question is the first place to inspect.
- **Duplicate triage lines** can appear after a full re-solve attempt (for example 26.2.5). The unresolved set is semantically correct, but the report can list the same QID twice.
- **Failed extractions are retried** by `main.py` on every run because the conductor does not consult `failed_extractions.jsonl` before attempting missing images.
- **Platform-broken starters** can still produce a correct solution through the standalone variant, but the learner may need to paste the whole program rather than only the missing fragment.
