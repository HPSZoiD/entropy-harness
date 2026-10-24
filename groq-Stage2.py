"""
codepractice pipeline — STAGE 2: Groq Speed-Run (Fire-and-Triage Edition)
Parallel execution across API keys, no retries (failures go to triage.jsonl).
deps: pip install openai
"""
import json, os, re, subprocess, sys, tempfile, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI

# ---------------- config ----------------
LEDGER   = Path("extractions.jsonl")
SOLUTIONS= Path("solutions.jsonl")
TRIAGE   = Path("triage.jsonl")
PROPOSALS= Path("mcq_proposals.jsonl")   # MCQs land here; judge_mcqs.py confirms locally
BASE_URL = "https://api.groq.com/openai/v1"

# Groq API keys are read from the GROQ_API_KEYS environment variable as a
# comma-separated list. Example (PowerShell):
#   $env:GROQ_API_KEYS = "<key1>,<key2>,<key3>"
# Never commit real keys to the repository.
KEYS = [k.strip() for k in os.environ.get("GROQ_API_KEYS", "").split(",") if k.strip()]
# Replace with exact model id if different (e.g. llama-3.3-70b-versatile, deepseek-r1-distill-llama-70b)
MODEL = "openai/gpt-oss-120b"  

# Rate limit math: 8k TPM. Est 2500 tokens per request. 
# 8000 / 2500 = 3.2 req/min. Sleep 20s per key to be safe (3 req/min = 7500 TPM).
SLEEP_BETWEEN_CALLS = 20 

# ---------------- prompts ----------------
SOLVE_CODE = """You are completing a Python exercise on codepractice. Input JSON: left_panel, starter_code, filename, mode.
mode="fragment": return ONLY the code that replaces the placeholder comment line(s).
mode="full": return the COMPLETE program: every starter line verbatim in order, with each placeholder comment replaced by your code.
Hard rules:
1. Never modify locked lines.
2. Indentation context: code inserted inside a block must be indented into it.
3. STDOUT EXACTNESS: output must match samples character-for-character. If a sample's stdout contains a line "<label>: <value>" where <value> is that sample's stdin, the program must emit that echo itself - normally by reading with input("<label>: ") - unless a locked line already does. Consoles do NOT echo input for free. When writing "tests", expected stdout is what a PIPED run prints: include the input() prompt text (e.g. "num: ") but NOT the typed value.
4. Generic logic; hidden tests exist.
5. Read input only if not already read.
6. If samples are missing, derive tests from Input/Output Format bullets.
Thinking rules: reason in AT MOST 6 sentences, decide each point once, never revisit. Then emit the JSON object as the very first thing in your reply content.
Return ONLY JSON: {"code": str, "tests": [{"stdin": str, "stdout": str}], "why": str}"""

SOLVE_MCQ = """You are answering a codepractice MCQ. Input JSON fields: left_panel (theory + question verbatim), options (answer labels verbatim).
Pick exactly one option. Return ONLY JSON: {"answer": str (copy one option verbatim), "why": str (one sentence)}"""

TESTGEN = """Given a codepractice problem statement (left_panel) and a finished program (code), produce the exact console tests. For each visible Sample Input/Output copy stdin/stdout verbatim. If none visible, derive from Input/Output Format bullets. expected stdout is what a PIPED run prints: include the input() prompt text (e.g. "num: ") but NOT the typed value. Return ONLY JSON: {"tests": [{"stdin": str, "stdout": str}]}"""

# ---------------- helpers ----------------
COMMENT_RE = re.compile(r"^\s*#")
MATCH_RE   = re.compile(r"^\s*(match\b.*:\s*$|case\b.*:\s*$|if\b.*\(\s*\)\s*:|elif\b.*\(\s*\)\s*:|else\s*:\s*|.*(\.\.\.|\*\*|___))")

def detect_mode(starter: str) -> str:
    lines = (starter or "").split("\n")
    n_ph = sum(1 for l in lines if COMMENT_RE.match(l))
    if n_ph == 1 and not any(MATCH_RE.match(l) for l in lines):
        return "fragment"
    return "full"

def splice(starter: str, code: str) -> str:
    lines = starter.split("\n")
    idx = [i for i, l in enumerate(lines) if COMMENT_RE.match(l)]
    if not idx: return starter.rstrip("\n") + "\n" + code
    s = e = idx[0]
    while e + 1 < len(lines) and COMMENT_RE.match(lines[e + 1]): e += 1
    base = len(lines[s]) - len(lines[s].lstrip())
    cl = code.rstrip("\n").split("\n")
    ne = [l for l in cl if l.strip()]
    if ne and base > 0 and min(len(l) - len(l.lstrip()) for l in ne) == 0:
        pad = " " * base
        cl = [(pad + l if l.strip() else l) for l in cl]
    lines[s:e + 1] = cl
    return "\n".join(lines)

def run_program(src, stdin_data, timeout=5):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(src); path = f.name
    try:
        p = subprocess.run([sys.executable, path], input=stdin_data, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    finally:
        os.unlink(path)

def echo_split(got_lines, exp_lines, panel):
    if len(got_lines) != len(exp_lines):
        return False
    for g, e in zip(got_lines, exp_lines):
        if g == e:
            continue
        if e and g.endswith(e):
            p = g[:len(g) - len(e)]
            if p and p in panel and (p.endswith(":") or p.endswith(": ") or p.endswith(" ")):
                continue
        return False
    return True

def transcript_variants(exp_lines, stdin_data):
    """Expected written as a console transcript: the typed value sits on the prompt line
    ('marks: 78' + 'distinction') while a piped run prints 'marks: distinction'.
    Strip the stdin value (never platform text) and merge with the next line."""
    vals = [v.strip() for v in (stdin_data or "").splitlines() if v.strip()]
    if not vals:
        return []
    v = vals[0]
    out, i = [], 0
    while i < len(exp_lines):
        r = exp_lines[i].rstrip()
        if v and r.endswith(v) and r[:-len(v)].strip():
            prefix = r[:-len(v)]
            if i + 1 < len(exp_lines):
                out.append(prefix + exp_lines[i + 1].lstrip())
                i += 2
                continue
            out.append(prefix)
        else:
            out.append(exp_lines[i])
        i += 1
    return [out] if out != exp_lines else []

def verify(starter, code, tests, mode, panel=""):
    src = code if mode == "full" else splice(starter, code)
    res = []
    for t in tests:
        rc, out, err = run_program(src, t.get("stdin", ""))
        gl, el = out.splitlines(), (t.get("stdout", "") or "").splitlines()
        cmp_mode = None
        if rc == 0:
            if gl == el:
                cmp_mode = "strict"
            elif echo_split(gl, el, panel):
                cmp_mode = "echo"
            elif any(gl == v for v in transcript_variants(el, t.get("stdin", ""))):
                cmp_mode = "transcript"
        ok = cmp_mode is not None
        res.append({"stdin": t.get("stdin",""), "expected": t.get("stdout",""),
                    "got": out, "stderr": err[-400:], "ok": ok, "compare": cmp_mode})
    passed = all(r["ok"] for r in res)
    if not passed and mode == "fragment":
        res_full = []
        for t in tests:
            rc, out, err = run_program(code, t.get("stdin", ""))
            gl, el = out.splitlines(), (t.get("stdout", "") or "").splitlines()
            cmp_mode = None
            if rc == 0:
                if gl == el:
                    cmp_mode = "strict"
                elif echo_split(gl, el, panel):
                    cmp_mode = "echo"
                elif any(gl == v for v in transcript_variants(el, t.get("stdin", ""))):
                    cmp_mode = "transcript"
            ok = cmp_mode is not None
            res_full.append({"stdin": t.get("stdin",""), "expected": t.get("stdout",""),
                             "got": out, "stderr": err[-400:], "ok": ok, "compare": cmp_mode})
        if all(r["ok"] for r in res_full):
            return True, res_full
    return passed, res

def try_parse(t):
    if not t: return None
    t = re.sub(r"^```(?:json)?\s*", "", t.strip()); t = re.sub(r"\s*```$", "", t)
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e <= s: return None
    try: return json.loads(t[s:e+1])
    except Exception: return None

def salvage(t):
    m = re.search(r'"code"\s*:\s*"((?:[^"\\]|\\.)*)"', t or "")
    if not m: return None
    try: code = json.loads('"' + m.group(1) + '"')
    except Exception: return None
    tests, i = None, (t or "").find('"tests"')
    if i != -1:
        j = t.find("[", i)
        if j != -1:
            try:
                arr, _ = json.JSONDecoder().raw_decode(t[j:])
                tests = arr if isinstance(arr, list) else None
            except Exception: tests = None
    return {"code": code, "tests": tests}

# ---------------- worker ----------------
def worker(key_id, api_key, tasks):
    client = OpenAI(base_url=BASE_URL, api_key=api_key)
    local_solutions, local_triage, local_proposals = [], [], []
    
    for i, rec in enumerate(tasks):
        d = rec.get("data") or {}
        print(f"[Key {key_id}] Processing {d['qid']} ({i+1}/{len(tasks)})...")
        
        if d["type"] == "mcq":
            try:
                r = client.chat.completions.create(model=MODEL, temperature=0.0, max_tokens=2048,
                    messages=[{"role":"system","content":SOLVE_MCQ}, {"role":"user","content":json.dumps({"left_panel": d["left_panel"], "options": d["options"]}, ensure_ascii=False)}])
                raw = (r.choices[0].message.content or "")
            except Exception as e:
                print(f"[Key {key_id}] API Error: {e}"); raw = ""
            
            sol = try_parse(raw) or {}
            ans = (sol.get("answer") or "").strip()
            pick = None
            if ans:
                norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()
                pick = next((o for o in d["options"] if norm(o) == norm(ans)), None) or \
                       next((o for o in d["options"] if norm(ans) in norm(o) or norm(o) in norm(ans)), None)
            if not ans or not pick:
                local_triage.append({"file": rec["file"], "qid": d["qid"],
                                     "type": "mcq", "reason": "unparseable_or_api_error", "raw": raw})
                print(f"[Key {key_id}] MCQ {d['qid']} -> TRIAGE (no valid answer)")
            else:
                local_proposals.append({"file": rec["file"], "qid": d["qid"], "type": "mcq",
                                        "answer": pick, "why": sol.get("why", ""),
                                        "left_panel": d["left_panel"], "options": d["options"]})
                print(f"[Key {key_id}] MCQ {d['qid']} -> PROPOSED {pick} (awaiting local judge)")
            
        else: # code
            mode = detect_mode(d.get("starter_code", ""))
            payload = json.dumps({**{k: d.get(k) for k in ("left_panel", "starter_code", "filename")}, "mode": mode}, ensure_ascii=False)
            cand, raw = None, ""
            try:
                r = client.chat.completions.create(model=MODEL, temperature=0.0, max_tokens=8192,
                    messages=[{"role":"system","content":SOLVE_CODE}, {"role":"user","content":payload}])
                raw = (r.choices[0].message.content or "")
                rea = getattr(r.choices[0].message, "reasoning_content", "") or ""
                if not rea and hasattr(r.choices[0].message, "model_extra"):
                    rea = r.choices[0].message.model_extra.get("reasoning_content", "") or ""
                cand = try_parse(raw) or try_parse(rea) or salvage(raw) or salvage(rea)
            except Exception as e:
                print(f"[Key {key_id}] API Error: {e}")
                
            if not cand or "code" not in cand:
                local_triage.append({"file": rec["file"], "qid": d["qid"], "reason": "unparseable", "raw": raw})
                print(f"[Key {key_id}] CODE {d['qid']} -> TRIAGE (unparseable)")
            else:
                tests = cand.get("tests")
                if not tests:
                    try:
                        r2 = client.chat.completions.create(model=MODEL, temperature=0.0, max_tokens=2048,
                            messages=[{"role":"system","content":TESTGEN}, {"role":"user","content":json.dumps({"left_panel": d["left_panel"], "code": cand["code"]}, ensure_ascii=False)}])
                        raw2 = (r2.choices[0].message.content or "")
                        tests = (try_parse(raw2) or {}).get("tests", [])
                    except Exception: tests = []
                        
                passed, results = verify(d.get("starter_code", ""), cand["code"], tests, mode,
                                         panel=d.get("left_panel", "") + "\n" + d.get("starter_code", ""))
                if passed:
                    local_solutions.append({"file": rec["file"], "qid": d["qid"], "type": "code", "status": "pass", "mode": mode, "code": cand["code"], "tests": tests})
                    print(f"[Key {key_id}] CODE {d['qid']} -> PASS")
                else:
                    local_triage.append({"file": rec["file"], "qid": d["qid"], "status": "fail", "code": cand["code"], "tests": tests, "results": results})
                    print(f"[Key {key_id}] CODE {d['qid']} -> TRIAGE (failed tests)")
                    
        time.sleep(SLEEP_BETWEEN_CALLS) # TPM rate limit protection
        
    return local_solutions, local_triage, local_proposals

# ---------------- main ----------------
def main():
    recs = [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]
    done_files = set()
    for f in (SOLUTIONS, TRIAGE, PROPOSALS):
        if f.exists():
            for l in f.read_text(encoding="utf-8").splitlines():
                if l.strip(): done_files.add(json.loads(l).get("file"))
                
    pending = [r for r in recs if r.get("file") not in done_files and r.get("ok") and (r.get("data") or {}).get("type") in ("code", "mcq")]
    print(f"[i] {len(pending)} tasks pending across {len(KEYS)} keys")
    
    chunks = [[] for _ in range(len(KEYS))]
    for i, task in enumerate(pending):
        chunks[i % len(KEYS)].append(task)
        
    with ThreadPoolExecutor(max_workers=len(KEYS)) as executor:
        futures = [executor.submit(worker, i, KEYS[i], chunks[i]) for i in range(len(KEYS))]
        for future in as_completed(futures):
            sols, tri, props = future.result()
            with SOLUTIONS.open("a", encoding="utf-8") as f:
                for s in sols: f.write(json.dumps(s, ensure_ascii=False) + "\n")
            with TRIAGE.open("a", encoding="utf-8") as f:
                for t in tri: f.write(json.dumps(t, ensure_ascii=False) + "\n")
            with PROPOSALS.open("a", encoding="utf-8") as f:
                for p in props: f.write(json.dumps(p, ensure_ascii=False) + "\n")
    print("[done] Check solutions.jsonl, triage.jsonl, mcq_proposals.jsonl")

if __name__ == "__main__":
    main()