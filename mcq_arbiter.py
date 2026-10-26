"""Deterministic interpreter arbitration for MCQ conflicts + sentinel handling.
Runs ONLY on triage conflict records. Unfalsifiable conflicts stay for humans."""
import ast, json, re, subprocess, sys, tempfile, os
from pathlib import Path

F_TRI, F_SOL = Path("triage.jsonl"), Path("solutions.jsonl")
SENTINELS = {"unknown", "n/a", "null", "none", ""}

def rows(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []
def append(p, o):
    with p.open("a", encoding="utf-8") as f: f.write(json.dumps(o, ensure_ascii=False) + "\n")
def nset(xs): return {re.sub(r"\s+", " ", x).strip().lower() for x in xs if isinstance(x, str)}

CLAIM_RES = (
    re.compile(r"^(.*?)[,;]\s*print\((.+)\)\s+(?:returns|is|prints?|evaluates to)\s+(.+)$", re.I),
    re.compile(r"print\((.+)\)\s+(?:returns|is|prints?|evaluates to)\s+(.+)$", re.I),
    re.compile(r"code:?\s*(.+?)\s+is\s+(.+)$", re.I),
    re.compile(r"[`\u201c\"](.+?)[`\u201d\"]\s+(?:returns|is|prints?|evaluates to)\s+(.+)$", re.I),
    re.compile(r"(.+?)\s+(?:returns|is|prints?|evaluates to)\s+(.+)$", re.I),
)

def run_claim(prefix, expr, claimed):
    claimed = claimed.strip().rstrip(" .;")
    src = (prefix.strip() + "\n" if prefix.strip() else "") + f"print(repr({expr}))"
    try: compile(src, "<claim>", "exec")
    except SyntaxError: return None
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(src); p = f.name
    try:
        r = subprocess.run([sys.executable, p], capture_output=True, text=True, timeout=5)
        got = r.stdout.strip()
    except Exception: return None
    finally: os.unlink(p)
    if not got: return None
    try: want = repr(ast.literal_eval(claimed))
    except Exception: want = claimed.strip("'\"")
    return got == want or got.strip("'\"") == want

def eval_claim(option):
    for i, r in enumerate(CLAIM_RES):
        m = r.search(option)
        if not m: continue
        if i == 0:
            prefix, expr, claimed = m.group(1), m.group(2), m.group(3)
        else:
            prefix, expr, claimed = "", m.group(1), m.group(2)
        v = run_claim(prefix, expr.strip(), claimed)
        if v is not None: return v
    return None

def main():
    solved = set()
    for r in rows(F_SOL): solved.add(r.get("file")); solved.add(r.get("qid"))
    confs = [r for r in rows(F_TRI) if r.get("status") == "conflict"
             and r.get("file") not in solved and r.get("qid") not in solved]
    print(f"[arbiter] {len(confs)} conflict(s) to arbitrate")
    for c in confs:
        groq = c.get("groq_answer") or []; orn = c.get("ornith_guess") or []
        groq = groq if isinstance(groq, list) else [groq]
        orn = orn if isinstance(orn, list) else [orn]
        if nset(orn) <= SENTINELS or not orn:
            append(F_SOL, {"file": c["file"], "qid": c["qid"], "type": "mcq", "answer": groq,
                           "status": "confirmed", "judge_missing": True})
            print(f"[sentinel] {c['qid']} judge sentinel -> proposer answer accepted (flagged)"); continue
        if nset(groq) <= SENTINELS or not groq:
            append(F_SOL, {"file": c["file"], "qid": c["qid"], "type": "mcq", "answer": orn,
                           "status": "confirmed", "proposer_missing": True})
            print(f"[sentinel] {c['qid']} proposer sentinel -> judge answer accepted (flagged)"); continue
        opts = []
        for o in (c.get("options") or []):
            if isinstance(o, str) and "\n" in o:
                opts.extend([l.strip() for l in o.splitlines() if l.strip()])
            else:
                opts.append(o)
        verdicts = {o: eval_claim(o) for o in opts}
        truth = nset([o for o, v in verdicts.items() if v is True])
        falsified = nset([o for o, v in verdicts.items() if v is False])
        def valid(side):
            s = nset(side)
            return bool(s) and bool(truth) and truth <= s and not (s & falsified)
        win = None
        if valid(groq) and not valid(orn):
            win = groq
        elif valid(orn) and not valid(groq):
            win = orn
        elif valid(groq) and valid(orn):
            win = groq if len(nset(groq)) <= len(nset(orn)) else orn
        if win:
            append(F_SOL, {"file": c["file"], "qid": c["qid"], "type": "mcq", "answer": win,
                           "status": "confirmed", "arbiter": "interpreter"})
            print(f"[interp  ] {c['qid']} interpreter verdict -> accepted")
        else:
            print(f"[human   ] {c['qid']} unfalsifiable or arbiter split -> human review")

if __name__ == "__main__":
    main()
