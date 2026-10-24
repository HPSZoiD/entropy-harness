"""
codepractice pipeline — PRODUCTION ORCHESTRATOR (main.py)
Conductor only: all logic imported from Stage1 / groq-Stage2 / judge_mcqs.
Flow: extract(local) -> queue -> groq solve+verify(parallel keys) -> judge(local) -> repair pool.
usage: python main.py --reset   |   python main.py
"""
import argparse, hashlib, importlib.util, inspect, json, queue, re, sys, threading, time
from pathlib import Path
from openai import OpenAI

ROOT = Path(__file__).resolve().parent

def _load(name, filename):
    p = (ROOT / filename).resolve()
    spec = importlib.util.spec_from_file_location(name, str(p))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

S1 = _load("stage1", "Stage1.py")
G2 = _load("groq_stage2", "groq-Stage2.py")
J  = _load("judge_mcqs", "judge_mcqs.py")

# ---------------- config ----------------
LM_URL, GROQ_URL = "http://localhost:1234/v1", "https://api.groq.com/openai/v1"
GROQ_MODEL = getattr(G2, "MODEL", "openai/gpt-oss-120b")
LM_MODEL   = ""                                  # blank = first loaded in LM Studio
KEYS       = getattr(G2, "KEYS", [])
KEY_SLEEP, REPAIR_ATTEMPTS = 20, 3
F_REPORT = ROOT / "run_report.json"
F_EX, F_FAIL = getattr(S1, "LEDGER", ROOT / "extractions.jsonl"), getattr(S1, "ERRORS", ROOT / "failed_extractions.jsonl")
F_SOL, F_TRI = getattr(G2, "SOLUTIONS", ROOT / "solutions.jsonl"), getattr(G2, "TRIAGE", ROOT / "triage.jsonl")
F_PROP       = getattr(J, "PROPOSALS", ROOT / "mcq_proposals.jsonl")
NUDGE = "Previous attempt died mid-thought. ZERO deliberation: output the JSON object immediately as your first token."

# ---------------- shared state ----------------
WL, SL = threading.Lock(), threading.Lock()
STATS = dict(extract_ok=0, extract_fail=0, skip_other=0, skip_duplicate=0, groq_calls=0, e429=0,
             tests_run=0, tests_pass=0, code_pass=0, code_triage=0, repaired=0,
             repair_fail=0, mcq_prop=0, mcq_conf=0, mcq_conflict=0)
task_q, judge_q = queue.Queue(), queue.Queue()
extract_done, groq_done = threading.Event(), threading.Event()
T_START = time.time()

def log(m): print(f"[{time.time()-T_START:7.1f}s] {m}", flush=True)
def bump(**kw):
    with SL:
        for k, v in kw.items(): STATS[k] += v
def append_json(path, obj):
    with WL:
        p = Path(path)
        # Deduplication guard: never write duplicate QIDs to solutions or proposals
        if p in (F_SOL, F_PROP) and obj.get("qid"):
            existing = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []
            if any(r.get("qid") == obj.get("qid") for r in existing):
                return
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
def read_jsonl(path):
    p = Path(path)
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []

# ---------------- compat adapters ----------------
try_parse = getattr(S1, "try_parse", None) or G2.try_parse
salvage   = getattr(G2, "salvage", None) or (lambda t: None)
def _kw(fn, **wanted):
    sig = inspect.signature(fn)
    return {k: v for k, v in wanted.items() if k in sig.parameters}
def extract_call(client, model, uri, nudge=None):
    return S1.call_vlm(client, model, uri, **(_kw(S1.call_vlm, nudge=nudge) if nudge else {}))
def verify_compat(starter, code, tests, mode, panel):
    return G2.verify(starter, code, tests, mode, **_kw(G2.verify, panel=panel))

def groq_call(client, system, user_text, max_tokens):
    while True:
        try:
            bump(groq_calls=1)
            r = client.chat.completions.create(model=GROQ_MODEL, temperature=0.0, max_tokens=max_tokens,
                messages=[{"role":"system","content":system},{"role":"user","content":user_text}])
            m = r.choices[0].message
            rea = getattr(m, "reasoning_content", "") or ""
            if not rea and getattr(m, "model_extra", None): rea = m.model_extra.get("reasoning_content", "") or ""
            return (m.content or ""), rea
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                bump(e429=1); log("  [429] backoff 60s"); time.sleep(60); continue
            log(f"  [groq error] {e}"); return "", ""

def gen_tests(gcall, left_panel, code):
    raw, rea = gcall(G2.TESTGEN, json.dumps({"left_panel": left_panel, "code": code}, ensure_ascii=False), 2048)
    return (try_parse(raw) or try_parse(rea) or {}).get("tests") or []

# ---------------- workers ----------------
def local_worker():
    client = OpenAI(base_url=LM_URL, api_key="lm-studio")
    model = LM_MODEL or client.models.list().data[0].id
    done = {r["file"]: r for r in read_jsonl(F_EX)}
    solved_qids = {r["qid"]: r.get("file", "") for r in read_jsonl(F_SOL) if "qid" in r}
    seen_qids = dict(solved_qids)  # tracks all seen QIDs to reject duplicate questions
    seen_hashes = {}
    q_dir = S1.QUESTIONS_DIR if Path(S1.QUESTIONS_DIR).is_absolute() else (ROOT / S1.QUESTIONS_DIR)
    
    # Phase 1: Pure OCR extraction (one at a time, strictly no concurrent LM Studio calls)
    for p in sorted(q for q in q_dir.iterdir() if q.suffix.lower() in S1.EXTS):
        # 1. Fast byte-level duplicate check
        try:
            img_hash = hashlib.md5(p.read_bytes()).hexdigest()
            if img_hash in seen_hashes:
                log(f"[extract] {p.name} identical image to {seen_hashes[img_hash]} -> skipped duplicate")
                bump(skip_duplicate=1)
                continue
            seen_hashes[img_hash] = p.name
        except Exception:
            pass

        # 2. Check if already extracted
        if p.name in done:
            rec = done[p.name]
            d = rec.get("data") or {}
            qid = d.get("qid")
            if qid and qid in solved_qids:
                # Already solved in solutions.jsonl -> skip re-solving!
                seen_qids[qid] = p.name
                continue
            if qid and qid in seen_qids and seen_qids[qid] != p.name:
                log(f"[extract] {qid} ({p.name}) duplicate of {seen_qids[qid]} -> rejected")
                bump(skip_duplicate=1)
                continue
            if qid:
                seen_qids[qid] = p.name
            if rec.get("ok") and d.get("type") in ("code", "mcq"):
                task_q.put(rec)
            continue

        # 3. Fresh extraction via LM Studio
        t0 = time.time()
        out = S1.prep_image(p); uri = out[0]
        content, rea = extract_call(client, model, uri)
        data = S1.normalize(try_parse(content) or try_parse(rea))
        if data is None:
            content, rea = extract_call(client, model, uri, nudge=NUDGE)
            data = S1.normalize(try_parse(content) or try_parse(rea))

        qid = (data or {}).get("qid")
        # 4. Duplicate QID rejection: if this question ID was already processed, reject it
        if data and qid and qid in seen_qids:
            bump(skip_duplicate=1)
            log(f"[extract] {qid} from {p.name} is a DUPLICATE of {seen_qids[qid]} -> rejected")
            rec = {"file": p.name, "seconds": round(time.time()-t0, 1), "ok": False, "duplicate": True, "duplicate_of": seen_qids[qid], "data": data}
            append_json(F_EX, rec)
            continue

        rec = {"file": p.name, "seconds": round(time.time()-t0, 1), "ok": data is not None, "data": data}
        append_json(F_EX if data else F_FAIL, rec)
        if data and qid:
            seen_qids[qid] = p.name

        if data and data["type"] in ("code", "mcq"):
            bump(extract_ok=1); log(f"[extract] {data['qid']} type={data['type']} ({rec['seconds']}s) -> queue")
            task_q.put(rec)
        elif data:
            bump(skip_other=1); log(f"[extract] {data['qid']} type=other -> skipped")
        else:
            bump(extract_fail=1); log(f"[extract] {p.name} FAILED")
            
    extract_done.set()
    
    # Phase 2: Judge drain (runs after OCR completes so LM Studio is strictly single-tasked)
    while True:
        try:
            item = judge_q.get(timeout=1)
        except queue.Empty:
            if groq_done.is_set() and judge_q.empty():
                break
            continue
        t0 = time.time()
        raw, rea = "", ""
        try:
            r = client.chat.completions.create(model=model, temperature=0.0, max_tokens=8192,
                messages=[{"role":"system","content":J.JUDGE_PROMPT},
                          {"role":"user","content":json.dumps({"left_panel": item["left_panel"],
                              "options": item["options"], "proposed_answer": item["answer"]}, ensure_ascii=False)}])
            raw = (r.choices[0].message.content or "") or (getattr(r.choices[0].message, "reasoning_content", "") or "")
        except Exception as e:
            log(f"  [judge error] {e}")
        v = try_parse(raw) or {}
        if v.get("agree") is True:
            bump(mcq_conf=1)
            append_json(F_SOL, {"file": item["file"], "qid": item["qid"], "type": "mcq",
                                "answer": item["answer"], "status": "confirmed", "why": item.get("why","")})
            log(f"[judge] {item['qid']} CONFIRMED ({time.time()-t0:.0f}s)")
        else:
            bump(mcq_conflict=1)
            append_json(F_TRI, {"file": item["file"], "qid": item["qid"], "type": "mcq",
                                "groq_answer": item["answer"], "ornith_guess": v.get("correct_option","unknown"),
                                "status": "conflict", "options": item["options"]})
            log(f"[judge] {item['qid']} CONFLICT (ornith: {v.get('correct_option','?')})")

def groq_worker(wid, key):
    client = OpenAI(base_url=GROQ_URL, api_key=key)
    def gcall(*a, **kw):
        out = groq_call(client, *a, **kw); time.sleep(KEY_SLEEP); return out
    while True:
        rec = task_q.get()
        if rec is None: break
        d = rec["data"]; t0 = time.time()
        if d["type"] == "mcq":
            raw, rea = gcall(G2.SOLVE_MCQ, json.dumps({"left_panel": d["left_panel"], "options": d["options"]}, ensure_ascii=False), 2048)
            sol = try_parse(raw) or try_parse(rea) or {}
            ans = (sol.get("answer") or "").strip(); pick = None
            if ans:
                norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()
                pick = next((o for o in d["options"] if norm(o) == norm(ans)), None) or \
                       next((o for o in d["options"] if norm(ans) in norm(o) or norm(o) in norm(ans)), None)
            if not ans or not pick:
                bump(code_triage=1)
                append_json(F_TRI, {"file": rec["file"], "qid": d["qid"], "type": "mcq",
                                    "reason": "unparseable_or_api_error", "raw": raw})
                log(f"[K{wid}] {d['qid']} MCQ -> TRIAGE (no valid answer)")
            else:
                bump(mcq_prop=1)
                append_json(F_PROP, {"file": rec["file"], "qid": d["qid"], "answer": pick,
                                     "left_panel": d["left_panel"], "options": d["options"], "why": sol.get("why","")})
                judge_q.put({"file": rec["file"], "qid": d["qid"], "answer": pick,
                             "left_panel": d["left_panel"], "options": d["options"], "why": sol.get("why","")})
                log(f"[K{wid}] {d['qid']} MCQ proposed -> judge queue")
        else:
            mode = G2.detect_mode(d.get("starter_code", ""))
            panel = (d.get("left_panel","") or "") + "\n" + (d.get("starter_code","") or "")
            payload = json.dumps({**{k: d.get(k) for k in ("left_panel","starter_code","filename")}, "mode": mode}, ensure_ascii=False)
            raw, rea = gcall(G2.SOLVE_CODE, payload, 8192)
            cand = try_parse(raw) or try_parse(rea) or salvage(raw) or salvage(rea)
            results = None
            if cand and "code" in cand:
                tests = cand.get("tests") or gen_tests(gcall, d["left_panel"], cand["code"])
                passed, results = verify_compat(d.get("starter_code",""), cand["code"], tests, mode, panel)
                if passed:
                    bump(code_pass=1, tests_run=len(results), tests_pass=len(results))
                    append_json(F_SOL, {"file": rec["file"], "qid": d["qid"], "type": "code", "status": "pass",
                                        "code": cand["code"], "tests": tests, "filename": d.get("filename","")})
                    log(f"[K{wid}] {d['qid']} CODE PASS {len(results)}/{len(results)} ({time.time()-t0:.0f}s)")
                    task_q.task_done(); continue
                bump(tests_run=len(results), tests_pass=sum(r["ok"] for r in results))
            bump(code_triage=1)
            append_json(F_TRI, {"file": rec["file"], "qid": d["qid"], "type": "code", "status": "fail",
                                "starter_code": d.get("starter_code",""), "left_panel": d.get("left_panel",""),
                                "code": (cand or {}).get("code"), "results": results, "raw": raw})
            log(f"[K{wid}] {d['qid']} CODE -> TRIAGE")
        task_q.task_done()

def repair_worker(wid, key, rq):
    client = OpenAI(base_url=GROQ_URL, api_key=key)
    def gcall(*a, **kw):
        out = groq_call(client, *a, **kw); time.sleep(KEY_SLEEP); return out
    while True:
        d = rq.get()
        if d is None: break
        feedback = json.dumps([r for r in (d.get("results") or []) if not r.get("ok")], ensure_ascii=False)[:3000] or "unparseable previous reply"
        mode = G2.detect_mode(d.get("starter_code", ""))
        panel = (d.get("left_panel","") or "") + "\n" + (d.get("starter_code","") or "")
        payload = json.dumps({"left_panel": d["left_panel"], "starter_code": d["starter_code"], "mode": mode}, ensure_ascii=False)
        fixed = False
        for attempt in range(1, REPAIR_ATTEMPTS + 1):
            raw, rea = gcall(G2.SOLVE_CODE, payload + f"\nPrevious attempt FAILED:\n{feedback}", 8192)
            cand = try_parse(raw) or try_parse(rea) or salvage(raw) or salvage(rea)
            if not cand or "code" not in cand: continue
            tests = cand.get("tests") or gen_tests(gcall, d["left_panel"], cand["code"])
            passed, results = verify_compat(d.get("starter_code",""), cand["code"], tests, mode, panel)
            if passed:
                bump(repaired=1, tests_run=len(results), tests_pass=len(results))
                append_json(F_SOL, {"file": d["file"], "qid": d["qid"], "type": "code", "status": "pass",
                                    "repaired": True, "code": cand["code"], "tests": tests})
                log(f"[R{wid}] {d['qid']} REPAIRED (attempt {attempt})"); fixed = True; break
            bump(tests_run=len(results), tests_pass=sum(r["ok"] for r in results))
            feedback = json.dumps([r for r in results if not r["ok"]], ensure_ascii=False)[:3000]
        if not fixed:
            bump(repair_fail=1); log(f"[R{wid}] {d['qid']} STILL BROKEN -> manual")
        rq.task_done()

# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--reset", action="store_true"); args = ap.parse_args()
    if not KEYS:
        log("[!] no Groq API keys found — set GROQ_API_KEYS (comma-separated) and rerun")
        return
    if args.reset:
        for f in (F_EX, F_SOL, F_TRI, F_PROP, F_FAIL, F_REPORT):
            Path(f).unlink(missing_ok=True)
        log("ledgers wiped — clean production run")
    workers = [threading.Thread(target=groq_worker, args=(i, KEYS[i]), daemon=True) for i in range(len(KEYS))]
    for w in workers: w.start()
    lm = threading.Thread(target=local_worker, daemon=True); lm.start()
    extract_done.wait(); task_q.join()
    for _ in KEYS: task_q.put(None)
    for w in workers: w.join()
    groq_done.set(); lm.join()
    # ---- repair phase ----
    solved = {r["file"] for r in read_jsonl(F_SOL) if "file" in r}
    broken = [r for r in read_jsonl(F_TRI) if r.get("type") == "code" and r.get("file") not in solved]
    if broken:
        log(f"[repair] {len(broken)} triage item(s) enter the repair pool")
        rq = queue.Queue()
        rws = [threading.Thread(target=repair_worker, args=(i, KEYS[i], rq), daemon=True) for i in range(len(KEYS))]
        for w in rws: w.start()
        for item in broken: rq.put(item)
        rq.join()
        for _ in KEYS: rq.put(None)
        for w in rws: w.join()
    # ---- report ----
    sol, tri, ex = read_jsonl(F_SOL), read_jsonl(F_TRI), read_jsonl(F_EX)
    solved_files = {r["file"] for r in sol if "file" in r}
    unresolved = [r for r in tri if r.get("file") not in solved_files]
    rep = {"wall_seconds": round(time.time() - T_START, 1), "stats": STATS,
           "extracted": len([r for r in ex if r.get("ok")]), "solved_unique": len(solved_files),
           "unresolved": [{"qid": r.get("qid"), "type": r.get("type")} for r in unresolved]}
    F_REPORT.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    log("=" * 60)
    log(f"WALL {rep['wall_seconds']}s | extracted {rep['extracted']} | solved {rep['solved_unique']} | unresolved {len(unresolved)}")
    log(json.dumps(STATS))
    for u in unresolved: log(f"  UNRESOLVED: {u}")

if __name__ == "__main__":
    main()
