#!/usr/bin/env python3
"""Gyankshetra Telegram bot (Groq AI)
Automatic Quiz Generator, Notes Generator & Scheduled Channel Publisher
"""
import os
import re
import json
import time
import html
import random
import subprocess
from html.parser import HTMLParser
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

# Telegram Channel ID ya Username
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "@Gyankshetra")

ALLOWED = {
    x.strip()
    for x in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
    if x.strip()
}

# Groq ne llama-3.3-70b-versatile aur llama-3.1-8b-instant 16 Aug 2026 ko band kar diye.
# Default ab openai/gpt-oss-120b hai. Badalna ho to GROQ_MODEL env set karein
# (jaise "qwen/qwen3.6-27b" ya "openai/gpt-oss-20b").
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

BASE_URL = "https://gyankshetra.github.io/Gyankshetra-app/"
TG = "https://api.telegram.org/bot" + BOT_TOKEN
OFFSET_FILE = "content/telegram_offset.txt"
REGISTRY_FILE = "content/generated_tests.json"
STUDY_MATERIAL_FILE = "content/study_material.json"
SCHEDULE_FILE = "content/scheduled_posts.json"

BATCH = 8            # ek call me kitne sawal (chhota batch = JSON kam katega)
MAX_TOKENS = 6000    # Groq response limit
TG_LIMIT = 4000      # Telegram message limit 4096 hai


# ------------------------------------------------------------------ Telegram
def telegram(method, data=None):
    r = requests.post(TG + "/" + method, json=data or {}, timeout=60)
    r.raise_for_status()
    return r.json()


def send_message(chat_id, text, parse_mode="HTML"):
    """Message bhejta hai. Fail hone par plain text try karta hai.
    Success par response dict, fail par None return karta hai."""
    text = text[:TG_LIMIT]
    try:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": False
        }
        return telegram("sendMessage", payload)
    except Exception as e:
        print(f"sendMessage failed for {chat_id}:", e)
        try:
            clean_text = html.unescape(re.sub(r'<[^>]+>', '', text))
            return telegram("sendMessage", {"chat_id": chat_id, "text": clean_text})
        except Exception as e2:
            print("plain sendMessage failed too:", e2)
            return None


# ------------------------------------------------------- Scheduled posts queue
def _load_queue():
    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_queue(queue):
    os.makedirs("content", exist_ok=True)
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)


def schedule_channel_post(chat_id, text, target_timestamp):
    queue = _load_queue()
    queue.append({"chat_id": chat_id, "text": text, "ts": target_timestamp, "tries": 0})
    _save_queue(queue)


def run_due_posts():
    queue = _load_queue()
    if not queue:
        return
    now = datetime.now(timezone.utc).timestamp()
    pending = []
    for p in queue:
        if p.get("ts", 0) > now:
            pending.append(p)
            continue
        if send_message(p["chat_id"], p["text"], parse_mode="HTML"):
            continue
        # send fail hua: queue me rakho, 5 koshish ke baad hi hatao
        p["tries"] = p.get("tries", 0) + 1
        if p["tries"] < 5:
            pending.append(p)
        else:
            print("Scheduled post dropped after 5 failed tries:", p.get("text", "")[:60])
    _save_queue(pending)


# ------------------------------------------------------------------ Commands
COMMANDS = ["/test", "/mock", "/quiz", "/practice", "/pyq", "/notes"]


def parse_schedule_time(text):
    match = re.search(r'\b(\d{1,2}):(\d{2})\s*(AM|PM)\b', text, flags=re.IGNORECASE)
    if not match:
        return None, text

    hours = int(match.group(1))
    minutes = int(match.group(2))
    period = match.group(3).upper()

    if period == "PM" and hours < 12:
        hours += 12
    elif period == "AM" and hours == 12:
        hours = 0

    clean_text = re.sub(r'\b\d{1,2}:\d{2}\s*(AM|PM)\b', '', text, flags=re.IGNORECASE)
    clean_text = re.sub(r'\b(today|publish)\b', '', clean_text, flags=re.IGNORECASE).strip()

    IST = timezone(timedelta(hours=5, minutes=30))
    ist_now = datetime.now(IST)
    try:
        target_dt = ist_now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    except ValueError:
        return None, clean_text

    if target_dt <= ist_now:
        target_dt += timedelta(days=1)

    return target_dt.timestamp(), clean_text


def parse_command(text):
    parts = (text or "").strip().split()
    if not parts:
        return None
    command = parts[0].lower().split("@")[0]
    if command not in COMMANDS:
        return None

    rest_str = " ".join(parts[1:])
    schedule_ts, cleaned_rest = parse_schedule_time(rest_str)

    rest_parts = cleaned_rest.split()
    count = 10
    if rest_parts and command != "/notes":
        m = re.fullmatch(r"(\d{1,3})[qQ]?", rest_parts[-1])
        prev = rest_parts[-2].lower() if len(rest_parts) > 1 else ""
        if m and prev != "class":
            count = int(m.group(1))
            rest_parts = rest_parts[:-1]

    topic = " ".join(rest_parts).strip() or "सामान्य अध्ययन"
    count = max(1, min(count, 100))

    return {
        "command": command,
        "topic": topic,
        "count": count,
        "schedule_ts": schedule_ts
    }


def command_info(command):
    table = {
        "/quiz": ("quiz", "quiz", "quiz", "Quiz", "🎯", "QUIZ NOW"),
        "/practice": ("practice", "practice", "practice", "Practice", "📝", "PRACTICE NOW"),
        "/pyq": ("practice", "pyq", "pyq", "PYQ Practice", "📚", "PRACTICE NOW"),
        "/mock": ("tests", "test", "mock", "Mock Test", "🎯", "MOCK TEST NOW"),
        "/test": ("tests", "test", "test", "Test Series", "📝", "TEST NOW"),
        "/notes": ("notes", "notes", "notes", "Study Material", "📖", "READ NOTES"),
    }
    folder, box, kind, label, emoji, btn_text = table.get(command, table["/test"])
    return {
        "folder": folder,
        "box": box,
        "type": kind,
        "label": label,
        "emoji": emoji,
        "btn_text": btn_text
    }


def extract_metadata(topic_text):
    class_match = re.search(
        r'\bclass\s*(\d{1,2})\b|\b(\d{1,2})(?:st|nd|rd|th)\b',
        topic_text, flags=re.IGNORECASE)
    if class_match:
        grade = "CLASS " + (class_match.group(1) or class_match.group(2))
        grade_text = class_match.group(0)
    else:
        grade, grade_text = "GENERAL", None

    subject_map = [
        ("hindi grammar", "Hindi Grammar"),
        ("english grammar", "English Grammar"),
        ("general science", "General Science"),
        ("social science", "Social Science"),
        ("physics", "Physics"),
        ("chemistry", "Chemistry"),
        ("biology", "Biology"),
        ("maths", "Maths"),
        ("math", "Maths"),
        ("mathematics", "Maths"),
        ("history", "History"),
        ("geography", "Geography"),
        ("polity", "Polity"),
        ("political science", "Polity"),
        ("economics", "Economics"),
        ("reasoning", "Reasoning"),
        ("computer", "Computer"),
        ("pedagogy", "Pedagogy"),
        ("evs", "EVS"),
        ("hindi", "Hindi"),
        ("english", "English"),
        ("sanskrit", "Sanskrit")
    ]

    found_sub = "General"
    matched_term = None
    for term, label in subject_map:
        if re.search(r'\b' + re.escape(term) + r'\b', topic_text, flags=re.IGNORECASE):
            found_sub = label
            matched_term = term
            break

    clean_topic = topic_text
    if grade_text:
        clean_topic = re.sub(re.escape(grade_text), '', clean_topic, count=1, flags=re.IGNORECASE)
    if matched_term:
        clean_topic = re.sub(r'\b' + re.escape(matched_term) + r'\b', '', clean_topic, flags=re.IGNORECASE)

    clean_topic = re.sub(r"\s+", " ", clean_topic).strip(" -_") or topic_text
    return {"class": grade, "subject": found_sub, "topic": clean_topic}


# -------------------------------------------------------------------- Groq AI
_client = None


def get_client():
    global _client
    if _client is None:
        from groq import Groq
        _client = Groq(api_key=GROQ_API_KEY)
    return _client


PROMPT = """
Gyankshetra परीक्षा ऐप के लिए MCQ तैयार करो।

विषय: __TOPIC__
प्रश्न संख्या: __COUNT__
प्रकार: __MODE__

नियम:
1. सभी प्रश्न उच्च-स्तरीय और परीक्षा उपयोगी हों।
2. प्रश्न हिन्दी में हों।
3. प्रत्येक प्रश्न के ठीक चार विकल्प हों, चारों अलग-अलग।
4. सही उत्तर 0, 1, 2 या 3 (विकल्प की position) हो।
5. प्रत्येक प्रश्न की स्पष्ट व्याख्या हो।
6. प्रत्येक प्रश्न के साथ 8 महत्वपूर्ण तथ्य हों।
7. प्रश्न एक-दूसरे से अलग हों।
8. गलत या मनगढ़ंत तथ्य न बनाओ। जिस तथ्य पर पक्का भरोसा न हो, उस पर प्रश्न मत बनाओ।
9. STET / BPSC TRE स्तर का ध्यान रखो।
10. केवल वैध JSON दो, अतिरिक्त टेक्स्ट या Markdown code fence नहीं।

सिर्फ इस structure में JSON दो:
{"questions":[{"question":"प्रश्न","options":["A","B","C","D"],"answer":0,"explanation":"व्याख्या","facts":["तथ्य 1","तथ्य 2","तथ्य 3","तथ्य 4","तथ्य 5","तथ्य 6","तथ्य 7","तथ्य 8"]}]}
"""

NOTES_PROMPT = """
Gyankshetra Study Material के लिए उच्च-स्तरीय परीक्षा उपयोगी विस्तृत नोट्स हिन्दी में तैयार करो।

विषय: __TOPIC__

नियम:
1. मुख्य परिभाषाएँ, महत्वपूर्ण सूत्र (Formulas), और बिंदु स्पष्ट रूप से हों।
2. STET / BPSC TRE / Board Exam स्तर के महत्वपूर्ण तथ्य और अवधारणाएं शामिल हों।
3. गलत या मनगढ़ंत तथ्य न दो।
4. केवल <div>...</div> टैग्स के अंदर का शुद्ध HTML कोड दें (बिना <html>, <body> या Markdown code fence के)। केवल h2, h3, p, ul, ol, li, b, table, tr, th, td टैग इस्तेमाल करें; style, script या attributes नहीं।
"""


def ask_groq(prompt, is_json=False):
    client = get_client()
    kwargs = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": MAX_TOKENS,
    }
    if "gpt-oss" in GROQ_MODEL:
        # reasoning model: soch me tokens kam lage, jawab ke liye bache
        kwargs["reasoning_effort"] = "low"
    if is_json:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        response = client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""
    except Exception as e:
        raise RuntimeError(f"Groq API Error: {str(e)[:250]}")


def _backoff(err, attempt):
    """Rate limit (429) par lamba wait, baaki errors par chhota."""
    msg = str(err).lower()
    if "429" in msg or "rate" in msg:
        return 20 * (attempt + 1)
    return 2 * (attempt + 1)


def clean_questions(data):
    items = data.get("questions") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("questions array नहीं मिला")
    out = []
    for q in items:
        if not isinstance(q, dict):
            continue
        question = str(q.get("question", "")).strip()
        raw_opts = q.get("options")
        options = (
            [str(x).strip() for x in raw_opts if str(x).strip()]
            if isinstance(raw_opts, list)
            else []
        )
        try:
            answer = int(q.get("answer", q.get("correct", -1)))
        except Exception:
            continue
        if not question or len(options) != 4 or len(set(options)) != 4:
            continue
        if not 0 <= answer <= 3:
            continue

        # LLM sahi jawab aksar option 0/1 me rakhta hai, isliye shuffle
        correct = options[answer]
        random.shuffle(options)
        answer = options.index(correct)

        facts = q.get("facts")
        facts = [str(x).strip() for x in facts if str(x).strip()] if isinstance(facts, list) else []
        explanation = str(q.get("explanation", "")).strip()
        if explanation:
            facts = [explanation] + facts
        out.append([question, options, answer, facts[:9]])
    return out


def groq_batch(topic, count, mode, previous):
    prompt = (
        PROMPT.replace("__COUNT__", str(count))
        .replace("__MODE__", mode)
        .replace("__TOPIC__", topic)  # topic sabse aakhir me, taaki andar ke shabd na badlein
    )
    if previous:
        prompt += "\nइन प्रश्नों को दोबारा न बनाएं:\n" + "\n".join(previous[-30:])

    last_err = None
    for attempt in range(3):
        try:
            raw = ask_groq(prompt, is_json=True).strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
            qs = clean_questions(json.loads(raw))
            if qs:
                return qs
            last_err = "कोई वैध प्रश्न नहीं मिला"
        except Exception as e:
            last_err = e
            print(f"Groq retry {attempt + 1}/3:", e)
        if attempt < 2:
            time.sleep(_backoff(last_err, attempt))
    raise RuntimeError(f"Groq का JSON सही नहीं आया: {str(last_err)[:200]}")


def generate_all(topic, count, mode):
    result, seen = [], []
    max_rounds = (count // BATCH) + 5
    rounds = 0
    while len(result) < count and rounds < max_rounds:
        rounds += 1
        need = min(BATCH, count - len(result))
        try:
            batch_qs = groq_batch(topic, need, mode, seen)
            for q in batch_qs:
                key = re.sub(r"\s+", " ", q[0]).strip().lower()
                if key in seen or len(result) >= count:
                    continue
                seen.append(key)
                result.append(q)
        except Exception as e:
            print(f"Round {rounds} failed: {e}")
            if not result:
                raise
        time.sleep(1)  # Groq tokens-per-minute limit se bachne ke liye
    return result


# ---------------------------------------------------------- HTML sanitizer
NOTES_TAGS = {
    "div", "p", "h1", "h2", "h3", "h4", "ul", "ol", "li", "b", "strong", "i", "em", "u",
    "table", "thead", "tbody", "tr", "td", "th", "br", "hr", "span", "sup", "sub",
}
VOID_TAGS = {"br", "hr"}
DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "noscript", "template"}


class _Sanitizer(HTMLParser):
    """Allowlist sanitizer: sirf upar ke tags rakhta hai, saare attributes
    (onclick, href, style...) hata deta hai, script/style ka content bhi."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in NOTES_TAGS:
            return
        if tag in VOID_TAGS:
            self.out.append(f"<{tag}>")
            return
        self.stack.append(tag)
        self.out.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        if not self.skip and tag in VOID_TAGS:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in NOTES_TAGS or tag in VOID_TAGS:
            return
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def result(self):
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_html(raw):
    s = _Sanitizer()
    s.feed(raw)
    s.close()
    return s.result()


def generate_notes_html(topic):
    raw = ask_groq(NOTES_PROMPT.replace("__TOPIC__", topic)).strip()
    raw = re.sub(r"^```(?:html)?\s*|\s*```$", "", raw)
    raw = sanitize_html(raw)
    if len(re.sub(r"<[^>]+>", "", raw).strip()) < 50:
        raise RuntimeError("Notes खाली या बहुत छोटे आए")
    safe_topic = html.escape(topic)

    return f"""<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{safe_topic} - Study Notes | Gyankshetra</title>
    <style>
        body {{
            font-family: system-ui, -apple-system, sans-serif;
            padding: 20px;
            line-height: 1.6;
            max-width: 850px;
            margin: auto;
            background: #fdfdfd;
            color: #222;
        }}
        h1, h2, h3 {{ color: #1a73e8; }}
        table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
        th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
        th {{ background: #f1f5fd; }}
        .action-bar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            border-bottom: 2px solid #1a73e8;
            padding-bottom: 12px;
        }}
        .back-btn {{
            text-decoration: none;
            color: #1a73e8;
            font-weight: bold;
            font-size: 15px;
        }}
        .print-btn {{
            background: #1a73e8;
            color: #ffffff;
            border: none;
            padding: 8px 16px;
            font-size: 14px;
            font-weight: bold;
            border-radius: 6px;
            cursor: pointer;
            box-shadow: 0 2px 5px rgba(0,0,0,0.15);
        }}
        .content {{
            background: #ffffff;
            padding: 25px;
            border-radius: 8px;
            border: 1px solid #e0e0e0;
        }}
        @media print {{
            body {{ padding: 0; background: #fff; }}
            .action-bar {{ display: none !important; }}
            .content {{ border: none !important; padding: 0 !important; }}
        }}
    </style>
</head>
<body>
    <div class="action-bar">
        <a href="../index.html" class="back-btn">⬅ Back to App</a>
        <button class="print-btn" onclick="window.print()">📥 Download / Print PDF</button>
    </div>
    <div class="header">
        <h1>📖 {safe_topic}</h1>
        <p style="color: #666; margin: 0;">Gyankshetra AI Study Notes</p>
    </div>
    <div class="content">
        {raw}
    </div>
</body>
</html>"""


# ------------------------------------------------------------ Page Builder
def _find_matching(text, start, opening, closing):
    depth, i, n = 0, start, len(text)
    quote_ch, escaped, line_c, block_c = None, False, False, False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if line_c:
            line_c = ch != "\n"
        elif block_c:
            if ch == "*" and nxt == "/":
                block_c = False
                i += 1
        elif quote_ch:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote_ch:
                quote_ch = None
        elif ch in ("'", '"', "`"):
            quote_ch = ch
        elif ch == "/" and nxt == "/":
            line_c = True
            i += 1
        elif ch == "/" and nxt == "*":
            block_c = True
            i += 1
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def js_safe(obj):
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _patch(source, old, new):
    """Template me old text na mile to saaf error (template badla hoga)."""
    if old not in source:
        raise RuntimeError("index.html में यह हिस्सा नहीं मिला: " + old[:60])
    return source.replace(old, new, 1)


ESC_HELPER = (
    "function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')"
    ".replace(/>/g,'&gt;').replace(/\"/g,'&quot;')}\n    let currentIndex = 0;"
)
FACTS_HTML = (
    "<div class=\"status-badge\">स्थिति: <b>${q.status.toUpperCase()}</b></div>\n"
    "          ${q.facts && q.facts.length ? '<div class=\"status-badge\"><b>📌 व्याख्या / तथ्य:</b>"
    "<ul style=\"margin:6px 0 0 18px\">' + q.facts.map(f => '<li>' + esc(f) + '</li>').join('') "
    "+ '</ul></div>' : ''}"
)


TEST_TEMPLATE = r"""<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gyankshetra</title>
<style>
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f4f6f9;color:#172033}
.top{background:#111827;color:#fff;padding:12px 14px;display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:5}
.top a{color:#fff;text-decoration:none;font-size:20px}.top .title{flex:1;font-weight:800;font-size:15px}
.wrap{max-width:620px;margin:auto;padding:14px}.card{background:#fff;border-radius:16px;padding:16px;box-shadow:0 3px 14px #0000000d;margin-bottom:12px}
.line{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #e5e9f0;padding-bottom:10px;margin-bottom:12px;font-weight:800}
.timer{color:#d97706}.q{font-size:17px;font-weight:800;line-height:1.55;margin:6px 0 14px}
.opt{display:block;width:100%;text-align:left;background:#fff;border:1.5px solid #d5dce6;border-radius:12px;padding:13px;margin-bottom:9px;font-size:15px}
.opt.sel{border-color:#075fc3;background:#e8f1fd}.opt.ok{border-color:#1f9d55;background:#e7f7ee}.opt.bad{border-color:#e03a3a;background:#fdecec}
.row{display:flex;gap:10px}.btn{flex:1;border:0;border-radius:12px;padding:13px;font-weight:800;font-size:15px}
.pri{background:#075fc3;color:#fff}.sec{background:#fff;border:1.5px solid #d5dce6;color:#172033}.grn{background:#16834f;color:#fff}.tg{background:#229ed9;color:#fff}
.pal{background:none;border:0;font-size:22px}.hide{display:none}
.donut{width:150px;height:150px;border-radius:50%;margin:16px auto;display:flex;align-items:center;justify-content:center;position:relative;background:#c9d1dc}
.donut:before{content:"";position:absolute;inset:16px;background:#f4f6f9;border-radius:50%}.donut b{position:relative;font-size:28px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;text-align:center}.grid4 div{background:#fff;border-radius:12px;padding:10px 2px}.grid4 b{display:block;font-size:17px}.grid4 small{font-size:11px;color:#667085}
.rv{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:12px 0}.rv button{border:0;border-radius:10px;padding:11px 2px;font-weight:800;font-size:12px;background:#e3f0ff;color:#075fc3}
.facts{background:#f1f5fb;border-radius:12px;padding:10px 12px;margin-top:10px;font-size:14px;line-height:1.6}.facts ul{margin:6px 0 0 18px;padding:0}
.modal{display:none;position:fixed;inset:0;background:#0007;align-items:center;justify-content:center;z-index:20}.modal.open{display:flex}
.box{background:#fff;border-radius:16px;padding:16px;width:88%;max-width:380px}.pg{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:12px 0}
.pg button{border:0;border-radius:10px;padding:12px 0;font-weight:800;background:#e6ebf2}.pg .a{background:#1f9d55;color:#fff}.pg .c{outline:3px solid #075fc3}
small.m{color:#667085}
</style>
</head>
<body>
<div class="top"><a href="../index.html" id="back">←</a><div class="title" id="ttl">Gyankshetra</div></div>
<div class="wrap">
 <section id="quiz">
  <div class="card">
   <div class="line"><button class="pal" onclick="openPal()">☰</button><span id="qc"></span><span class="timer" id="tm"></span></div>
   <div class="q" id="qt"></div><div id="opts"></div>
   <div class="row"><button class="btn sec" id="pv" onclick="go(-1)">← पिछला</button><button class="btn pri" id="nx" onclick="go(1)">अगला →</button></div>
  </div>
 </section>
 <section id="result" class="hide">
  <h2 style="margin:6px 0">🏆 परिणाम</h2><p id="who" style="text-align:center;font-weight:800;margin:0"></p>
  <div class="donut" id="donut"><b id="pct">0%</b></div>
  <div class="grid4"><div><b id="mk"></b><small>अंक</small></div><div><b id="co"></b><small>सही</small></div><div><b id="wr"></b><small>गलत</small></div><div><b id="sk"></b><small>छूटे</small></div></div>
  <div class="rv"><button onclick="review('all')">📖 सभी</button><button onclick="review('ok')">✅ सही</button><button onclick="review('bad')">❌ गलत</button><button onclick="review('skip')">⏭ छूटे</button></div>
  <div class="row" style="margin-bottom:10px"><button class="btn grn" onclick="location.reload()">🔁 Reattempt</button><button class="btn tg" onclick="share()">📤 Result Share करें</button></div>
  <a href="../index.html" class="btn sec" style="display:block;text-align:center;text-decoration:none">⬅ Back to App</a>
 </section>
 <section id="review" class="hide">
  <div class="card"><div class="line"><button class="pal" onclick="backRes()">←</button><span id="rc"></span><span></span></div>
   <div class="q" id="rq"></div><div id="ro"></div><div id="rf"></div>
   <div class="row" style="margin-top:12px"><button class="btn sec" onclick="rgo(-1)">← Previous</button><button class="btn pri" onclick="rgo(1)">Next →</button></div>
  </div>
 </section>
</div>
<div class="modal" id="pal" onclick="closePal()"><div class="box" onclick="event.stopPropagation()"><b>प्रश्न पैलेट</b><div class="pg" id="pg"></div><button class="btn pri" style="width:100%" onclick="closePal()">बंद करें</button></div></div>
<script>
const testData = __TESTDATA__;
const Q=testData.questions,N=Q.length;let ans=Array(N).fill(null),qi=0,left=N*60,tid=null,done=false,rOrder=[],ri=0,res={c:0,w:0,s:0};
const $=id=>document.getElementById(id);
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function fmt(t){t=Math.max(0,t);return String(Math.floor(t/60)).padStart(2,'0')+':'+String(t%60).padStart(2,'0')}
function uname(){try{if(localStorage.getItem('gyankshetraLoggedIn')==='1'){return localStorage.getItem('gyankshetraUserName')||testData.studentName}}catch(e){}return testData.studentName}
$('ttl').textContent='Gyankshetra · '+testData.topic;
function render(){const q=Q[qi];$('qc').textContent='प्रश्न '+(qi+1)+'/'+N;$('qt').textContent=q.text;
$('opts').innerHTML=q.options.map((o,i)=>'<button class="opt '+(ans[qi]===i?'sel':'')+'" onclick="pick('+i+')">'+String.fromCharCode(65+i)+'. '+esc(o)+'</button>').join('');
$('pv').disabled=qi===0;$('nx').textContent=qi===N-1?'Submit Test ✔':'अगला →';$('tm').textContent='⏱ '+fmt(left)}
function pick(i){ans[qi]=i;render()}
function go(d){if(d>0&&qi===N-1){if(confirm('क्या आप टेस्ट Submit करना चाहते हैं?'))finish();return}const n=qi+d;if(n<0||n>=N)return;qi=n;render()}
function tick(){left--;$('tm').textContent='⏱ '+fmt(left);if(left<=0)finish()}
function openPal(){$('pg').innerHTML=Q.map((q,i)=>'<button class="'+(ans[i]!==null?'a ':'')+(i===qi?'c':'')+'" onclick="jump('+i+')">'+(i+1)+'</button>').join('');$('pal').classList.add('open')}
function closePal(){$('pal').classList.remove('open')}
function jump(i){qi=i;closePal();render()}
function finish(){if(done)return;done=true;clearInterval(tid);closePal();let c=0,w=0,s=0;ans.forEach((a,i)=>{if(a===null)s++;else if(a===Q[i].correctOption)c++;else w++});res={c,w,s};
const a=c/N*100,b=a+w/N*100;$('donut').style.background='conic-gradient(#1f9d55 0 '+a+'%,#e03a3a '+a+'% '+b+'%,#c9d1dc '+b+'% 100%)';
$('pct').textContent=Math.round(a)+'%';$('mk').textContent=c+' / '+N;$('co').textContent=c;$('wr').textContent=w;$('sk').textContent=s;$('who').textContent='👤 '+uname();
try{const h=JSON.parse(localStorage.getItem('gyankshetraHistory')||'[]');h.push({day:new Date().toISOString().slice(0,10),date:new Date().toLocaleString('hi-IN'),test:testData.topic,marks:c,correct:c,wrong:w,skipped:s,total:N});localStorage.setItem('gyankshetraHistory',JSON.stringify(h))}catch(e){}
$('quiz').classList.add('hide');$('result').classList.remove('hide');window.scrollTo(0,0)}
function review(f){rOrder=Q.map((q,i)=>i).filter(i=>f==='all'||(f==='ok'&&ans[i]===Q[i].correctOption)||(f==='bad'&&ans[i]!==null&&ans[i]!==Q[i].correctOption)||(f==='skip'&&ans[i]===null));if(!rOrder.length){alert('इस श्रेणी में कोई प्रश्न नहीं है।');return}ri=0;$('result').classList.add('hide');$('review').classList.remove('hide');rrender()}
function rrender(){const i=rOrder[ri],q=Q[i];$('rc').textContent='Review '+(ri+1)+'/'+rOrder.length;$('rq').textContent=(i+1)+'. '+q.text;
$('ro').innerHTML=q.options.map((o,k)=>'<div class="opt '+(k===q.correctOption?'ok':(k===ans[i]?'bad':''))+'">'+String.fromCharCode(65+k)+'. '+esc(o)+(k===q.correctOption?' ✅':'')+(k===ans[i]&&k!==q.correctOption?' ❌':'')+'</div>').join('');
$('rf').innerHTML='<small class="m">आपका उत्तर: '+(ans[i]===null?'छोड़ा गया':String.fromCharCode(65+ans[i]))+' • सही उत्तर: '+String.fromCharCode(65+q.correctOption)+'</small>'+(q.facts&&q.facts.length?'<div class="facts"><b>📌 व्याख्या / तथ्य:</b><ul>'+q.facts.map(f=>'<li>'+esc(f)+'</li>').join('')+'</ul></div>':'')}
function rgo(d){const n=ri+d;if(n<0||n>=rOrder.length)return;ri=n;rrender()}
function backRes(){$('review').classList.add('hide');$('result').classList.remove('hide')}
function share(){const link=location.href.split('#')[0].split('?')[0],p=Math.round(res.c/N*100);
const t='🎓 Gyankshetra Result\n👤 '+uname()+'\n📝 '+testData.topic+'\n🏆 अंक: '+res.c+'/'+N+' ('+p+'%)\n✅ सही: '+res.c+'  ❌ गलत: '+res.w+'  ⏭ छूटे: '+res.s+'\n📢 Telegram चैनल: https://t.me/gyankshetra\n🔗 टेस्ट लिंक: '+link;
window.open('https://t.me/share/url?url='+encodeURIComponent(link)+'&text='+encodeURIComponent(t),'_blank')}
(function(){let x0=0,y0=0;const w=$('quiz');w.addEventListener('touchstart',e=>{x0=e.touches[0].clientX;y0=e.touches[0].clientY},{passive:true});
w.addEventListener('touchend',e=>{const dx=e.changedTouches[0].clientX-x0,dy=e.changedTouches[0].clientY-y0;if(Math.abs(dx)<50||Math.abs(dx)<Math.abs(dy)*1.3)return;if(dx>0){if(qi>0){qi--;render()}}else if(qi<N-1){qi++;render()}},{passive:true})})();
render();tid=setInterval(tick,1000);
</script>
</body>
</html>
"""


def make_test_page(topic, count, questions, test_id):
    """Bot ke andar rakhe template se naya test page banata hai (ab index.html par nirbhar nahi)."""
    data = {
        "isReattempted": False,
        "studentName": "छात्र",
        "topic": topic,
        "questions": [
            {
                "id": i + 1,
                "text": q[0],
                "options": q[1],
                "correctOption": q[2],
                "selected": None,
                "status": "skipped",
                "facts": q[3],
            }
            for i, q in enumerate(questions)
        ],
    }
    safe = html.escape(topic)
    s = TEST_TEMPLATE.replace("__TESTDATA__", js_safe(data))
    s = re.sub(r"<title>.*?</title>", lambda m: f"<title>{safe} | Gyankshetra</title>", s, count=1, flags=re.S)
    return s


def update_registry(topic, count, command, url, item_id):
    os.makedirs("content", exist_ok=True)
    info = command_info(command)
    meta = extract_metadata(topic)

    entry = {
        "id": item_id,
        "title": f"{info['emoji']} {topic}",
        "class": meta["class"],
        "subject": meta["subject"],
        "topic": meta["topic"],
        "category": info["label"],
        "type": info["type"],
        "url": url,
        "btn_text": info["btn_text"],
        "created_at": item_id
    }

    target_file = STUDY_MATERIAL_FILE if command == "/notes" else REGISTRY_FILE
    if command != "/notes":
        entry["questions"] = count

    try:
        with open(target_file, "r", encoding="utf-8") as f:
            registry = json.load(f)
        if not isinstance(registry, list):
            registry = []
    except Exception:
        registry = []

    registry.insert(0, entry)
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(registry[:500], f, ensure_ascii=False, indent=2)


def git_publish(message):
    if not os.environ.get("GITHUB_ACTIONS"):
        return True
    try:
        run = lambda *a: subprocess.run(a, check=True)
        run("git", "config", "user.name", "gyankshetra")
        run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        dirs = [d for d in ("content", "tests", "quiz", "practice", "notes") if os.path.isdir(d)]
        run("git", "add", *dirs)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return True
        run("git", "commit", "-m", message)
        run("git", "pull", "--rebase", "--autostash")
        run("git", "push")
        return True
    except Exception as e:
        print("Git push error:", e)
        return False


def safe_slug(text):
    text = re.sub(r"[^\w\u0900-\u097F]+", "-", text, flags=re.UNICODE).strip("-")
    return (text or "test")[:70]


def load_offset():
    try:
        with open(OFFSET_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def save_offset(offset):
    os.makedirs("content", exist_ok=True)
    with open(OFFSET_FILE, "w", encoding="utf-8") as f:
        f.write(str(offset))


# ------------------------------------------------------------------- Handler
def process_update(update):
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return

    word = text.split()[0].lower().split("@")[0]
    if word == "/id":
        send_message(chat_id, f"आपका chat id: {chat_id}")
        return

    if ALLOWED and str(chat_id) not in ALLOWED:
        send_message(chat_id, "यह bot निजी है।")
        return

    cmd = parse_command(text)
    if not cmd:
        return

    topic, count = cmd["topic"], cmd["count"]
    schedule_ts = cmd["schedule_ts"]
    info = command_info(cmd["command"])
    safe_topic = html.escape(topic)

    # --- Notes Flow ---
    if cmd["command"] == "/notes":
        send_message(chat_id, f"🤖 Groq AI <b>{safe_topic}</b> के लिए Study Material तैयार कर रहा है...\n\nथोड़ा समय लगेगा...")
        try:
            page = generate_notes_html(topic)
            item_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            filename = f"{safe_slug(topic)}-notes-{item_id}.html"
            folder = "notes"
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, filename), "w", encoding="utf-8") as f:
                f.write(page)

            file_url = BASE_URL + folder + "/" + quote(filename)
            update_registry(topic, 0, cmd["command"], file_url, item_id)
            if not git_publish(f"Generate notes: {topic}"):
                raise RuntimeError("GitHub पर publish नहीं हो पाया, link काम नहीं करेगा")

            formatted_message = (
                "✅ <b>Study Material तैयार है!</b>\n\n"
                f"🎯 <b>विषय:</b> {safe_topic}\n"
                f"📌 <b>प्रकार:</b> Study Material\n\n"
                f'<a href="{html.escape(file_url, quote=True)}">🔗 READ NOTES</a>'
            )
            send_message(chat_id, formatted_message)
            if CHANNEL_ID:
                send_message(CHANNEL_ID, formatted_message)
        except Exception as e:
            send_message(chat_id, f"❌ Notes तैयार नहीं हो पाए: {html.escape(str(e)[:1500])}")
        return

    # --- Test/Quiz Flow ---
    send_message(
        chat_id,
        f"🤖 Groq AI प्रश्न तैयार कर रहा है...\n\n{info['emoji']} <b>विषय:</b> {safe_topic}\n"
        f"📌 <b>प्रकार:</b> {info['label']}\n📝 <b>प्रश्न:</b> {count}\n\nथोड़ा समय लगेगा..."
    )
    try:
        questions = generate_all(topic, count, info["label"])
        if not questions:
            raise RuntimeError("कोई प्रश्न नहीं बन पाया।")

        # kam sawal bane to page me asli count likho (timer bhi usi se banta hai)
        requested = count
        count = len(questions)
        shortfall = (
            f"\n⚠️ माँगे गए {requested} में से {count} प्रश्न ही बन पाए।"
            if count < requested else ""
        )

        test_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"{safe_slug(topic)}-{count}q-{test_id}.html"
        folder = info["folder"]
        os.makedirs(folder, exist_ok=True)

        page = make_test_page(topic, count, questions, test_id)
        with open(os.path.join(folder, filename), "w", encoding="utf-8") as f:
            f.write(page)

        test_url = BASE_URL + folder + "/" + quote(filename)
        update_registry(topic, count, cmd["command"], test_url, test_id)
        if not git_publish(f"Generate test: {topic} ({count}Q)"):
            raise RuntimeError("GitHub पर publish नहीं हो पाया, link काम नहीं करेगा")

        formatted_message = (
            "✅ <b>Test तैयार है!</b>\n\n"
            f"🎯 <b>विषय:</b> {safe_topic}\n"
            f"📌 <b>प्रकार:</b> {info['label']}\n"
            f"📝 <b>प्रश्न:</b> {count}\n\n"
            f'<a href="{html.escape(test_url, quote=True)}">🔗 {info["btn_text"]}</a>'
        )

        current_ts = datetime.now(timezone.utc).timestamp()
        if schedule_ts and schedule_ts > current_ts:
            send_message(
                chat_id,
                "📅 टेस्ट तैयार है और तय समय पर चैनल में पब्लिश हो जाएगा!" + shortfall
                + "\n\n" + formatted_message
            )
            if CHANNEL_ID:
                schedule_channel_post(CHANNEL_ID, formatted_message, schedule_ts)
        else:
            send_message(
                chat_id,
                formatted_message + shortfall
                + "\n\n(GitHub Pages पर लिंक चालू होने में 1-2 मिनट लग सकते हैं)"
            )
            if CHANNEL_ID:
                send_message(CHANNEL_ID, formatted_message)

    except Exception as e:
        send_message(chat_id, f"❌ Test generate नहीं हो पाया\n\nError:\n{html.escape(str(e)[:1500])}")


def main():
    print("Gyankshetra Telegram Bot started with Groq, model:", GROQ_MODEL)
    if not ALLOWED:
        print("WARNING: TELEGRAM_ALLOWED_CHAT_IDS khali hai, koi bhi bot use kar sakta hai")

    try:
        run_due_posts()
    except Exception as e:
        print("Error running due posts:", e)

    offset = load_offset()
    try:
        res = telegram("getUpdates", {"offset": offset, "timeout": 10})
        for u in res.get("result", []):
            offset = max(offset, u["update_id"] + 1)
            save_offset(offset)
            try:
                process_update(u)
            except Exception as e:
                print("process_update failed:", e)
    except Exception as e:
        print("Error fetching updates:", e)

    git_publish("Update bot state")


if __name__ == "__main__":
    main()
