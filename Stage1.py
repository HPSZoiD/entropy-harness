"""
CodePractice pipeline — STAGE 1: extraction v3 (anti-spiral edition)
deps: pip install openai pillow
"""
import base64, io, json, re, time
from pathlib import Path
from openai import OpenAI
from PIL import Image

# ---------------- config ----------------
QUESTIONS_DIR = Path("questions")
LEDGER        = Path("extractions.jsonl")
ERRORS        = Path("failed_extractions.jsonl")
LMSTUDIO_URL  = "http://localhost:1234/v1"
MODEL         = ""
MAX_TOKENS    = 8192        # room to finish thought AND emit JSON
PIXEL_BUDGET  = 1_500_000   # ~1900 img tokens; knob: raise to 3M only for retakes with tiny digits
EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

SYSTEM_PROMPT = """You are a strict OCR extraction engine. You receive one photo of a laptop screen showing a CodePractice page. Return ONLY one valid JSON object: no prose, no markdown fences, no thought blocks. The JSON must be the first and only thing in your reply.
Schema: {"qid":str,"title":str,"type":"code"|"mcq"|"other","left_panel":str,"options":[str],"filename":str,"starter_code":str,"unclear":[str]}
Field rules:
- qid: dotted number at top-left of the content header (e.g. "16.1.1"), no trailing dot.
- title: header text immediately after qid on the same line.
- type: "code" if the right half is a line-numbered code editor. "mcq" if the right half shows a vertical list of short labels each preceded by a small empty square or circle; such lists can LOOK like navigation menus but they ARE answer options. "other" only if neither is present.
- A question line is any line in the left panel phrased as a task or question ("Which of the following...", "What is...", "Select...", "Write a program...").
- If the left panel contains a question line and the RIGHT half shows two or more short text lines stacked vertically, treat them as options EVEN IF no square/circle is visible, and set type to "mcq". Only use "other" when the right half is completely empty or holds no short stacked labels.
- options: for mcq, those right-panel labels verbatim, top to bottom; else [].
- left_panel: EVERY visible text in the left panel, top to bottom, verbatim, line breaks as \\n. Include theory, bullets, syntax/code blocks, the task line (often a SINGLE line near the BOTTOM such as "Which of the following..." or "Write a program to..."), Sample Input and Output blocks, Note lines, and collapsed labels like "Sample Test Cases". Never summarize, never correct.
- filename: editor tab name (e.g. "Ifcon01.py"); else "".
- starter_code: every visible editor line verbatim joined by \\n, INCLUDING highlighted lines and comments; else "".
- unclear: any word/number you cannot read confidently (blur/glare); else [].
- Truncation: if a Sample Test Case block (or any block) is visibly cut off at the bottom edge of the photo or the panel fold (its transcript stops mid-interaction), transcribe what IS visible, then append the marker [TRUNCATED] on its own line immediately after it, and add an entry like "Test case 2 cut off at panel edge" to unclear.
Thinking rules: spend AT MOST one sentence of thought per field. Once a field is decided, NEVER revisit it. If torn between two values, pick one immediately and move on. Never guess unreadable text; put it in unclear. Numbers must be exact."""

DEFAULTS = {"qid":"","title":"","type":"other","left_panel":"",
            "options":[],"filename":"","starter_code":"","unclear":[]}

def normalize(d):
    if not isinstance(d, dict):
        return None
    for k, v in DEFAULTS.items():
        d.setdefault(k, list(v) if isinstance(v, list) else v)
    opts, seen = [], set()
    for o in d.get("options") or []:
        if isinstance(o, str):
            if o in seen:
                continue
            seen.add(o)
        opts.append(o)
    d["options"] = opts
    if d["type"] != "code" and d["options"]:   # label list + no editor => it's an MCQ
        d["type"] = "mcq"
    return d

# ---------------- helpers ----------------
def load_ledger() -> dict:
    done = {}
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                done[rec["file"]] = rec
    return done

def prep_image(path: Path):
    img = Image.open(path).convert("RGB")
    w, h = img.size
    scale = min(1.0, (PIXEL_BUDGET / (w * h)) ** 0.5)
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    b64 = base64.b64encode(buf.getvalue()).decode()
    w2, h2 = img.size
    return f"data:image/jpeg;base64,{b64}", (w2, h2), (w2 * h2) // 784

def try_parse(text: str):
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return None
    try:
        return json.loads(t[s:e + 1])
    except Exception:
        return None

def call_vlm(client, model, data_uri, nudge=""):
    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_uri}},
            {"type": "text", "text": nudge or "Extract now. Output the JSON object as your first token."},
        ]},
    ]
    resp = client.chat.completions.create(
        model=model, messages=msgs, temperature=0.0, max_tokens=MAX_TOKENS)
    msg = resp.choices[0].message
    return (msg.content or ""), (getattr(msg, "reasoning_content", "") or "")

# ---------------- main ----------------
def main():
    client = OpenAI(base_url=LMSTUDIO_URL, api_key="lm-studio")
    model = MODEL or client.models.list().data[0].id
    print(f"[i] model: {model}")
    done = load_ledger()
    images = sorted(p for p in QUESTIONS_DIR.iterdir() if p.suffix.lower() in EXTS)
    print(f"[i] {len(images)} images, {len(done)} already in ledger")

    for p in images:
        if p.name in done:
            print(f"= skip  {p.name}")
            continue
        t0 = time.time()
        data_uri, size, img_tok = prep_image(p)

        content, reasoning = call_vlm(client, model, data_uri)
        data = normalize(try_parse(content) or try_parse(reasoning))   # salvage from reasoning channel
        raw_text = content
        if data is None:
            content2, reasoning2 = call_vlm(client, model, data_uri, nudge=
                "Your previous attempt died mid-thought. ZERO deliberation this time: output the JSON object immediately as your first token.")
            data = normalize(try_parse(content2) or try_parse(reasoning2))
            raw_text, reasoning = (content2 or content), (reasoning2 or reasoning)

        rec = {"file": p.name, "size": list(size), "img_tokens": img_tok,
               "seconds": round(time.time() - t0, 1),
               "reasoning_chars": len(reasoning),
               "ok": data is not None, "data": data}
        if data is None:
            rec["raw"] = raw_text or reasoning[-2000:]
        with LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if data is None:
            with ERRORS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        d = data or {}
        print(f"[{'ok  ' if data else 'FAIL'}] {p.name} -> qid={d.get('qid','?')} "
              f"type={d.get('type','?')} opts={len(d.get('options') or [])} "
              f"img_tok={img_tok} reason_chars={len(reasoning)} ({rec['seconds']}s)")
        if d.get("unclear"):
            print(f"       unclear: {d['unclear']}  <-- (truncation or glare - inspect)")
    print("[done] ledger:", LEDGER)

if __name__ == "__main__":
    main()