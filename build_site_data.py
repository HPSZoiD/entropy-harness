"""Merge extractions.jsonl + solutions.jsonl -> site_data.json for the reference site."""
import json, time
from pathlib import Path

EX_F, SOL_F, OUT = Path("extractions.jsonl"), Path("solutions.jsonl"), Path("site_data.json")

def qkey(q): return [int(x) if x.isdigit() else 0 for x in (q or "").split(".")]
def rows(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []

EX, SOL = {}, {}
for r in rows(EX_F):
    d = r.get("data") or r
    q = d.get("qid")
    if not q or not isinstance(d.get("left_panel"), str): continue
    cur = EX.get(q)
    if cur is None:
        EX[q] = {"title": d.get("title",""), "type": d.get("type","other"),
                 "left_panel": d.get("left_panel",""), "options": d.get("options",[]) or [],
                 "starter_code": d.get("starter_code",""), "filename": d.get("filename","")}
        continue
    lp = d.get("left_panel") or ""
    if lp and lp not in cur["left_panel"] and cur["left_panel"] not in lp:
        cur["left_panel"] += ("\n\n" if cur["left_panel"] else "") + lp      # continuation merge
    if len(d.get("starter_code") or "") > len(cur["starter_code"]): cur["starter_code"] = d["starter_code"]
    if not cur["options"] and d.get("options"): cur["options"] = d["options"]
    if not cur["filename"] and d.get("filename"): cur["filename"] = d["filename"]
    if cur["type"] == "other" and d.get("type") != "other": cur["type"] = d["type"]
    if not cur["title"] and d.get("title"): cur["title"] = d["title"]

for r in rows(SOL_F):
    if r.get("status") not in ("pass", "confirmed") and not r.get("repaired"): continue
    if r.get("qid"): SOL[r["qid"]] = r

items = []
for q, ex in EX.items():
    it = dict(ex); it["qid"] = q; it["solution"] = None
    s = SOL.get(q)
    if s:
        it["solution"] = ({"kind":"code","code":s.get("code",""),"tests":s.get("tests",[]),"status":s.get("status","pass")}
                          if isinstance(s.get("code"), str) else
                          {"kind":"mcq","answer":s.get("answer",""),"status":s.get("status","confirmed")})
    items.append(it)
items.sort(key=lambda it: qkey(it["qid"]))

nc = sum(1 for i in items if i["type"] == "code"); nm = sum(1 for i in items if i["type"] == "mcq")
nu = sum(1 for i in items if not i["solution"])
OUT.write_text(json.dumps({"generated": time.strftime("%Y-%m-%d %H:%M"), "items": items},
                          ensure_ascii=False, indent=1), encoding="utf-8")
print(f"[ok] {OUT}: {len(items)} items ({nc} code, {nm} mcq, {nu} unsolved)")