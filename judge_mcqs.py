"""
Stage 3: Local MCQ Verification (The Ornith Judge)
deps: pip install openai
Reads Groq's MCQ guesses from mcq_proposals.jsonl, asks the local model to agree/disagree,
routes confirmed -> solutions.jsonl and conflicts -> triage.jsonl.
"""
import json, re
from pathlib import Path
from openai import OpenAI

PROPOSALS = Path("mcq_proposals.jsonl")
SOLUTIONS = Path("solutions.jsonl")
TRIAGE    = Path("triage.jsonl")
LMSTUDIO_URL = "http://localhost:1234/v1"
MODEL = ""  # auto-detect

JUDGE_PROMPT = """You are a strict grader for a university computer science exam.
I will give you a question, the options, and an answer chosen by another AI.
Read the left_panel (theory/context) carefully.
Determine if the chosen answer is logically correct based on the text or general CS knowledge.
Reply ONLY with a JSON object: {"agree": true/false, "correct_option": "exact string of the right answer"}"""

def try_parse(t):
    if not t: return None
    t = re.sub(r"^```(?:json)?\s*", "", t.strip()); t = re.sub(r"\s*```$", "", t)
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e <= s: return None
    try: return json.loads(t[s:e+1])
    except Exception: return None

def main():
    client = OpenAI(base_url=LMSTUDIO_URL, api_key="lm-studio")
    model = MODEL or client.models.list().data[0].id
    print(f"[i] Judge model: {model}")

    solved = set()
    for f in (SOLUTIONS, TRIAGE):
        if f.exists():
            for l in f.read_text(encoding="utf-8").splitlines():
                if l.strip(): solved.add(json.loads(l).get("file"))

    if not PROPOSALS.exists():
        print("[!] no mcq_proposals.jsonl yet")
        return
    props = [json.loads(l) for l in PROPOSALS.read_text(encoding="utf-8").splitlines() if l.strip()]

    for p in props:
        if p["file"] in solved:
            continue

        groq_answer = p["answer"]
        user_payload = json.dumps({
            "left_panel": p["left_panel"],
            "options": p["options"],
            "proposed_answer": groq_answer
        }, ensure_ascii=False)

        print(f"Judging {p['qid']} (Groq said: {groq_answer})...")

        try:
            r = client.chat.completions.create(
                model=model, temperature=0.0, max_tokens=8192,
                messages=[{"role": "system", "content": JUDGE_PROMPT},
                          {"role": "user", "content": user_payload}]
            )
            msg = r.choices[0].message
            raw = (msg.content or "") or getattr(msg, "reasoning_content", "") or ""
            verdict = try_parse(raw) or {}
        except Exception as e:
            print(f"  [!] API Error: {e}")
            verdict = {}

        if verdict.get("agree") is True:
            out = {"file": p["file"], "qid": p["qid"], "type": "mcq",
                   "answer": groq_answer, "status": "confirmed", "why": p.get("why", "")}
            with SOLUTIONS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(out, ensure_ascii=False) + "\n")
            print("  -> CONFIRMED")
        else:
            ornith_guess = verdict.get("correct_option", "unknown")
            out = {"file": p["file"], "qid": p["qid"], "type": "mcq",
                   "groq_answer": groq_answer, "ornith_guess": ornith_guess,
                   "status": "conflict", "options": p["options"]}
            with TRIAGE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(out, ensure_ascii=False) + "\n")
            print(f"  -> CONFLICT (Ornith guessed: {ornith_guess})")

if __name__ == "__main__":
    main()
