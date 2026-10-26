"""Targeted re-verification: re-run stored triage code artifacts against the FIXED harness.
Groq is called ONLY as a one-shot referee for items that fail every local compare mode."""
import importlib.util, json, os, sys
from pathlib import Path
from openai import OpenAI

def _load(n, p):
    s = importlib.util.spec_from_file_location(n, Path(p).resolve())
    m = importlib.util.module_from_spec(s); sys.modules[n] = m; s.loader.exec_module(m); return m

G2 = _load("groq_stage2", "groq-Stage2.py")
F_TRI, F_SOL = Path("triage.jsonl"), Path("solutions.jsonl")
KEY = (os.environ.get("GROQ_API_KEYS", "").split(",") + [""])[0] or (getattr(G2, "KEYS", None) or [""])[0]

def rows(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []
def append(p, o):
    with p.open("a", encoding="utf-8") as f: f.write(json.dumps(o, ensure_ascii=False) + "\n")

def main():
    solved = set()
    for r in rows(F_SOL): solved.add(r.get("file")); solved.add(r.get("qid"))
    items = [r for r in rows(F_TRI) if r.get("type") == "code" and r.get("code")
             and r.get("file") not in solved and r.get("qid") not in solved]
    print(f"[reverify] {len(items)} triage code artifact(s) to re-check")
    client = None
    for it in items:
        qid, starter, panel = it.get("qid"), it.get("starter_code", ""), it.get("left_panel", "")
        code, tests = it["code"], it.get("tests") or []
        if not tests:
            tests = [{"stdin": r.get("stdin", ""), "stdout": r.get("expected", "")}
                     for r in (it.get("results") or [])]
        if not tests:
            print(f"[skip  ] {qid}: no stored tests/results to re-check")
            continue
        mode = G2.detect_mode(starter)
        if mode != "full" and not G2.starter_compiles(starter) and not any(G2.is_hint_line(l) for l in starter.splitlines()):
            print(f"[human ] {qid}: starter SyntaxError - check live editor (OCR scar?) or platform bug; patch extractions.jsonl starter_code, then re-run me")
            continue
        passed, results = G2.verify(starter, code, tests, mode, panel=panel)
        if passed:
            append(F_SOL, {"file": it["file"], "qid": qid, "type": "code", "status": "pass",
                           "code": code, "tests": tests, "harness_fix": True,
                           "modes": [r.get("compare") for r in results]})
            print(f"[fix   ] {qid} PASS via fixed harness modes={[r.get('compare') for r in results]}")
            continue
        client = client or OpenAI(base_url="https://api.groq.com/openai/v1", api_key=KEY)
        r = client.chat.completions.create(model=G2.MODEL, temperature=0.0, max_tokens=4096,
            messages=[{"role": "system", "content": G2.REFEREE},
                      {"role": "user", "content": json.dumps(
                          {"left_panel": panel, "starter_code": starter, "code": code, "tests": tests,
                           "failures": [{"stdin": x.get("stdin"), "expected": x.get("expected"),
                                         "got": x.get("got")} for x in results if not x.get("ok")]},
                          ensure_ascii=False)}])
        ref = G2.try_parse(r.choices[0].message.content) or {}
        fixed = False
        if ref.get("wrong") == "tests" and ref.get("corrected_tests"):
            p2, _ = G2.verify(starter, code, ref["corrected_tests"], mode, panel=panel)
            if p2:
                append(F_SOL, {"file": it["file"], "qid": qid, "type": "code", "status": "pass",
                               "code": code, "tests": ref["corrected_tests"],
                               "tests_corrected": True, "verdict": ref.get("verdict", "")})
                print(f"[fix   ] {qid} PASS with referee-corrected tests: {ref.get('verdict')}")
                fixed = True
        if not fixed and ref.get("wrong") == "code" and ref.get("corrected_code"):
            p3, _ = G2.verify(starter, ref["corrected_code"], tests, mode, panel=panel)
            if p3:
                append(F_SOL, {"file": it["file"], "qid": qid, "type": "code", "status": "pass",
                               "code": ref["corrected_code"], "tests": tests, "referee_code": True})
                print(f"[fix   ] {qid} PASS with referee-corrected code")
                fixed = True
        if not fixed:
            print(f"[leave ] {qid} still failing -> repair pool on next main.py run")

if __name__ == "__main__":
    main()
