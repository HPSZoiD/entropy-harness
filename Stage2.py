"""
CodePractice pipeline — STAGE 2: solve + sandbox-verify
deps: pip install openai
Solver endpoint via env: SOLVER_BASE_URL / SOLVER_API_KEY / SOLVER_MODEL
(defaults to local LM Studio — Ornith-1.5-9B is a strong coder, let it reason here)
"""
import importlib.util, json, os, re, sys
from pathlib import Path
from openai import OpenAI

def _load_g2():
    p = Path(__file__).with_name("groq-Stage2.py").resolve()
    spec = importlib.util.spec_from_file_location("groq_stage2_standalone", str(p))
    m = importlib.util.module_from_spec(spec)
    sys.modules["groq_stage2_standalone"] = m
    spec.loader.exec_module(m)
    return m

G2 = _load_g2()

LEDGER, OUT = Path("extractions.jsonl"), Path("solutions.jsonl")
MAX_ATTEMPTS = 3
MAX_TOKENS   = 8192   # reasoning + JSON share one budget on llama.cpp; needs ctx >= 12288
BASE_URL = os.environ.get("SOLVER_BASE_URL", "http://localhost:1234/v1")
API_KEY  = os.environ.get("SOLVER_API_KEY", "lm-studio")
MODEL    = os.environ.get("SOLVER_MODEL", "")
COMMENT_RE = re.compile(r"^\s*#")
MATCH_RE   = re.compile(r"^\s*(match\b.*:\s*$|case\b.*:\s*$)")

SOLVE_CODE = """You are completing a Python exercise on CodePractice. Input JSON: left_panel (problem verbatim), starter_code (exact editor content; highlighted lines are LOCKED), filename, mode.
mode="fragment": return ONLY the code that replaces the placeholder comment line(s).
mode="full": return the COMPLETE program: every starter line verbatim in order (locked lines unchanged), with each placeholder comment replaced by your code.
Hard rules:
1. Never modify locked lines; variables defined there already exist.
2. Indentation context: code inserted inside a block (match/if/for/def/while) must be indented into it; case clauses sit one level under their match.
3. STDOUT EXACTNESS: output must match samples character-for-character. If a sample's stdout contains a line "<label>: <value>" where <value> is that sample's stdin, the program must emit that echo itself - normally by reading with input("<label>: ") - unless a locked line already does. Consoles do NOT echo input for free.
4. Generic logic; hidden tests exist; never hardcode sample inputs.
5. Read input only if not already read by starter/locked lines.
6. If samples are missing, derive tests from Input/Output Format bullets (every listed case + one invalid/edge).
Thinking rules: reason in AT MOST 6 sentences, decide each point once, never revisit. Then emit the JSON object as the very first thing in your reply content.
Return ONLY JSON: {"code": str, "tests": [{"stdin": str, "stdout": str}], "why": str}"""

TESTGEN = """Given a CodePractice problem statement (left_panel) and a finished program (code), produce the exact console tests. For each visible Sample Input/Output copy stdin/stdout verbatim. If none visible, derive from Input/Output Format bullets (every listed case + one invalid/edge). stdout includes any input-prompt echo exactly as samples show. Return ONLY JSON: {"tests": [{"stdin": str, "stdout": str}]}"""

SOLVE_MCQ = """You are answering a CodePractice MCQ. Input JSON fields: left_panel (theory + question verbatim), options (answer labels verbatim).
Pick exactly one option. Return ONLY JSON: {"answer": str (copy one option verbatim), "why": str (one sentence)}"""

def try_parse(t):
    if not t: return None
    t = re.sub(r"^```(?:json)?\s*", "", t.strip()); t = re.sub(r"\s*```$", "", t)
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e <= s: return None
    try: return json.loads(t[s:e+1])
    except Exception: return None

def salvage(t):
    """Recover a complete 'code' string from a reply whose JSON was beheaded."""
    m = re.search(r'"code"\s*:\s*"((?:[^"\\]|\\.)*)"', t or "")
    if not m:
        return None
    try:
        code = json.loads('"' + m.group(1) + '"')
    except Exception:
        return None
    tests, i = None, (t or "").find('"tests"')
    if i != -1:
        j = t.find("[", i)
        if j != -1:
            try:
                arr, _ = json.JSONDecoder().raw_decode(t[j:])
                tests = arr if isinstance(arr, list) else None
            except Exception:
                tests = None
    return {"code": code, "tests": tests}

def gen_tests(client, model, left_panel, code):
    raw, rea = call(client, model, TESTGEN,
                    json.dumps({"left_panel": left_panel, "code": code}, ensure_ascii=False))
    return ((try_parse(raw) or try_parse(rea)) or {}).get("tests") or []

def call(client, model, system, user):
    r = client.chat.completions.create(model=model, temperature=0.0, max_tokens=MAX_TOKENS,
        messages=[{"role":"system","content":system},{"role":"user","content":user}])
    m = r.choices[0].message
    return (m.content or ""), (getattr(m, "reasoning_content", "") or "")

def detect_mode(starter: str) -> str:
    lines = (starter or "").split("\n")
    n_ph = sum(1 for l in lines if COMMENT_RE.match(l))
    if n_ph == 1 and not any(MATCH_RE.match(l) for l in lines):
        return "fragment"
    return "full"          # >=2 placeholders, match/case skeleton, or none at all

def splice(starter: str, code: str) -> str:
    lines = starter.split("\n")
    idx = [i for i, l in enumerate(lines) if COMMENT_RE.match(l)]
    if not idx:
        return starter.rstrip("\n") + "\n" + code
    s = e = idx[0]                                  # maximal consecutive comment run
    while e + 1 < len(lines) and COMMENT_RE.match(lines[e + 1]):
        e += 1
    base = len(lines[s]) - len(lines[s].lstrip())
    cl = code.rstrip("\n").split("\n")
    ne = [l for l in cl if l.strip()]
    if ne and base > 0 and min(len(l) - len(l.lstrip()) for l in ne) == 0:
        pad = " " * base                            # auto-indent safety net
        cl = [(pad + l if l.strip() else l) for l in cl]
    lines[s:e + 1] = cl
    return "\n".join(lines)

def verify(starter, code, tests, mode, panel=""):
    return G2.verify(starter, code, tests, mode, panel=panel)

def main():
    client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
    model = MODEL or client.models.list().data[0].id
    print(f"[i] solver: {model}")
    solved = {json.loads(l)["file"] for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip()} if OUT.exists() else set()
    recs = [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]
    for rec in recs:
        d = rec.get("data") or {}
        if not rec.get("ok") or d.get("type") not in ("code", "mcq") or rec["file"] in solved:
            continue
        if d["type"] == "mcq":
            raw, rea = call(client, model, SOLVE_MCQ, json.dumps(
                {"left_panel": d["left_panel"], "options": d["options"]}, ensure_ascii=False))
            sol = try_parse(raw) or try_parse(rea) or {}
            norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()
            ans = sol.get("answer", "")
            pick = next((o for o in d["options"] if norm(o) == norm(ans)), None) or \
                   next((o for o in d["options"] if norm(ans) in norm(o) or norm(o) in norm(ans)), None)
            out = {"file": rec["file"], "qid": d["qid"], "type": "mcq",
                   "answer": pick or ans, "matched": pick is not None, "why": sol.get("why", "")}
            print(f"[mcq ] {d['qid']} -> {out['answer']}  (matched={pick is not None})")
        else:
            mode = detect_mode(d.get("starter_code", ""))
            payload = json.dumps({**{k: d.get(k) for k in ("left_panel", "starter_code", "filename")},
                                  "mode": mode}, ensure_ascii=False)
            feedback, sol = "", None
            for attempt in range(1, MAX_ATTEMPTS + 1):
                user = payload + (f"\nPrevious attempt FAILED these tests:\n{feedback}" if feedback else "")
                raw, rea = call(client, model, SOLVE_CODE, user)
                cand = try_parse(raw) or try_parse(rea) or salvage(raw) or salvage(rea)
                if not cand or "code" not in cand:
                    feedback = "unparseable reply"; continue
                tests = cand.get("tests") or gen_tests(client, model, d["left_panel"], cand["code"])
                passed, results = verify(d.get("starter_code", ""), cand["code"], tests, mode)
                if passed:
                    sol = cand; print(f"[code] {d['qid']} -> PASS {len(results)}/{len(results)} tests (attempt {attempt})")
                    break
                feedback = json.dumps([r for r in results if not r["ok"]], ensure_ascii=False)[:3000]
                print(f"[code] {d['qid']} -> attempt {attempt} failed {sum(not r['ok'] for r in results)} test(s), retrying")
            if sol is None:
                print(f"[code] {d['qid']} -> UNRESOLVED after {MAX_ATTEMPTS} attempts, logged raw")
                out = {"file": rec["file"], "qid": d["qid"], "type": "code", "status": "unresolved", "last": feedback}
            else:
                out = {"file": rec["file"], "qid": d["qid"], "type": "code", "status": "pass",
                       "code": sol["code"], "tests": sol.get("tests", []), "why": sol.get("why", "")}
                print(f"----- paste into {d.get('filename','editor')} {'(whole editor)' if mode == 'full' else 'at the placeholder'} -----")
                print(sol["code"])
                print("------------------------------------------------------------------")
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
    print("[done] solutions:", OUT)

if __name__ == "__main__":
    main()