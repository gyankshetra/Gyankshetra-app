#!/usr/bin/env python3
"""Gyankshetra Telegram bot (Gemini)
Automatic Quiz Generator, Notes Generator & Scheduled Channel Publisher
"""
import os
import re
import json
import time
import html
import subprocess
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]

# Telegram Channel ID ya Username
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "@Gyankshetra")

ALLOWED = {
    x.strip()
    for x in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
    if x.strip()
}

BASE_URL = "https://gyankshetra.github.io/Gyankshetra-app/"
TG = "https://api.telegram.org/bot" + BOT_TOKEN
OFFSET_FILE = "content/telegram_offset.txt"
REGISTRY_FILE = "content/generated_tests.json"
STUDY_MATERIAL_FILE = "content/study_material.json"
SCHEDULE_FILE = "content/scheduled_posts.json"

BATCH = 25


# ------------------------------------------------------------------ Telegram
def telegram(method, data=None):
    r = requests.post(TG + "/" + method, json=data or {}, timeout=60)
    r.raise_for_status()
    return r.json()


def send_message(chat_id, text, parse_mode="HTML"):
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
            clean_text = re.sub(r'<[^>]+>', '', text)
            return telegram("sendMessage", {"chat_id": chat_id, "text": clean_text})
        except Exception:
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
    queue.append({"chat_id": chat_id, "text": text, "ts": target_timestamp})
    _save_queue(queue)


def run_due_posts():
    queue = _load_queue()
    if not queue:
        return
    now = datetime.now(timezone.utc).timestamp()
    pending = []
    for p in queue:
        if p.get("ts", 0) <= now:
            send_message(p["chat_id"], p["text"], parse_mode="HTML")
        else:
            pending.append(p)
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


# -------------------------------------------------------------------- Gemini
_client = None


def get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=GEMINI_KEY)
    return _client


PROMPT = """
Gyankshetra परीक्षा ऐप के लिए MCQ तैयार करो।

विषय: TOPIC
प्रश्न संख्या: COUNT
प्रकार: MODE

नियम:
1. सभी प्रश्न उच्च-स्तरीय और परीक्षा उपयोगी हों।
2. प्रश्न हिन्दी में हों।
3. प्रत्येक प्रश्न के ठीक चार विकल्प हों, चारों अलग-अलग।
4. सही उत्तर 0, 1, 2 या 3 (विकल्प की position) हो।
5. प्रत्येक प्रश्न की स्पष्ट व्याख्या हो।
6. प्रत्येक प्रश्न के साथ 8 महत्वपूर्ण तथ्य हों।
7. प्रश्न एक-दूसरे से अलग हों।
8. गलत या मनगढ़ंत तथ्य न बनाओ।
9. STET / BPSC TRE स्तर का ध्यान रखो।
10. केवल JSON दो, Markdown code fence नहीं।

सिर्फ इस structure में JSON दो:
{"questions":[{"question":"प्रश्न","options":["A","B","C","D"],"answer":0,"explanation":"व्याख्या","facts":["तथ्य 1","तथ्य 2","तथ्य 3","तथ्य 4","तथ्य 5","तथ्य 6","तथ्य 7","तथ्य 8"]}]}
"""

NOTES_PROMPT = """
Gyankshetra Study Material के लिए उच्च-स्तरीय परीक्षा उपयोगी विस्तृत नोट्स हिन्दी में तैयार करो।

विषय: TOPIC

नियम:
1. मुख्य परिभाषाएँ, महत्वपूर्ण सूत्र (Formulas), और बिंदु स्पष्ट रूप से हों।
2. STET / BPSC TRE / Board Exam स्तर के महत्वपूर्ण तथ्य और अवधारणाएं शामिल हों।
3. केवल <div>...</div> टैग्स के अंदर का शुद्ध HTML कोड दें (बिना <html>, <body> या Markdown code fence के)।
"""

FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get("GEMINI_MODELS", "gemini-3.8-flash,gemini-2.5-flash").split(",")
    if m.strip()
]


def ask_gemini(prompt, is_json=False):
    from google.genai import types
    config = types.GenerateContentConfig(temperature=0.3)
    if is_json:
        config.response_mime_type = "application/json"

    last_err = ""
    for model_name in FALLBACK_MODELS:
        try:
            resp = get_client().models.generate_content(
                model=model_name,
                contents=prompt,
                config=config,
            )
            if resp and resp.text:
                return resp.text
        except Exception as e:
            last_err = str(e)
            print(f"Model {model_name} failed: {e}")
            time.sleep(1)
            continue

    raise RuntimeError(f"Gemini API Error: {last_err[:250]}")


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
        facts = q.get("facts")
        facts = [str(x).strip() for x in facts if str(x).strip()] if isinstance(facts, list) else []
        explanation = str(q.get("explanation", "")).strip()
        if explanation:
            facts = [explanation] + facts
        out.append([question, options, answer, facts[:9]])
    return out


def gemini_batch(topic, count, mode, previous):
    prompt = PROMPT.replace("TOPIC", topic).replace("COUNT", str(count)).replace("MODE", mode)
    if previous:
        prompt += "\nइन प्रश्नों को दोबारा न बनाएं:\n" + "\n".join(previous[-30:])
    for _ in range(3):
        raw = ask_gemini(prompt, is_json=True).strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        try:
            qs = clean_questions(json.loads(raw))
            if qs:
                return qs
        except Exception as e:
            print("Bad Gemini JSON:", e)
    raise RuntimeError("Gemini का JSON सही नहीं आया")


def generate_all(topic, count, mode):
    result, seen = [], []
    max_rounds = (count // BATCH) + 5
    rounds = 0
    while len(result) < count and rounds < max_rounds:
        rounds += 1
        need = min(BATCH, count - len(result))
        try:
            batch_qs = gemini_batch(topic, need, mode, seen)
            for q in batch_qs:
                key = re.sub(r"\s+", " ", q[0]).strip().lower()
                if key in seen or len(result) >= count:
                    continue
                seen.append(key)
                result.append(q)
        except Exception as e:
            print(f"Round {rounds} failed: {e}")
            if result:
                continue
            raise e
    return result


def generate_notes_html(topic):
    raw = ask_gemini(NOTES_PROMPT.replace("TOPIC", topic)).strip()
    raw = re.sub(r"^```(?:html)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
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


def replace_questions(source, placeholder):
    m = re.search(r"\bconst\s+questions\s*=", source)
    if not m:
        raise RuntimeError("index.html में 'const questions=' नहीं मिला")
    start = source.find("[", m.end())
    end = _find_matching(source, start, "[", "]") if start >= 0 else -1
    if end < 0:
        raise RuntimeError("index.html में questions array का अंत नहीं मिला")
    return source[: m.start()] + "const questions=" + placeholder + source[end + 1:]


def replace_function(source, name, replacement):
    m = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", source)
    if not m:
        raise RuntimeError("index.html में function " + name + "() नहीं मिला")
    end = _find_matching(source, m.end() - 1, "{", "}")
    if end < 0:
        raise RuntimeError("index.html में function " + name + "() का अंत नहीं मिला")
    return source[: m.start()] + replacement + source[end + 1:]


START_QUIZ = (
    "function startQuiz(){qi=0;ans=Array(TOTAL).fill(null);time=0;reviewIndex=0;"
    "running=true;clearInterval(timerHandle);go('quiz');render();saveProgress();"
    "timerHandle=setInterval(tick,1000)}"
)
TICK = (
    "function tick(){if(time<TIME){time++;renderTimer();"
    "if(time>=TIME)finish();else if(time%5===0)saveProgress()}else finish()}"
)


def js_safe(obj):
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def make_test_page(topic, count, questions, test_id):
    with open("index.html", "r", encoding="utf-8") as f:
        s = f.read()

    s = re.sub(
        r"<!-- GYANKSHETRA_GENERATED_CONTENT_START -->.*?<!-- GYANKSHETRA_GENERATED_CONTENT_END -->",
        "", s, flags=re.S)
    s = re.sub(
        r"<!-- GYANKSHETRA_AUTO_GENERATED_TESTS -->\s*<script>.*?</script>",
        "", s, flags=re.S)

    s, n = re.subn(
        r"const\s+TOTAL\s*=\s*\d+\s*,\s*TIME\s*=\s*[^,;]+",
        f"const TOTAL={count},TIME={count * 60}", s, count=1)
    if not n:
        raise RuntimeError("index.html में 'const TOTAL=..,TIME=..' नहीं मिला")
    s = replace_questions(s, "__GK_QUESTIONS__")
    s = replace_function(s, "startQuiz", START_QUIZ)
    s = replace_function(s, "tick", TICK)

    for old, new in (
        ("'gyankshetraSaved'", f"'gyankshetraSaved_{test_id}'"),
        ("'gyankshetraProgress_'", f"'gyankshetraProgress_{test_id}_'"),
    ):
        s = s.replace(old, new)

    s = s.replace("test:'विलयन'", "test:__GK_TOPIC_JS__")
    s = s.replace("विलयन", "__GK_TOPIC_HTML__")

    for old, new in (
        ("20Q", f"{count}Q"),
        ("20 Questions", f"{count} Questions"),
        ("20 Marks", f"{count} Marks"),
        ("20 Minutes", f"{count} Minutes"),
        ("20 प्रश्न", f"{count} प्रश्न"),
        ("20 अंक", f"{count} अंक"),
        ("20 मिनट", f"{count} मिनट"),
        ("' / 20'", "' / '+TOTAL"),
        ("marks}/20`", "marks}/${TOTAL}`"),
        ("0 / 20</strong>", f"0 / {count}</strong>"),
        ("0 / 20</b>", f"0 / {count}</b>"),
        ('id="timer">20:00', 'id="timer">00:00'),
        (">1/20<", f">1/{count}<"),
    ):
        s = s.replace(old, new)

    s = s.replace("__GK_TOPIC_JS__", js_safe(topic))
    s = s.replace("__GK_TOPIC_HTML__", html.escape(topic))
    s = s.replace("__GK_QUESTIONS__", js_safe(questions))
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

    # --- Notes Flow ---
    if cmd["command"] == "/notes":
        send_message(chat_id, f"🤖 Gemini AI <b>{html.escape(topic)}</b> के लिए Study Material तैयार कर रहा है...\n\nथोड़ा समय लगेगा...", parse_mode="HTML")
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
            git_publish(f"Generate notes: {topic}")

            formatted_message = (
                "✅ <b>Study Material तैयार है!</b>\n\n"
                f"🎯 <b>विषय:</b> {html.escape(topic)}\n"
                f"📌 <b>प्रकार:</b> Study Material\n\n"
                f'<a href="{file_url}">🔗 READ NOTES</a>'
            )
            send_message(chat_id, formatted_message, parse_mode="HTML")
            if CHANNEL_ID:
                send_message(CHANNEL_ID, formatted_message, parse_mode="HTML")
        except Exception as e:
            send_message(chat_id, f"❌ Notes तैयार नहीं हो पाए: {html.escape(str(e))}", parse_mode="HTML")
        return

    # --- Test/Quiz Flow ---
    send_message(
        chat_id,
        f"🤖 Gemini AI प्रश्न तैयार कर रहा है...\n\n{info['emoji']} <b>विषय:</b> {html.escape(topic)}\n📌 <b>प्रकार:</b> {info['label']}\n📝 <b>प्रश्न:</b> {count}\n\nथोड़ा समय लगेगा...",
        parse_mode="HTML"
    )
    try:
        questions = generate_all(topic, count, info["label"])
        if len(questions) < count:
            raise RuntimeError(f"माँगे गए {count} में केवल {len(questions)} प्रश्न बन पाए।")

        test_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"{safe_slug(topic)}-{count}q-{test_id}.html"
        folder = info["folder"]
        os.makedirs(folder, exist_ok=True)
        page = make_test_page(topic, count, questions, test_id)

        with open(os.path.join(folder, filename), "w", encoding="utf-8") as f:
            f.write(page)

        test_url = BASE_URL + folder + "/" + quote(filename)
        update_registry(topic, count, cmd["command"], test_url, test_id)
        git_publish(f"Generate test: {topic} ({count}Q)")

        formatted_message = (
            "✅ <b>Test तैयार है!</b>\n\n"
            f"🎯 <b>विषय:</b> {html.escape(topic)}\n"
            f"📌 <b>प्रकार:</b> {info['label']}\n"
            f"📝 <b>प्रश्न:</b> {count}\n\n"
            f'<a href="{test_url}">🔗 {info["btn_text"]}</a>'
        )

        current_ts = datetime.now(timezone.utc).timestamp()

        if schedule_ts and schedule_ts > current_ts:
            send_message(chat_id, "📅 टेस्ट तैयार है और तय समय पर चैनल में पब्लिश हो जाएगा!\n\n" + formatted_message, parse_mode="HTML")
            if CHANNEL_ID:
                schedule_channel_post(CHANNEL_ID, formatted_message, schedule_ts)
        else:
            send_message(chat_id, formatted_message, parse_mode="HTML")
            if CHANNEL_ID:
                send_message(CHANNEL_ID, formatted_message, parse_mode="HTML")

    except Exception as e:
        send_message(chat_id, f"❌ Test generate नहीं हो पाया\n\nError:\n{html.escape(str(e)[:2000])}", parse_mode="HTML")


def main():
    print("Gyankshetra Telegram Bot started")

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
