#!/usr/bin/env python3
"""Gyankshetra Telegram bot (Groq AI)
Automatic Quiz Generator, Notes Generator & Scheduled Channel Publisher
"""
import io
import base64
import difflib
import os
import re
import json
import time
import html
import random
import subprocess
from html.parser import HTMLParser
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, unquote

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
    Text me ⟦BTN|naam|url⟧ ho to unke inline URL button ban jate hain.
    Success par response dict, fail par None return karta hai."""
    text, btns = _split_buttons(text)
    text = text[:TG_LIMIT]
    try:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": False
        }
        if btns:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": n, "url": u}] for n, u in btns[:100]]}
        return telegram("sendMessage", payload)
    except Exception as e:
        print(f"sendMessage failed for {chat_id}:", e)
        try:
            clean_text = html.unescape(re.sub(r'<[^>]+>', '', text))
            if btns:   # button na chale to link text me dikha do
                clean_text += "\n\n" + "\n".join(u for _, u in btns)
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



# ------------------------------------------------ Mixed (multi-subject) tests
_Q_WORD = r"(?:q|mcqs?|questions?|prashn|प्रश्न|सवाल)"
_BAD_NAMES = {"class", "chapter", "unit", "part", "lesson", "ch", "std", "paper", "set", "year", "ncert", "bpsc", "stet", "tre"}


def _clean_section_name(chunk):
    name = re.sub(r"\d{1,3}", " ", chunk)
    name = re.sub(r"\b" + _Q_WORD + r"\b", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"[:\-–—()\[\]\.]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def _parse_pairs(t):
    chunks = [c.strip() for c in re.split(r"[,;\n+|]", t) if c.strip()]
    pairs = []
    if len(chunks) >= 2:
        for c in chunks:
            nums = re.findall(r"\d{1,3}", c)
            name = _clean_section_name(c)
            if len(nums) != 1 or not name:
                return None
            pairs.append((name, int(nums[0])))
    else:
        # bina comma: 'history 8 geo 8 math 8'  ya  '8q history 8q geo'
        if re.match(r"\s*\d", t):
            found = re.findall(r"(\d{1,3})\s*(?:" + _Q_WORD + r")?\s*([^\d]+)", t, flags=re.IGNORECASE)
            pairs = [(_clean_section_name(n), int(c)) for c, n in found]
        else:
            found = re.findall(r"([^\d]+?)\s*(\d{1,3})\s*(?:" + _Q_WORD + r")?(?=\s|$)", t, flags=re.IGNORECASE)
            pairs = [(_clean_section_name(n), int(c)) for n, c in found]
        if len(pairs) < 2:
            return None
    for name, n in pairs:
        last = name.split()[-1].lower() if name.split() else ""
        if not name or last in _BAD_NAMES or n < 1 or n > 100:
            return None
    return pairs


def parse_sections(text):
    """'history 8, geo 8, math 8' ya '8q history, 8q geo' jaise text se
    [(naam, count), ...] banata hai. Normal '/test topic 20' ko nahi chhedta."""
    t = (text or "").strip()
    if not t:
        return None
    # shuru me total ("40 mcq ...", "40 questions ...") ho to pehle use hata kar dekho
    strong = re.match(r"\s*(\d{1,3})\s*(?:mcqs?|questions?|prashn|प्रश्न|सवाल)\b", t, flags=re.IGNORECASE)
    if strong:
        pairs = _parse_pairs(t[strong.end():].strip(" ,;:-"))
        if pairs:
            return pairs
    pairs = _parse_pairs(t)
    if pairs:
        return pairs
    weak = re.match(r"\s*(\d{1,3})\s*q\b", t, flags=re.IGNORECASE)  # '40q history 8, geo 8'
    if weak:
        return _parse_pairs(t[weak.end():].strip(" ,;:-"))
    return None


def generate_sections(sections, mode, note=""):
    """Har vishay ke apne prashn alag bana kar ek list me jodta hai."""
    out = []
    for i, (name, n) in enumerate(sections):
        print(f"Section {i + 1}/{len(sections)}: {name} x{n}")
        out.extend(generate_all(name + note, n, mode))
    return out


_DIFF_RE = re.compile(r"\b(easy|moderate|medium|hard|difficult)\b|आसान|मध्यम|कठिन", re.IGNORECASE)


def extract_difficulty(text):
    """Text me se Easy/Moderate/Hard shabd nikal kar (saaf text, difficulty) deta hai.
    Ek se zyada level likhe ho to 'mixed' (teeno ka mishran)."""
    levels = set()
    for m in _DIFF_RE.finditer(text or ""):
        w = m.group(0).lower()
        if w in ("easy", "आसान"):
            levels.add("easy")
        elif w in ("moderate", "medium", "मध्यम"):
            levels.add("moderate")
        else:
            levels.add("hard")
    clean = re.sub(r"\s+", " ", _DIFF_RE.sub(" ", text or "")).strip()
    if len(levels) >= 2:
        return clean, "mixed"
    return clean, (levels.pop() if levels else "")


def difficulty_note(level):
    return {
        "easy": " (कठिनाई: सभी प्रश्न Easy स्तर के हों)",
        "moderate": " (कठिनाई: सभी प्रश्न Moderate स्तर के हों)",
        "hard": " (कठिनाई: सभी प्रश्न Hard स्तर के हों)",
        "mixed": " (कठिनाई: Easy, Moderate और Hard प्रश्नों का बराबर मिश्रण हो)",
    }.get(level or "", "")


def parse_command(text):
    parts = (text or "").strip().split()
    if not parts:
        return None
    command = parts[0].lower().split("@")[0]
    if command not in COMMANDS:
        return None

    rest_str = " ".join(parts[1:])
    schedule_ts, cleaned_rest = parse_schedule_time(rest_str)
    cleaned_rest, difficulty = extract_difficulty(cleaned_rest)

    sections = None
    if command != "/notes":
        sections = parse_sections(cleaned_rest)
    if sections:
        total = sum(n for _, n in sections)
        names = {"/test": "Test", "/mock": "Mock", "/quiz": "Quiz", "/practice": "Practice", "/pyq": "PYQ"}
        return {
            "command": command,
            "topic": f"{names.get(command, 'Test')} {total}Q",
            "series": True,
            "count": total,
            "sections": sections,
            "error": "कुल प्रश्न 100 से ज़्यादा नहीं हो सकते।" if total > 100 else None,
            "difficulty": difficulty,
            "schedule_ts": schedule_ts,
        }

    rest_parts = cleaned_rest.split()
    count = 10
    if rest_parts and command != "/notes":
        m = re.fullmatch(r"(\d{1,3})(?:q|mcqs?|प्रश्न)?", rest_parts[-1], flags=re.IGNORECASE)
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
        "difficulty": difficulty,
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
        ("bihar current affairs", "Bihar Current Affairs"),
        ("bihar gk", "Bihar GK"),
        ("bihar general knowledge", "Bihar GK"),
        ("current affairs", "Current Affairs"),
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
        ("science", "Science"),
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


def ask_groq(prompt, is_json=False, temperature=None):
    client = get_client()
    kwargs = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3 if temperature is None else temperature,
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
            font-size: 26px;
        }}
        .print-btn {{
            background: #1a73e8;
            color: #ffffff;
            border: none;
            padding: 8px 16px;
            font-size: 15px;
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
        <a href="../index.html" class="back-btn" aria-label="Back to App">←</a>
        <button class="print-btn" onclick="window.print()">Download</button>
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
.aE{background:none;border:1.5px solid #fff;color:#fff;border-radius:8px;padding:4px 8px;font-weight:800;font-size:13px}
.wrap{max-width:620px;margin:auto;padding:14px}.card{background:#fff;border-radius:16px;padding:16px;box-shadow:0 3px 14px #0000000d;margin-bottom:12px}
.line{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #e5e9f0;padding-bottom:10px;margin-bottom:12px;font-weight:800}
.timer{color:#d97706}.q{font-size:17px;font-weight:800;line-height:1.55;margin:6px 0 14px}.qname{font-size:12px;color:#5b6b82;font-weight:700;margin:-4px 0 8px}
.opt{display:block;width:100%;text-align:left;background:#fff;border:1.5px solid #d5dce6;border-radius:12px;padding:13px;margin-bottom:9px;font-size:15px}
.opt.sel{border-color:#075fc3;background:#e8f1fd}.opt.ok{border-color:#1f9d55;background:#e7f7ee}.opt.bad{border-color:#e03a3a;background:#fdecec}
.row{display:flex;gap:10px}.btn{flex:1;border:0;border-radius:12px;padding:13px;font-weight:800;font-size:15px}
.pri{background:#075fc3;color:#fff}.sec{background:#fff;border:1.5px solid #d5dce6;color:#172033}.grn{background:#16834f;color:#fff}.tg{background:#229ed9;color:#fff}.red{background:#d92d2d;color:#fff}.blk{background:#111827;color:#fff}
.pal{background:none;border:0;font-size:22px}.hide{display:none}
.donut{width:150px;height:150px;border-radius:50%;margin:16px auto;display:flex;align-items:center;justify-content:center;position:relative;background:#c9d1dc}
.donut:before{content:"";position:absolute;inset:16px;background:#f4f6f9;border-radius:50%}.donut b{position:relative;font-size:28px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;text-align:center}.grid4 div{background:#fff;border-radius:12px;padding:10px 2px;cursor:pointer;border:1px solid #e3ebf5;box-shadow:0 2px 8px #0001}.grid4 div:active{background:#e3f0ff}.grid4 b{display:block;font-size:17px}.grid4 small{font-size:11px;color:#667085}
.tip{text-align:center;font-size:11px;color:#7b8799;margin:6px 0 12px}
.facts{background:#f1f5fb;border-radius:12px;padding:10px 12px;margin-top:10px;font-size:14px;line-height:1.6}.facts ul{margin:6px 0 0 18px;padding:0}
.modal{display:none;position:fixed;inset:0;background:#0007;align-items:center;justify-content:center;z-index:20}.modal.open{display:flex}
.box{background:#fff;border-radius:16px;padding:16px;width:88%;max-width:380px}.pg{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:12px 0}
.pg button{border:0;border-radius:10px;padding:12px 0;font-weight:800;background:#e6ebf2}.pg .a{background:#1f9d55;color:#fff}.pg .c{outline:3px solid #075fc3}
.sheetm{align-items:flex-end}.sheet{background:#fff;width:100%;max-width:620px;border-radius:18px 18px 0 0;padding:16px 22px 24px}.sheet h4{color:#8b96a6;margin:0 0 6px}.sheet div{padding:15px 0;border-bottom:1px solid #eef1f5;font-size:16px}
small.m{color:#667085}
</style>
</head>
<body>
<div class="top"><a href="../index.html" id="back">←</a><div class="title" id="ttl">Gyankshetra</div><button class="aE" onclick="openLang()">अ/E</button></div>
<div class="wrap">
 <section id="quiz">
  <div class="card">
   <div class="line"><button class="pal" onclick="openPal()">☰</button><span id="qc"></span><span class="timer" id="tm"></span></div>
   <div class="qname" id="nm"></div>
   <div class="q" id="qt"></div><div id="opts"></div>
   <div class="row"><button class="btn sec" id="pv" onclick="go(-1)"></button><button class="btn pri" id="nx" onclick="go(1)"></button></div>
  </div>
 </section>
 <section id="result" class="hide">
  <h2 style="margin:6px 0" data-i="result"></h2><p id="who" style="text-align:center;font-weight:800;margin:0"></p>
  <div class="donut" id="donut"><b id="pct">0%</b></div>
  <div class="grid4"><div onclick="review('all')"><b id="mk"></b><small data-i="marks"></small></div><div onclick="review('ok')"><b id="co"></b><small data-i="correct"></small></div><div onclick="review('bad')"><b id="wr"></b><small data-i="wrong"></small></div><div onclick="review('skip')"><b id="sk"></b><small data-i="skipped"></small></div></div>
  <p class="tip" data-i="tip"></p>
  <button class="btn pri" style="width:100%;margin-bottom:10px" onclick="review('all')" data-i="analysis"></button>
  <div class="row" style="margin-bottom:10px"><button class="btn grn" onclick="location.reload()" data-i="reattempt"></button><button class="btn tg" onclick="share()" data-i="share"></button></div>
  <a href="../index.html" class="btn blk" style="display:block;text-align:center;text-decoration:none" data-i="back"></a>
 </section>
 <section id="review" class="hide">
  <div class="card"><div class="line"><button class="pal" onclick="backRes()">←</button><span id="rc"></span><span></span></div>
   <div class="q" id="rq"></div><div id="ro"></div><div id="rf"></div>
   <div class="row" style="margin-top:12px"><button class="btn sec" onclick="rgo(-1)" data-i="rprev"></button><button class="btn pri" onclick="rgo(1)" data-i="rnext"></button></div>
  </div>
 </section>
</div>
<div class="modal" id="pal" onclick="closePal()"><div class="box" onclick="event.stopPropagation()"><b data-i="palette"></b><div class="pg" id="pg"></div><button class="btn red" style="width:100%;margin-bottom:8px" onclick="closePal();submitNow()" data-i="submitBtn"></button><button class="btn sec" style="width:100%" onclick="closePal()" data-i="close"></button></div></div>
<div class="modal sheetm" id="lsh" onclick="closeLang()"><div class="sheet" onclick="event.stopPropagation()"><h4 data-i="selLang"></h4><div onclick="setLang('hi')">हिन्दी</div><div onclick="setLang('en')">English</div><div onclick="setLang('bh')">भोजपुरी</div></div></div>
<script>
const testData = __TESTDATA__;
const Q=testData.questions,N=Q.length;let ans=Array(N).fill(null),qi=0,left=N*60,tid=null,done=false,rOrder=[],ri=0,res={c:0,w:0,s:0},phase='quiz';
const $=id=>document.getElementById(id);
const I18N={
hi:{q:'प्रश्न',prev:'← पिछला',next:'अगला →',submitT:'Submit Test ✔',palette:'प्रश्न पैलेट',close:'बंद करें',submitBtn:'✅ Submit',result:'🏆 परिणाम',marks:'अंक',correct:'सही',wrong:'गलत',skipped:'छूटे',tip:'किसी भी बॉक्स पर टैप करें — उसका Analysis खुलेगा',analysis:'📚 Analysis',reattempt:'🔁 Reattempt',share:'📤 Result Share करें',back:'🏠 ऐप पर वापस जाएँ',review:'समीक्षा',rprev:'← पिछला',rnext:'अगला →',yours:'आपका उत्तर',notans:'छोड़ा गया',right:'सही उत्तर',factsH:'📌 व्याख्या / तथ्य:',empty:'इस श्रेणी में कोई प्रश्न नहीं है।',confirm:'क्या आप टेस्ट Submit करना चाहते हैं?',selLang:'भाषा चुनें'},
en:{q:'Question',prev:'← Previous',next:'Next →',submitT:'Submit Test ✔',palette:'Question palette',close:'Close',submitBtn:'✅ Submit',result:'🏆 Result',marks:'Marks',correct:'Correct',wrong:'Wrong',skipped:'Skipped',tip:'Tap any box to open its analysis',analysis:'📚 Analysis',reattempt:'🔁 Reattempt',share:'📤 Share Result',back:'🏠 Back to App',review:'Review',rprev:'← Previous',rnext:'Next →',yours:'Your answer',notans:'Not answered',right:'Correct answer',factsH:'📌 Explanation / Facts:',empty:'No questions in this category.',confirm:'Do you want to submit the test?',selLang:'Select Language'},
bh:{q:'सवाल',prev:'← पिछिला',next:'अगिला →',submitT:'Submit Test ✔',palette:'सवाल पैलेट',close:'बंद करीं',submitBtn:'✅ Submit',result:'🏆 नतीजा',marks:'अंक',correct:'सही',wrong:'गलत',skipped:'छोड़ल गइल',tip:'कवनो बॉक्स पर टैप करीं — ओकर विश्लेषण खुली',analysis:'📚 Analysis',reattempt:'🔁 Reattempt',share:'📤 Result Share करीं',back:'🏠 ऐप पर लौटीं',review:'समीक्षा',rprev:'← पिछिला',rnext:'अगिला →',yours:'रउआ जवाब',notans:'छोड़ल गइल',right:'सही जवाब',factsH:'📌 व्याख्या / तथ्य:',empty:'एह श्रेणी में कवनो सवाल नइखे।',confirm:'का रउआ टेस्ट Submit करे के चाहत बानी?',selLang:'भाषा चुनीं'}};
let lang='hi';try{lang=localStorage.getItem('gyankshetraLang')||'hi'}catch(e){}
if(!I18N[lang])lang='hi';
function T(k){return (I18N[lang]&&I18N[lang][k])||I18N.hi[k]||k}
function V(i){const q=Q[i],t=testData.tr&&testData.tr[lang]&&testData.tr[lang][i];
if(t&&t.text&&Array.isArray(t.options)&&t.options.length===q.options.length)return{text:t.text,options:t.options,facts:(Array.isArray(t.facts)&&t.facts.length)?t.facts:q.facts};
return{text:q.text,options:q.options,facts:q.facts}}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function fmt(t){t=Math.max(0,t);return String(Math.floor(t/60)).padStart(2,'0')+':'+String(t%60).padStart(2,'0')}
function uname(){try{if(localStorage.getItem('gyankshetraLoggedIn')==='1'){return localStorage.getItem('gyankshetraUserName')||testData.studentName}}catch(e){}return testData.studentName}
$('ttl').textContent='Gyankshetra · '+testData.topic;
function render(){const v=V(qi);$('qc').textContent=T('q')+' '+(qi+1)+'/'+N;$('qt').textContent=v.text;$('nm').textContent='👤 '+uname();
$('opts').innerHTML=v.options.map((o,i)=>'<button class="opt '+(ans[qi]===i?'sel':'')+'" onclick="pick('+i+')">'+String.fromCharCode(65+i)+'. '+esc(o)+'</button>').join('');
$('pv').disabled=qi===0;$('pv').textContent=T('prev');$('nx').textContent=qi===N-1?T('submitT'):T('next');$('tm').textContent='⏱ '+fmt(left)}
function pick(i){ans[qi]=i;render()}
function go(d){if(d>0&&qi===N-1){submitNow();return}const n=qi+d;if(n<0||n>=N)return;qi=n;render()}
function submitNow(){const sk=ans.filter(a=>a===null).length;if(confirm(T('confirm')+(sk?'\n('+sk+' '+T('skipped')+')':'')))finish()}
function tick(){left--;$('tm').textContent='⏱ '+fmt(left);if(left<=0)finish()}
function openPal(){$('pg').innerHTML=Q.map((q,i)=>'<button class="'+(ans[i]!==null?'a ':'')+(i===qi?'c':'')+'" onclick="jump('+i+')">'+(i+1)+'</button>').join('');$('pal').classList.add('open')}
function closePal(){$('pal').classList.remove('open')}
function jump(i){qi=i;closePal();render()}
function openLang(){$('lsh').classList.add('open')}
function closeLang(){$('lsh').classList.remove('open')}
function setLang(l){lang=l;try{localStorage.setItem('gyankshetraLang',l)}catch(e){}closeLang();applyLang()}
function applyLang(){document.documentElement.lang=lang==='bh'?'bho':lang;document.querySelectorAll('[data-i]').forEach(e=>e.textContent=T(e.dataset.i));if(phase==='quiz')render();else if(phase==='result')paintRes();else rrender()}
function paintRes(){const c=res.c,w=res.w,s=res.s,a=c/N*100,b=a+w/N*100;$('donut').style.background='conic-gradient(#1f9d55 0 '+a+'%,#e03a3a '+a+'% '+b+'%,#c9d1dc '+b+'% 100%)';
$('pct').textContent=Math.round(a)+'%';$('mk').textContent=c+' / '+N;$('co').textContent=c;$('wr').textContent=w;$('sk').textContent=s;$('who').textContent='👤 '+uname()}
function finish(){if(done)return;done=true;clearInterval(tid);closePal();let c=0,w=0,s=0;ans.forEach((a,i)=>{if(a===null)s++;else if(a===Q[i].correctOption)c++;else w++});res={c,w,s};
try{const h=JSON.parse(localStorage.getItem('gyankshetraHistory')||'[]');h.push({day:new Date().toISOString().slice(0,10),date:new Date().toLocaleString('hi-IN'),test:testData.topic,marks:c,correct:c,wrong:w,skipped:s,total:N});localStorage.setItem('gyankshetraHistory',JSON.stringify(h))}catch(e){}
phase='result';paintRes();$('quiz').classList.add('hide');$('result').classList.remove('hide');window.scrollTo(0,0)}
function review(f){rOrder=Q.map((q,i)=>i).filter(i=>f==='all'||(f==='ok'&&ans[i]===Q[i].correctOption)||(f==='bad'&&ans[i]!==null&&ans[i]!==Q[i].correctOption)||(f==='skip'&&ans[i]===null));if(!rOrder.length){alert(T('empty'));return}ri=0;phase='review';$('result').classList.add('hide');$('review').classList.remove('hide');rrender()}
function rrender(){const i=rOrder[ri],q=Q[i],v=V(i);$('rc').textContent=T('review')+' '+(ri+1)+'/'+rOrder.length;$('rq').textContent=(i+1)+'. '+v.text;
$('ro').innerHTML=v.options.map((o,k)=>'<div class="opt '+(k===q.correctOption?'ok':(k===ans[i]?'bad':''))+'">'+String.fromCharCode(65+k)+'. '+esc(o)+(k===q.correctOption?' ✅':'')+(k===ans[i]&&k!==q.correctOption?' ❌':'')+'</div>').join('');
$('rf').innerHTML='<small class="m">'+T('yours')+': '+(ans[i]===null?T('notans'):String.fromCharCode(65+ans[i]))+' • '+T('right')+': '+String.fromCharCode(65+q.correctOption)+'</small>'+(v.facts&&v.facts.length?'<div class="facts"><b>'+T('factsH')+'</b><ul>'+v.facts.map(f=>'<li>'+esc(f)+'</li>').join('')+'</ul></div>':'')}
function rgo(d){const n=ri+d;if(n<0||n>=rOrder.length)return;ri=n;rrender()}
function backRes(){phase='result';$('review').classList.add('hide');$('result').classList.remove('hide')}
function share(){const link=location.href.split('#')[0].split('?')[0],p=Math.round(res.c/N*100);
const t='🎓 Gyankshetra Result\n👤 '+uname()+'\n📝 '+testData.topic+'\n🏆 अंक: '+res.c+'/'+N+' ('+p+'%)\n✅ सही: '+res.c+'  ❌ गलत: '+res.w+'  ⏭ छूटे: '+res.s+'\n📢 Telegram चैनल: https://t.me/gyankshetra\n🔗 टेस्ट लिंक: '+link;
window.open('https://t.me/share/url?url='+encodeURIComponent(link)+'&text='+encodeURIComponent(t),'_blank')}
(function(){let x0=0,y0=0;const w=$('quiz');w.addEventListener('touchstart',e=>{x0=e.touches[0].clientX;y0=e.touches[0].clientY},{passive:true});
w.addEventListener('touchend',e=>{const dx=e.changedTouches[0].clientX-x0,dy=e.changedTouches[0].clientY-y0;if(Math.abs(dx)<50||Math.abs(dx)<Math.abs(dy)*1.3)return;if(dx>0){if(qi>0){qi--;render()}}else if(qi<N-1){qi++;render()}},{passive:true})})();
applyLang();tid=setInterval(tick,1000);
</script>
</body>
</html>
"""



# ------------------------------------------- Question translation (en / bh)
TR_LANGS = {
    "en": "English",
}
TRANSLATE_TESTS = os.environ.get("TRANSLATE_TESTS", "1") != "0"
TR_CHUNK = 5

TR_PROMPT = """
नीचे MCQ प्रश्नों की सूची JSON में है। हर प्रश्न का "text", "options" और "facts" का अनुवाद __LANG__ में करो।

नियम:
0. अनुवाद शुद्ध, सटीक और परीक्षा-स्तर की सही भाषा में हो (शब्दशः नहीं, अर्थ के अनुसार)। वर्तनी/व्याकरण की गलती न हो। नाम (व्यक्ति, स्थान, संस्था, ग्रंथ) का मानक spelling इस्तेमाल करो (जैसे बिहार→Bihar, गया→Gaya, हड़प्पा→Harappa)। 'उपयुक्त में से कोई नहीं' जैसे विकल्पों का स्थापित रूप लिखो ('None of the above', 'More than one of the above')। मूल हिन्दी में कोई वर्तनी की गलती दिखे तो संदर्भ से सही अर्थ लेकर अनुवाद करो।
1. अर्थ, संख्याएँ, सूत्र, यूनिट और वैज्ञानिक/तकनीकी शब्द सही और जस के तस रखो।
2. options की संख्या और क्रम बिल्कुल वही रखो (अनुवाद के बाद भी चार विकल्प, उसी क्रम में)।
3. facts की संख्या और क्रम भी वही रखो।
4. गलत या नया तथ्य मत जोड़ो।
5. केवल वैध JSON दो, कोई अतिरिक्त टेक्स्ट या Markdown code fence नहीं।

सिर्फ इस structure में JSON दो (items की संख्या इनपुट के बराबर हो):
{"items":[{"text":"...","options":["...","...","...","..."],"facts":["..."]}]}

इनपुट:
__DATA__
"""


def _translate_chunk(chunk, lang_label):
    data = json.dumps(
        [{"text": q[0], "options": q[1], "facts": q[3]} for q in chunk],
        ensure_ascii=False,
    )
    prompt = TR_PROMPT.replace("__LANG__", lang_label).replace("__DATA__", data)
    last_err = None
    for attempt in range(2):
        try:
            raw = ask_groq(prompt, is_json=True, temperature=0.1).strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
            items = json.loads(raw).get("items")
            if not isinstance(items, list) or len(items) != len(chunk):
                raise ValueError("items की संख्या मेल नहीं खाती")
            out = []
            for q, it in zip(chunk, items):
                ok = (
                    isinstance(it, dict)
                    and isinstance(it.get("text"), str) and it["text"].strip()
                    and isinstance(it.get("options"), list)
                    and len(it["options"]) == len(q[1])
                    and all(isinstance(o, str) and o.strip() for o in it["options"])
                )
                if not ok:
                    out.append(None)
                    continue
                facts = it.get("facts")
                facts = [str(f) for f in facts if str(f).strip()] if isinstance(facts, list) else []
                out.append({
                    "text": it["text"].strip(),
                    "options": [o.strip() for o in it["options"]],
                    "facts": facts,
                })
            return out
        except Exception as e:
            last_err = e
            print(f"Translate retry {attempt + 1}/2:", e)
            time.sleep(_backoff(e, attempt))
    print("Translate chunk failed:", last_err)
    return [None] * len(chunk)


def translate_questions(questions):
    """Har prashn ka English + Bhojpuri anuvad. Fail hone par us bhasha me Hindi hi dikhega."""
    result = {}
    if not TRANSLATE_TESTS:
        return result
    for code, label in TR_LANGS.items():
        items = []
        for i in range(0, len(questions), TR_CHUNK):
            items.extend(_translate_chunk(questions[i:i + TR_CHUNK], label))
            time.sleep(1)
        if any(items):
            result[code] = items
    return result


# ------------------------------------------------------------- Notes title
def _ordinal(n):
    n = int(n)
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def hindi_chapter(text):
    """Chapter ka naam Hindi me (jaise 'gravitation' -> 'गुरुत्वाकर्षण'). Fail ho to jaisa hai waisa."""
    if re.search(r"[\u0900-\u097F]", text):
        return text
    try:
        out = ask_groq(
            "इस परीक्षा-विषय/अध्याय का केवल हिन्दी नाम लिखो (2-4 शब्द, कोई अतिरिक्त शब्द, चिह्न या अंग्रेज़ी नहीं): "
            + text
        ).strip().strip("\"'`.।\n ")
        out = out.splitlines()[0].strip() if out else ""
        if out and re.search(r"[\u0900-\u097F]", out) and len(out) <= 60:
            return out
    except Exception as e:
        print("hindi_chapter failed:", e)
    return text


def notes_title(topic):
    """'📄 9th Physics — गुरुत्वाकर्षण (Notes)' jaisa title (📄 app khud lagata hai)."""
    meta = extract_metadata(topic)
    head = []
    m = re.search(r"\d+", meta["class"]) if meta["class"] != "GENERAL" else None
    if m:
        head.append(_ordinal(m.group(0)))
    if meta["subject"] != "General":
        head.append(meta["subject"])
    chapter = hindi_chapter(meta["topic"])
    left = " ".join(head)
    return f"{left} — {chapter} (Notes)" if left else f"{chapter} (Notes)"


def _find_index_html():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(os.getcwd(), "index.html"),
              os.path.join(here, "index.html"),
              os.path.join(here, "..", "index.html")):
        if os.path.isfile(p):
            return p
    return None


def make_test_page(topic, count, questions, test_id, translations=None):
    """Repo ki index.html (app ka asli test UI) se naya test page banata hai.
    index.html me GK_DATA_START/GK_DATA_END ke beech ka data badla jata hai.
    index.html na mile ya marker na ho to simple template use hota hai."""
    path = _find_index_html()
    src = ""
    if path:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
    a, b = src.find("/*GK_DATA_START*/"), src.find("/*GK_DATA_END*/")
    if a < 0 or b < a:
        print("index.html ya GK_DATA marker nahi mila - simple template use ho raha hai")
        return make_test_page_simple(topic, count, questions, test_id, translations)

    qlist = [[q[0], q[1], q[2], q[3]] for q in questions]
    tr = {}
    for code, items in (translations or {}).items():
        tr[code] = [
            [it["text"], it["options"], it.get("facts") or q[3]] if it else None
            for it, q in zip(items, questions)
        ]
    data = (
        "/*GK_DATA_START*/\n"
        f"const TOTAL={count},TIME={count * 60},TEST_NAME={js_safe(topic)},TEST_ID={js_safe(test_id)};\n"
        f"const questions={js_safe(qlist)};\n"
        f"const TR={js_safe(tr)};\n"
    )
    page = src[:a] + data + src[b:]
    safe = html.escape(topic)
    page = re.sub(
        r"<title>.*?</title>",
        lambda m: f"<title>Gyankshetra — {safe} {count}Q</title>",
        page, count=1, flags=re.S,
    )
    return page


def make_test_page_simple(topic, count, questions, test_id, translations=None):
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
        "tr": translations or {},
    }
    safe = html.escape(topic)
    s = TEST_TEMPLATE.replace("__TESTDATA__", js_safe(data))
    s = re.sub(r"<title>.*?</title>", lambda m: f"<title>{safe} | Gyankshetra</title>", s, count=1, flags=re.S)
    return s


def next_series_number(prefix):
    """'Test 40Q' ke liye registry dekh kar agla number deta hai (1, 2, 3...)."""
    try:
        with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
            reg = json.load(f)
    except Exception:
        reg = []
    nums = [0]
    for e in reg if isinstance(reg, list) else []:
        m = re.fullmatch(re.escape(prefix) + r" (\d+)", str(e.get("topic", "")))
        if m:
            nums.append(int(m.group(1)))
    return max(nums) + 1


def update_registry(topic, count, command, url, item_id, mixed=False, meta_topic=None):
    os.makedirs("content", exist_ok=True)
    info = command_info(command)
    meta = extract_metadata(meta_topic or topic)

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

    if meta_topic:
        entry["topic"] = topic

    if mixed:  # kai vishay ka test: kisi ek subject me nahi, seedha Test Series me
        entry["class"], entry["subject"], entry["topic"] = "GENERAL", "Mixed", topic

    target_file = STUDY_MATERIAL_FILE if command == "/notes" else REGISTRY_FILE
    if command == "/notes":
        entry["title"] = notes_title(topic)
    else:
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
        dirs = [d for d in ("content", "tests", "quiz", "practice", "notes", "materials") if os.path.isdir(d)]
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
    """File ke naam ke liye sirf English akshar/ank (chhota, saaf URL; Hindi naam par 'test')."""
    text = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-")
    return (text or "test")[:50]


LINK_STYLE = os.environ.get("LINK_STYLE", "button").strip().lower()   # button | text | url


def link_line(label, url, tag=None):
    """Test/notes ka link. LINK_STYLE:
    button (default): message ke neeche "🔗 QUIZ NOW" naam ka button, URL chhupa rehta hai (Telegram 'Open Link' nahi poochta)
    text            : "🔗 QUIZ NOW" hyperlink text (Telegram 'Open Link' poochta hai)
    url             : "🔗 https://..." seedha dikhta URL"""
    if LINK_STYLE == "url":
        return f"🔗 {html.escape(url)}"
    if LINK_STYLE == "text":
        return f'🔗 <a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
    name = f"🔗 {label}" + (f" · {str(tag)[:22]}" if tag else "")
    name = name.replace("|", "-").replace("⟦", "").replace("⟧", "")
    return f"⟦BTN|{name}|{url}⟧"


_BTN_RE = re.compile(r"⟦BTN\|([^|⟧]*)\|([^⟧]+)⟧")


def _split_buttons(text):
    """Text me chhupe ⟦BTN|naam|url⟧ nishan hata kar (text, [(naam, url)]) deta hai."""
    btns = [(m.group(1), m.group(2)) for m in _BTN_RE.finditer(text)]
    text = _BTN_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).rstrip()
    return text, btns


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

# ------------------------------------------------ Universal (natural language)
BOT_VERSION = "2026-10-12e • OCR + 8-point analysis + /rebuild + link-button"

HELP_TEXT = (
    "🤖 <b>Gyankshetra Bot</b>\n"
    "<i>Version: " + BOT_VERSION + "</i>\n\n"
    "जो चाहिए सीधे लिख दें, जैसे:\n"
    "• भारत का भूगोल 30 प्रश्न का quiz, Easy Moderate Hard\n"
    "• 40 MCQ test: history 8, geo 8, math 8, science 8, current affairs 8\n"
    "• बिहार GK का mock test 50 प्रश्न\n"
    "• ऑक्सीकरण के short notes / study material\n"
    "• 20 प्रश्नों का practice set गुरुत्वाकर्षण पर\n"
    "• Test 40Q 1 delete कर दो\n"
    "• /rebuild — पुराने test/quiz pages में ऐप का नया design/सुधार लगाना\n"
    "• BPSC 2019 paper से 30 प्रश्न का quiz (ऐप में सेव की हुई फ़ाइल से)\n"
    "• 📎 PDF / DOCX भेजें (साथ में लिखें: 30 प्रश्न quiz, विषय अलग-अलग, chapter wise, या सब मिलाकर एक test)\n\n"
    "Commands भी चलते हैं: /test /mock /quiz /practice /pyq /notes"
)

NL_PROMPT = """
तुम Gyankshetra परीक्षा-ऐप के Telegram bot के लिए intent parser हो।
नीचे यूज़र का संदेश है (हिन्दी / English / Hinglish)। समझो कि वह क्या बनवाना चाहता है और केवल वैध JSON दो।

intent के विकल्प:
- "test": test, टेस्ट, test series, question set, प्रश्न-सेट
- "mock": mock test, मॉक
- "quiz": quiz, क्विज़
- "practice": practice, अभ्यास, set, सवाल/प्रश्न बनाओ (बिना किसी और शब्द के)
- "pyq": पिछले वर्षों के प्रश्न, PYQ
- "notes": notes, नोट्स, study material, अध्ययन सामग्री, download, PDF, summary, पढ़ने की सामग्री
- "from_file": यूज़र नीचे दी गई "सेव की हुई फ़ाइलें" में से किसी फ़ाइल/paper/किताब से test/quiz बनवाना चाहे। अगर संदेश में "book / किताब / pdf / paper / notes / NCERT से" जैसा शब्द हो और सूची की किसी फ़ाइल के नाम में उसका विषय-शब्द (जैसे "tissue") मिले, तो यही चुनो; topic में उस फ़ाइल का पूरा नाम (सूची जैसा) लिखो
- "rebuild": ऐप का नया design/सुधार पुराने बने test/quiz pages में भी लगाना (update/refresh/rebuild all tests)
- "delete": बना हुआ test/quiz/notes हटाना (delete, हटाओ, मिटाओ); topic में उसका नाम, जैसे "Test 40Q 1"
- "chat": ऊपर में से कुछ नहीं; सामान्य सवाल/बातचीत

JSON structure:
{"intent":"test","topic":"विषय","count":10,"sections":[],"difficulty":"","reply":""}

नियम:
1. topic में केवल विषय/अध्याय लिखो (संख्या, test/quiz जैसे शब्द और Easy/Hard हटाकर)।
2. count: कुल प्रश्न (1 से 100)। न लिखा हो तो 10। notes पर count 0।
3. अगर यूज़र कई विषयों के अलग-अलग प्रश्न चाहे (जैसे "history 8, geo 8, math 8"), तो sections में [{"name":"History","count":8},...] दो और topic खाली छोड़ो। वरना sections खाली [] रखो।
4. difficulty: "easy", "moderate", "hard" में से, या अगर कई स्तर बताए तो "mixed", न बताया हो तो "".
5. intent "chat" हो तो reply में हिन्दी में छोटा, सही जवाब (अधिकतम 800 अक्षर) दो और बाकी खाली रखो।
6. केवल JSON, कोई अतिरिक्त शब्द या Markdown नहीं।

सेव की हुई फ़ाइलें (ऐप के Study Material में):
__FILES__

यूज़र का संदेश:
__TEXT__
"""

_INTENT_CMD = {"test": "/test", "mock": "/mock", "quiz": "/quiz", "practice": "/practice", "pyq": "/pyq", "notes": "/notes"}


def natural_command(text):
    """Seedhe likhe sandesh ko Groq se samajh kar parse_command jaisa dict deta hai.
    Return: (cmd, reply). Chat jaisa ho to cmd None aur reply me jawab."""
    schedule_ts, cleaned = parse_schedule_time(text)
    raw = ask_groq(NL_PROMPT.replace("__FILES__", "\n".join(material_titles()) or "(कोई नहीं)").replace("__TEXT__", cleaned), is_json=True).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    data = json.loads(raw)

    intent = str(data.get("intent", "")).lower().strip()
    if intent == "rebuild":
        return {"command": "/rebuild"}, ""
    if intent == "from_file":
        return {"command": "/doc", "topic": str(data.get("topic", "")).strip(), "caption": text}, ""
    if intent == "delete":
        return {"command": "/delete", "topic": str(data.get("topic", "")).strip()}, ""
    if intent not in _INTENT_CMD:
        return None, (str(data.get("reply", "")).strip() or HELP_TEXT)

    command = _INTENT_CMD[intent]
    if command != "/notes" and re.search(r"ncert|book|pdf|file|paper|notes|किताब|पुस्तक|बुक|फ़ाइल|पेपर|नोट्स", text, re.IGNORECASE):
        m, _many = find_material(str(data.get("topic", "")).strip() or text)
        if len(m) == 1:
            return {"command": "/doc", "topic": str(m[0].get("title")), "caption": text}, ""
    _, diff_text = extract_difficulty(text)
    difficulty = str(data.get("difficulty", "") or "").lower().strip()
    if difficulty not in ("easy", "moderate", "hard", "mixed"):
        difficulty = diff_text

    sections = []
    for sec in data.get("sections") or []:
        try:
            name = str(sec.get("name", "")).strip()
            n = int(sec.get("count", 0))
        except Exception:
            continue
        if name and 1 <= n <= 100:
            sections.append((name, n))

    if command != "/notes" and len(sections) >= 2:
        total = sum(n for _, n in sections)
        names = {"/test": "Test", "/mock": "Mock", "/quiz": "Quiz", "/practice": "Practice", "/pyq": "PYQ"}
        return {
            "command": command,
            "topic": f"{names.get(command, 'Test')} {total}Q",
            "count": total,
            "sections": sections,
            "error": "कुल प्रश्न 100 से ज़्यादा नहीं हो सकते।" if total > 100 else None,
            "difficulty": difficulty,
            "schedule_ts": schedule_ts,
        }, ""

    topic = str(data.get("topic", "")).strip() or "सामान्य अध्ययन"
    try:
        count = int(data.get("count", 10))
    except Exception:
        count = 10
    count = max(1, min(count, 100))
    return {
        "command": command,
        "topic": topic,
        "count": count,
        "difficulty": difficulty,
        "schedule_ts": schedule_ts,
    }, ""



# ------------------------------------------------------------------ Delete
def _norm_title(t):
    t = re.sub(r"^[^\w\u0900-\u097F]+", "", str(t or "")).lower()
    return re.sub(r"\s+", " ", t).strip()


def delete_items(query):
    """Naam se bana hua test/quiz/notes hatata hai (registry + HTML file). 
    Return: (hataye gaye titles, ek se zyada milne par titles ki list)."""
    q = _norm_title(query)
    if not q:
        return [], []
    found = []
    for path in (REGISTRY_FILE, STUDY_MATERIAL_FILE):
        try:
            with open(path, "r", encoding="utf-8") as f:
                reg = json.load(f)
        except Exception:
            continue
        if isinstance(reg, list):
            found.append((path, reg))

    exact, partial = [], []
    for path, reg in found:
        for e in reg:
            t = _norm_title(e.get("title"))
            if t == q:
                exact.append((path, e))
            elif q in t:
                partial.append((path, e))

    hits = exact or partial
    if not hits:
        return [], []
    if len(hits) > 1 and not exact:
        return [], [str(e.get("title")) for _, e in hits]

    deleted = []
    for path, e in hits:
        reg = dict(found)[path]
        reg[:] = [x for x in reg if x.get("id") != e.get("id") or x.get("url") != e.get("url")]
        url = str(e.get("url", ""))
        if url.startswith(BASE_URL):
            rel = unquote(url[len(BASE_URL):].split("?")[0].split("#")[0])
            top = rel.split("/")[0]
            ok_dir = (top in ("tests", "quiz", "practice", "notes") and rel.endswith(".html")) or top == "materials"
            if ok_dir and ".." not in rel and os.path.isfile(rel):
                os.remove(rel)
        deleted.append(str(e.get("title")))
    for path, reg in found:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)
    return deleted, []


# ------------------------------------------- File upload (PDF / DOCX / TXT)
MAX_FILE_MB = 20      # Telegram bot sirf itni badi file utaar sakta hai
MAX_CHUNKS = 60       # ek file se zyada se zyada itne hisse padhe jate hain
CHUNK_CHARS = 4500
MIN_GROUP = 5         # isse chhote vishay/chapter group "अन्य" me mila diye jate hain
MAX_PER_TEST = 100

SUBJ_HI = {
    "History": "इतिहास", "Geography": "भूगोल", "Polity": "राजव्यवस्था", "Economics": "अर्थशास्त्र",
    "Physics": "भौतिक विज्ञान", "Chemistry": "रसायन विज्ञान", "Biology": "जीव विज्ञान",
    "Science": "विज्ञान", "Social Science": "सामाजिक विज्ञान", "Maths": "गणित",
    "Reasoning": "रीज़निंग", "Hindi": "हिन्दी", "English": "English", "Sanskrit": "संस्कृत",
    "Computer": "कंप्यूटर", "Pedagogy": "शिक्षाशास्त्र", "EVS": "EVS",
    "Current Affairs": "करेंट अफेयर्स", "Bihar GK": "बिहार GK",
    "Bihar Current Affairs": "बिहार करेंट अफेयर्स", "General Knowledge": "सामान्य ज्ञान",
}

EXTRACT_PROMPT = """
नीचे किसी परीक्षा-पेपर / प्रश्न-सेट (PDF या Word) का टेक्स्ट है। इसमें जितने भी बहुविकल्पीय प्रश्न (MCQ) हैं, सब निकालकर JSON दो।

नियम:
1. अपनी तरफ से कोई नया प्रश्न मत जोड़ो; अधूरा या कटा हुआ प्रश्न छोड़ दो।
2. प्रश्न और विकल्प उसी भाषा में रखो (हिन्दी/English)। PDF से निकले हिन्दी टेक्स्ट में शब्द टूटे या मात्राएँ गलत जगह हो सकती हैं (जैसे 'सबहार' = 'बिहार', 'सकि' = 'किस', 'िएक' = 'एक'); उन्हें संदर्भ से समझकर शुद्ध, सही वर्तनी वाली हिन्दी में ठीक करके लिखो, पर तथ्य/अर्थ न बदलो। विकल्पों से (A), (B), 1., क) जैसे लेबल हटा दो। ठीक 4 विकल्प हों।
3. answer: सही विकल्प का index (0, 1, 2 या 3)। उत्तर फ़ाइल में (या नीचे उत्तर-कुंजी में) दिया हो तो वही लो और answer_in_file true रखो; न मिले तो खुद हल करके सही उत्तर चुनो और answer_in_file false रखो।
4. subject: इस सूची में से ठीक एक अंग्रेज़ी नाम: History, Geography, Polity, Economics, Physics, Chemistry, Biology, Science, Maths, Reasoning, Hindi, English, Sanskrit, Computer, Pedagogy, EVS, Current Affairs, Bihar GK, Bihar Current Affairs, General Knowledge. फ़ाइल के शीर्षक/सेक्शन से तय करो, न मिले तो प्रश्न देखकर।
5. chapter: अध्याय/टॉपिक का छोटा नाम (हिन्दी में); न पता हो तो "".
6. facts: उत्तर का एक पंक्ति का छोटा तथ्य/व्याख्या (हिन्दी में)।
7. एक भी प्रश्न न मिले तो {"items": []} दो।
केवल JSON दो: {"items":[{"q":"","options":["","","",""],"answer":0,"answer_in_file":true,"subject":"","chapter":"","facts":[""]}]}
__KEY__

टेक्स्ट:
__TEXT__
"""

GEN_FROM_TEXT_PROMPT = """
नीचे अध्ययन-सामग्री का टेक्स्ट है। केवल इसी सामग्री के आधार पर __N__ बहुविकल्पीय प्रश्न (MCQ) हिन्दी में बनाओ।

नियम:
1. सभी प्रश्न परीक्षा-उपयोगी हों और सामग्री में दिए तथ्यों पर टिके हों; अपनी तरफ से गलत तथ्य मत जोड़ो।
2. प्रत्येक प्रश्न के ठीक 4 अलग-अलग विकल्प हों। answer सही विकल्प का index (0-3) हो।
3. subject: इस सूची में से ठीक एक अंग्रेज़ी नाम: History, Geography, Polity, Economics, Physics, Chemistry, Biology, Science, Maths, Reasoning, Hindi, English, Sanskrit, Computer, Pedagogy, EVS, Current Affairs, Bihar GK, Bihar Current Affairs, General Knowledge.
4. chapter: अध्याय/टॉपिक का छोटा नाम (हिन्दी में); facts: उत्तर की एक पंक्ति की व्याख्या।__NOTE__
केवल JSON दो: {"items":[{"q":"","options":["","","",""],"answer":0,"subject":"","chapter":"","facts":[""]}]}

सामग्री:
__TEXT__
"""


def download_telegram_file(file_id):
    info = telegram("getFile", {"file_id": file_id})
    path = (info.get("result") or {}).get("file_path")
    if not path:
        raise RuntimeError("Telegram से फ़ाइल उतार नहीं पाया")
    r = requests.get("https://api.telegram.org/file/bot" + BOT_TOKEN + "/" + path, timeout=120)
    r.raise_for_status()
    return r.content


def extract_text(data, filename):
    ext = os.path.splitext(filename.lower())[1]
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("pypdf install नहीं है। GitHub workflow में pip install वाली लाइन में pypdf और python-docx जोड़ें।")
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    if ext == ".docx":
        try:
            from docx import Document
            from docx.table import Table
            from docx.text.paragraph import Paragraph
        except ImportError:
            raise RuntimeError("python-docx install नहीं है। GitHub workflow में pip install वाली लाइन में pypdf और python-docx जोड़ें।")
        doc = Document(io.BytesIO(data))
        out = []
        for el in doc.element.body.iterchildren():
            if el.tag.endswith("}p"):
                out.append(Paragraph(el, doc).text)
            elif el.tag.endswith("}tbl"):
                for row in Table(el, doc).rows:
                    out.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(out)
    if ext in (".txt", ".md"):
        return data.decode("utf-8", errors="replace")
    raise RuntimeError("सिर्फ़ PDF, DOCX या TXT फ़ाइल भेजें।")


def find_answer_key(text):
    """Aakhri hisse me 'उत्तरमाला / Answer Key' ho to (prashn-bhaag, kunji) deta hai."""
    start = int(len(text) * 0.55)
    m = re.search(r"(उत्तर\s*(?:माला|कुंजी|सूची)|answer\s*key|answers?\s*:)", text[start:], flags=re.IGNORECASE)
    if not m:
        return text, ""
    pos = start + m.start()
    block = text[pos:pos + 4000]
    if len(re.findall(r"\b\d{1,3}\s*[\.\-\):]?\s*\(?[A-Da-dक-घ1-4]\)?(?=\s|,|;|$)", block)) < 10:
        return text, ""
    return text[:pos], block


def make_chunks(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    chunks, cur, size, fresh = [], [], 0, 0
    for line in lines:
        cur.append(line)
        size += len(line) + 1
        fresh += 1
        if size >= CHUNK_CHARS:
            chunks.append("\n".join(cur))
            cur = cur[-4:]          # thoda overlap, taaki beech me kata prashn na chhute
            size = sum(len(x) + 1 for x in cur)
            fresh = 0
    if cur and fresh:
        chunks.append("\n".join(cur))
    return chunks


def canon_subject(raw):
    s0 = str(raw or "").strip()
    for en, hi in SUBJ_HI.items():
        if s0.lower() == en.lower() or s0 == hi:
            return en
    short = {"geo": "Geography", "hist": "History", "pol": "Polity", "eco": "Economics", "sci": "Science", "ca": "Current Affairs",
             "gk": "General Knowledge", "math": "Maths", "maths": "Maths", "bio": "Biology", "chem": "Chemistry", "phy": "Physics"}
    if s0.lower() in short:
        return short[s0.lower()]
    found = extract_metadata(s0.lower())["subject"]
    alias = {"General": "", "General Science": "Science", "Hindi Grammar": "Hindi", "English Grammar": "English"}
    found = alias.get(found, found)
    return found or s0 or "General Knowledge"


def _items_from_raw(raw):
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    data = json.loads(raw)
    items = data.get("items") if isinstance(data, dict) else data
    out = []
    for it in items or []:
        try:
            q = str(it.get("q", "")).strip()
            opts = [str(x).strip() for x in (it.get("options") or []) if str(x).strip()]
            ans = int(it.get("answer", -1))
        except Exception:
            continue
        if not q or len(opts) < 4 or not 0 <= ans <= 3 or len(set(opts[:4])) < 4:
            continue
        facts = it.get("facts")
        facts = [str(f).strip() for f in facts if str(f).strip()][:2] if isinstance(facts, list) else ([str(facts).strip()] if facts else [])
        out.append({
            "q": [q, opts[:4], ans, facts],
            "subject": canon_subject(it.get("subject")),
            "chapter": str(it.get("chapter", "") or "").strip()[:40],
            "solved": it.get("answer_in_file") is False,
        })
    return out


def groq_items(prompt, tries=3):
    last = None
    for attempt in range(tries):
        try:
            return _items_from_raw(ask_groq(prompt, is_json=True))
        except Exception as e:
            last = e
            time.sleep(_backoff(e, attempt))
    raise last


def dedupe_items(items):
    seen, out = set(), []
    for it in items:
        k = re.sub(r"\W+", "", it["q"][0].lower())[:60]
        if k and k not in seen:
            seen.add(k)
            out.append(it)
    return out


def extract_mcqs(chunks, key, cache):
    items, failed = [], 0
    for i, ch in enumerate(chunks):
        if i not in cache:
            prompt = EXTRACT_PROMPT.replace("__KEY__", ("\nउत्तर-कुंजी (प्रश्न-संख्या से मिलाओ):\n" + key + "\n") if key else "").replace("__TEXT__", ch)
            try:
                cache[i] = groq_items(prompt)
            except Exception as e:
                print("chunk failed:", i, e)
                cache[i] = []
                failed += 1
            time.sleep(1)
        items.extend(cache[i])
    return dedupe_items(items), failed


def generate_from_text(chunks, count, note):
    k = max(1, min(len(chunks), -(-count // 8)))
    picks = sorted({int(i * len(chunks) / k) for i in range(k)})
    per = -(-count // len(picks))
    items = []
    for i in picks:
        prompt = GEN_FROM_TEXT_PROMPT.replace("__N__", str(min(per, 12))).replace("__NOTE__", ("\n" + note.strip(" ()")) if note else "").replace("__TEXT__", chunks[i])
        try:
            items.extend(groq_items(prompt))
        except Exception as e:
            print("generate chunk failed:", i, e)
        time.sleep(1)
    return dedupe_items(items)[:count]


# ---------------------------------------- Kharab-font PDF / scan: OCR + shuddh text + analysis
MAX_OCR_PAGES = 40
ENRICH = os.environ.get("ENRICH_ANALYSIS", "1") != "0"
TESS_LANG = os.environ.get("TESS_LANG", "hin+eng")
VISION_MODELS = [m for m in (os.environ.get("GROQ_VISION_MODEL"),
                             "meta-llama/llama-4-scout-17b-16e-instruct",
                             "meta-llama/llama-4-maverick-17b-128e-instruct") if m]

OCR_PROMPT = """यह किसी परीक्षा-पेपर / किताब का एक पेज है। पेज पर लिखा पूरा टेक्स्ट पढ़ने के क्रम में, जैसा लिखा है बिल्कुल वैसा ही लिखो।
- हिन्दी (देवनागरी) की मात्राएँ और वर्तनी सही रखो; English जैसा है वैसा रखो।
- प्रश्न-संख्या, विकल्प (A)(B)(C)(D) या (1)(2)(3)(4) और उत्तर-कुंजी भी वैसे ही रखो।
- अपनी तरफ से कुछ जोड़ो, बदलो या समझाओ मत।
केवल पेज का टेक्स्ट दो।"""

ENRICH_PROMPT = """
नीचे MCQ प्रश्नों की सूची JSON में है (PDF/फ़ाइल से निकाले गए)। हर प्रश्न के लिए:
1. "q" और "options" में वर्तनी, मात्रा और टूटे हुए शब्दों की गलतियाँ ठीक करो ताकि भाषा शुद्ध हो। अर्थ, नाम, संख्याएँ, तारीख़ें बिल्कुल न बदलो; विकल्प ठीक चार रहें और उनका क्रम वही रहे।
2. "explanation" में सही उत्तर ("answer" index वाला विकल्प) की 1-2 पंक्ति की स्पष्ट व्याख्या दो।
3. "facts" में उसी प्रश्न से जुड़े 8 महत्वपूर्ण, परीक्षा-उपयोगी तथ्य दो (हिन्दी में, हर तथ्य एक छोटी पंक्ति)। गलत या मनगढ़ंत तथ्य मत दो।
4. प्रश्नों की संख्या और क्रम वही रखो।
केवल वैध JSON दो: {"items":[{"q":"","options":["","","",""],"explanation":"","facts":["","","","","","","",""]}]}

इनपुट:
__DATA__
"""


def looks_garbled(text):
    """PDF ka text-layer toota ho (matra akele / ulte) to True."""
    dev = len(re.findall(r"[\u0900-\u097F]", text))
    if dev < 100:
        return False
    orphan = len(re.findall(r"(?:^|[\s\W\d])[\u093E-\u094D\u0901-\u0903]", text, flags=re.M))
    return orphan >= 6 or orphan / dev > 0.004


def render_pdf_pages(data, max_pages=MAX_OCR_PAGES):
    """PDF ke pages ko JPEG images me badalta hai. (images, kul pages)."""
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=data, filetype="pdf")
        out = []
        for i, pg in enumerate(doc):
            if i >= max_pages:
                break
            out.append(pg.get_pixmap(dpi=130).tobytes("jpeg"))
        return out, len(doc)
    except ImportError:
        pass
    try:
        from pdf2image import convert_from_bytes
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("OCR ke liye pymupdf install nahi hai। GitHub workflow ki pip install lain me pymupdf jodein।")
    total = len(PdfReader(io.BytesIO(data)).pages)
    out = []
    for im in convert_from_bytes(data, dpi=130, first_page=1, last_page=min(total, max_pages)):
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=80)
        out.append(buf.getvalue())
    return out, total


def ocr_page_vision(img):
    b64 = base64.b64encode(img).decode()
    last = None
    for model in VISION_MODELS:
        for attempt in range(2):
            try:
                resp = get_client().chat.completions.create(
                    model=model, temperature=0, max_tokens=4000,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": OCR_PROMPT},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                    ]}],
                )
                return resp.choices[0].message.content or ""
            except Exception as e:
                last = e
                if "429" in str(e) or "rate" in str(e).lower():
                    time.sleep(_backoff(e, attempt))
                    continue
                break
    raise RuntimeError("Vision OCR error: " + str(last)[:200])


def ocr_page_tesseract(img):
    import pytesseract
    from PIL import Image
    return pytesseract.image_to_string(Image.open(io.BytesIO(img)), lang=TESS_LANG)


def ocr_pdf(data):
    """(text, kul pages, fail hue pages). Pehle Groq vision, na chale to tesseract."""
    imgs, total = render_pdf_pages(data)
    texts, fails, last = [], 0, None
    for img in imgs:
        t = ""
        try:
            t = ocr_page_vision(img)
        except Exception as e1:
            last = e1
            try:
                t = ocr_page_tesseract(img)
            except Exception:
                pass
        if not t.strip():
            fails += 1
        texts.append(t)
        time.sleep(1)
    if fails == len(imgs):
        raise RuntimeError(str(last)[:200] if last else "pages se text nahi mila")
    return "\n".join(texts), total, fails


def enrich_items(items, batch=6):
    """Har prashn ki vartani sudharta hai aur 8 tathya + vyakhya (purane jaisa analysis) jodta hai."""
    done = 0
    for i in range(0, len(items), batch):
        part = items[i:i + batch]
        data = json.dumps([{"q": it["q"][0], "options": it["q"][1], "answer": it["q"][2]} for it in part], ensure_ascii=False)
        prompt = ENRICH_PROMPT.replace("__DATA__", data)
        res = None
        for attempt in range(2):
            try:
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", ask_groq(prompt, is_json=True, temperature=0.2).strip())
                res = json.loads(raw).get("items")
                if isinstance(res, list) and len(res) == len(part):
                    break
                res = None
            except Exception as e:
                print("enrich failed:", e)
                time.sleep(_backoff(e, attempt))
        if not res:
            continue
        for it, r in zip(part, res):
            if not isinstance(r, dict):
                continue
            q = it["q"]
            nq = str(r.get("q", "")).strip()
            if nq and difflib.SequenceMatcher(None, q[0], nq).ratio() >= 0.4:
                q[0] = nq
            nopts = [str(x).strip() for x in (r.get("options") or [])]
            if len(nopts) == 4 and all(nopts) and len(set(nopts)) == 4 and all(
                    difflib.SequenceMatcher(None, a, b).ratio() >= 0.4 for a, b in zip(q[1], nopts)):
                q[1] = nopts
            facts = [str(x).strip() for x in (r.get("facts") or []) if str(x).strip()]
            expl = str(r.get("explanation", "")).strip()
            if expl:
                facts = [expl] + facts
            if facts:
                q[3] = facts[:9]
                done += 1
        time.sleep(1)
    return done


_EXCL_RE = re.compile(r"(chod\s*kar|chhod\s*kar|chodkar|छोड़\s*कर|छोड़कर|छोडकर|हटाकर|हटा\s*कर|except|excluding|exclude|without|बिना)")
_INCL_RE = re.compile(r"(?:sirf|सिर्फ़|सिर्फ|केवल|only)\b(.*)")


def _subjects_in(seg):
    """Text me jo vishay ke naam aaye (English ya Hindi) unka set deta hai."""
    low = seg.lower()
    names = []
    for en, hi in SUBJ_HI.items():
        names += [(en.lower(), en), (hi, en)]
    names += [("current affair", "Current Affairs"), ("करेंट", "Current Affairs")]
    names.sort(key=lambda x: -len(x[0]))        # lambe naam pehle: 'social science' ko 'science' na samjhe
    found = set()
    for n, en in names:
        if n and n in low:
            found.add(en)
            low = low.replace(n, " ")
    return found


POLISH_PROMPT = """
नीचे MCQ (JSON) हैं। इनका हिन्दी टेक्स्ट PDF/Word से निकालने की वजह से बिगड़ा हो सकता है (जैसे 'सबहार' = 'बिहार', 'सकि' = 'किस', बिखरी मात्राएँ 'िएक' = 'एक', अधूरे या टूटे शब्द)।

हर प्रश्न (q), विकल्प (options) और तथ्य (facts) को संदर्भ से समझकर शुद्ध, सही वर्तनी वाली हिन्दी में दोबारा लिखो।

नियम:
1. अर्थ, तथ्य, नाम, तिथि, संख्या, विकल्पों का क्रम और संख्या बिल्कुल न बदलो; सही उत्तर वही रहेगा।
2. जो टेक्स्ट पहले से सही है या English में है, उसे वैसा ही रखो।
3. कोई नया तथ्य मत जोड़ो।
4. केवल JSON दो, items की संख्या इनपुट के बराबर और उसी क्रम में: {"items":[{"q":"","options":["","","",""],"facts":[""]}]}

इनपुट:
__DATA__
"""


def polish_items(items, batch=12):
    """Extract kiye gaye (bigde) Hindi prashnon ko shuddh Hindi me sudharta hai."""
    fixed = 0
    for start in range(0, len(items), batch):
        part = items[start:start + batch]
        if not any(re.search(r"[\u0900-\u097F]", it["q"][0] + "".join(it["q"][1])) for it in part):
            continue
        data = json.dumps([{"q": it["q"][0], "options": it["q"][1], "facts": it["q"][3]} for it in part], ensure_ascii=False)
        try:
            raw = ask_groq(POLISH_PROMPT.replace("__DATA__", data), is_json=True)
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
            got = json.loads(raw).get("items")
            if not isinstance(got, list) or len(got) != len(part):
                raise ValueError("items ki sankhya mel nahi khati")
        except Exception as e:
            print("polish batch failed:", e)
            continue
        for it, g in zip(part, got):
            try:
                q = str(g["q"]).strip()
                opts = [str(o).strip() for o in g["options"]]
            except Exception:
                continue
            old_q = it["q"][0]
            if not q or len(opts) != 4 or not all(opts) or len(set(opts)) < 4:
                continue
            if not 0.4 <= len(q) / max(1, len(old_q)) <= 2.5:
                continue
            it["q"][0], it["q"][1] = q, opts
            facts = g.get("facts")
            if isinstance(facts, list) and any(str(f).strip() for f in facts):
                it["q"][3] = [str(f).strip() for f in facts if str(f).strip()][:2]
            fixed += 1
        time.sleep(1)
    return fixed


def doc_options(caption):
    """Caption se: kaun sa type, kitne prashn, kaise groups."""
    cap = (caption or "").strip()
    low = cap.lower()
    first = low.split()[0].split("@")[0] if low.split() else ""
    command = "/test"
    for cmd_name in ("/mock", "/quiz", "/practice", "/pyq", "/test"):
        if first == cmd_name:
            command = cmd_name
    if first not in ("/mock", "/quiz", "/practice", "/pyq", "/test"):
        if re.search(r"\bmock\b|मॉक", low):
            command = "/mock"
        elif re.search(r"quiz|क्विज", low):
            command = "/quiz"
        elif re.search(r"practice|अभ्यास|\bset\b|सेट", low):
            command = "/practice"
        elif re.search(r"\bpyq\b|पिछले", low):
            command = "/pyq"
    cap_clean, difficulty = extract_difficulty(cap)
    m = (re.search(r"(\d{1,3})\s*(?:q\b|mcqs?|prashn|prasn|prshn|sawal|saval|swal|ques\w*|प्रश्न|सवाल|questions?)", cap, re.IGNORECASE)
         or re.search(r"(?:\bq|mcqs?|questions?|prashn|sawal)\s*[:\-]?\s*(\d{1,3})\b", cap, re.IGNORECASE)
         or re.fullmatch(r"\s*/?\w*\s+(\d{1,3})\s*", cap_clean))
    count = int(m.group(1)) if m else None
    if re.search(r"मिलाकर|मिक्स|mixed|combine|एक ही|ek hi|ek test|single|one test|सब मिला", low):
        group = "combined"
    elif re.search(r"chapter|अध्याय|चैप्टर|टॉपिक|topic", low):
        group = "chapter"
    elif re.search(r"subject|विषय|अलग|alag|wise", low):
        group = "subject"
    else:
        group = "combined" if count else "subject"   # ginti likhi ho to ek hi test

    exclude, include = set(), set()
    mk = _EXCL_RE.search(low)
    if mk:
        after_first = re.match(r"except|exclud|without|बिना", mk.group(0))
        exclude = _subjects_in(low[mk.end():] if after_first else low[:mk.start()])
    else:
        mi = _INCL_RE.search(low)
        if mi:
            include = _subjects_in(mi.group(1))
        if not include:
            include = _subjects_in(low)      # caption me sirf kisi vishay ka naam ho, jaise "Current affairs ke quiz banaye"
    return {"command": command, "count": count, "difficulty": difficulty, "group": group,
            "exclude": exclude, "include": include}


def write_test_page(topic, command, questions, seq, mixed=False, meta_topic=None):
    info = command_info(command)
    count = len(questions)
    test_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + f"-{seq:02d}"
    filename = f"{safe_slug(topic)}-{count}q-{test_id}.html"
    folder = info["folder"]
    os.makedirs(folder, exist_ok=True)
    try:
        translations = translate_questions(questions)
    except Exception as e:
        print("Translation skipped:", e)
        translations = {}
    page = make_test_page(topic, count, questions, test_id, translations)
    with open(os.path.join(folder, filename), "w", encoding="utf-8") as f:
        f.write(page)
    url = BASE_URL + folder + "/" + quote(filename)
    update_registry(topic, count, command, url, test_id, mixed=mixed, meta_topic=meta_topic)
    return url


def split_message(lines, limit=3500):
    parts, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        parts.append(cur)
    return parts


def handle_document(chat_id, doc, caption):
    name = doc.get("file_name") or "file"
    process_document(chat_id, name, doc.get("file_size") or 0, lambda: download_telegram_file(doc["file_id"]), caption)


def process_document(chat_id, name, size, loader, caption, title=None):
    ext = os.path.splitext(name.lower())[1]
    if ext not in (".pdf", ".docx", ".txt"):
        send_message(chat_id, "❌ सिर्फ़ PDF, DOCX या TXT फ़ाइल से test बन सकता है। (पुरानी .doc फ़ाइल को Word में DOCX करके भेजें)")
        return
    if size > MAX_FILE_MB * 1024 * 1024:
        send_message(chat_id, f"❌ फ़ाइल {MAX_FILE_MB} MB से बड़ी है। उसे छोटे हिस्सों में बाँटकर भेजें।")
        return

    opt = doc_options(caption)
    command, count, group = opt["command"], opt["count"], opt["group"]
    info = command_info(command)
    note = difficulty_note(opt["difficulty"])
    stem = (title or re.sub(r"[_\-]+", " ", os.path.splitext(name)[0])).strip()
    stem = re.sub(r"^[^\w\u0900-\u097F]+", "", stem)[:25] or "File"

    send_message(chat_id, f"📄 <b>{html.escape(name)}</b> पढ़ रहा हूँ...")
    try:
        data = loader()
        text = re.sub(r"[ \t]+", " ", extract_text(data, name))
        warns = []
        if ext == ".pdf" and (len(text.strip()) < 200 or looks_garbled(text)):
            send_message(chat_id, "🔍 इस PDF का टेक्स्ट सीधे नहीं पढ़ा जा रहा (फ़ॉन्ट खराब या स्कैन)। पेज देखकर पढ़ रहा हूँ, थोड़ा समय लगेगा...")
            try:
                ocr_text, total_pages, ocr_fail = ocr_pdf(data)
                if len(ocr_text.strip()) >= 200:
                    text = re.sub(r"[ \t]+", " ", ocr_text)
                    if ocr_fail:
                        warns.append(f"⚠️ {ocr_fail} पेज पढ़ नहीं पाया, उनके प्रश्न छूट सकते हैं।")
                    if total_pages > MAX_OCR_PAGES:
                        warns.append(f"⚠️ PDF में {total_pages} पेज हैं, सिर्फ़ पहले {MAX_OCR_PAGES} पढ़े गए।")
                elif len(text.strip()) < 200:
                    raise RuntimeError("OCR से भी पर्याप्त टेक्स्ट नहीं मिला।")
            except Exception as e:
                if len(text.strip()) < 200:
                    raise RuntimeError("PDF का टेक्स्ट पढ़ नहीं पाया: " + str(e)[:300])
                warns.append("⚠️ PDF का टेक्स्ट खराब था और पेज पढ़ने (OCR) में दिक्कत आई, इसलिए प्रश्नों में गलतियाँ हो सकती हैं: " + str(e)[:150])
        if len(text.strip()) < 200:
            raise RuntimeError("फ़ाइल से लगभग कोई टेक्स्ट नहीं मिला।")
        body, key = find_answer_key(text)
        chunks = make_chunks(body)
        truncated = len(chunks) > MAX_CHUNKS
        chunks = chunks[:MAX_CHUNKS]
        send_message(chat_id, f"🤖 {len(text):,} अक्षर, {len(chunks)} हिस्से। प्रश्न ढूँढ रहा हूँ... (समय लग सकता है)")

        # pehle 3 hisse jaanch kar dekho: file me taiyar MCQ hain ya sirf padhne ki saamagri
        cache, mode, failed = {}, "extract", 0
        probe = sorted({0, len(chunks) // 2, len(chunks) - 1})
        probe_found = []
        for i in probe:
            got, f = extract_mcqs([chunks[i]], key, {})
            cache[i] = got
            failed += f
            probe_found += got
        if len(dedupe_items(probe_found)) >= 3:
            items, f2 = extract_mcqs(chunks, key, cache)
            failed += f2
        else:
            mode = "generate"
            items = generate_from_text(chunks, count or 20, note)
        if not items:
            raise RuntimeError("फ़ाइल से कोई प्रश्न नहीं मिला/बना। हो सकता है PDF का फ़ॉन्ट पढ़ा न जा रहा हो (पुराने Hindi फ़ॉन्ट) या फ़ाइल बहुत छोटी हो।")

        excl, incl = opt["exclude"], opt["include"]
        present = {}
        for it in items:
            present[it["subject"]] = present.get(it["subject"], 0) + 1
        if excl:
            items = [it for it in items if it["subject"] not in excl]
        if incl:
            items = [it for it in items if it["subject"] in incl]
        if not items:
            have = ", ".join(f"{SUBJ_HI.get(k, k)} ({v})" for k, v in sorted(present.items(), key=lambda kv: -kv[1]))
            raise RuntimeError("चुने हुए विषय के प्रश्न फ़ाइल में नहीं मिले।\nफ़ाइल में ये विषय मिले: " + have + "\nइन्हीं में से लिखकर दोबारा भेजें, जैसे: 'इतिहास का quiz'")
        available = len(items)
        filt_note = (" (" + ", ".join(SUBJ_HI.get(x, x) for x in sorted(excl)) + " छोड़कर)") if excl else \
                    (" (सिर्फ़ " + ", ".join(SUBJ_HI.get(x, x) for x in sorted(incl)) + ")") if incl else ""

        if mode == "extract":
            send_message(chat_id, "✍️ हिन्दी की वर्तनी/मात्राएँ शुद्ध कर रहा हूँ...")
            polish_items(items)

        secs = parse_sections(caption) if mode == "extract" else None
        sec_warn = []
        if secs and len(secs) >= 2:
            picked = []
            for name, n in secs:
                subj = canon_subject(name)
                pool = [it for it in items if it["subject"] == subj]
                take = sorted(random.sample(range(len(pool)), min(n, len(pool))))
                picked += [pool[i] for i in take]
                if len(pool) < n:
                    sec_warn.append(f"{SUBJ_HI.get(subj, subj)}: {len(pool)}/{n}")
            if not picked:
                raise RuntimeError("लिखे हुए विषयों के प्रश्न फ़ाइल में नहीं मिले।")
            items = picked
            group = "combined"
            count = None
        if count and mode == "extract" and len(items) > count:
            keep = sorted(random.sample(range(len(items)), count))
            items = [items[i] for i in keep]
        solved = sum(1 for it in items if it["solved"])
        if ENRICH and items:
            send_message(chat_id, f"✍️ {len(items)} प्रश्नों की भाषा शुद्ध करके हर प्रश्न का विस्तृत analysis (8 बिंदु) बना रहा हूँ...")
            enrich_items(items)

        # sirf ek vishay ho to "Test 10Q 1" ki jagah file ke naam wala test banao
        if group == "combined" and len({it["subject"] for it in items}) == 1:
            group = "subject"

        # groups
        label = {"/test": "Test", "/mock": "Mock", "/quiz": "Quiz", "/practice": "Practice", "/pyq": "PYQ"}[command]
        groups = {}
        if group == "combined":
            groups[("", "")] = items
        else:
            for it in items:
                groups.setdefault((it["subject"], it["chapter"] or "अन्य" if group == "chapter" else ""), []).append(it)
            small = [k for k, v in groups.items() if len(v) < MIN_GROUP]
            if small and len(small) < len(groups) + 1:
                other = []
                for k in small:
                    other += groups.pop(k)
                if len(other) >= MIN_GROUP or not groups:
                    groups[("General Knowledge", "अन्य")] = groups.get(("General Knowledge", "अन्य"), []) + other
                else:  # bahut kam bache: sabse bade group me jod do
                    big = max(groups, key=lambda k: len(groups[k]))
                    groups[big] += other

        send_message(chat_id, f"✅ {len(items)} प्रश्न {'फ़ाइल से निकाले' if mode == 'extract' else 'फ़ाइल की सामग्री से बनाए'}{filt_note}। अब {len(groups)} test बना रहा हूँ...")

        made, seq = [], 0
        for (subj, chap), qs in groups.items():
            parts = [qs[i:i + MAX_PER_TEST] for i in range(0, len(qs), MAX_PER_TEST)]
            for pi, part in enumerate(parts, 1):
                questions = [p["q"] for p in part]
                n = len(questions)
                suffix = f" भाग {pi}" if len(parts) > 1 else ""
                if group == "combined":
                    base = f"{label} {n}Q"
                    topic = f"{base} {next_series_number(base)}"
                    meta, mixed = None, True
                else:
                    hi = SUBJ_HI.get(subj, subj)
                    middle = chap if group == "chapter" else stem
                    topic = f"{hi} — {middle} {n}Q{suffix}"
                    meta, mixed = (subj if subj in SUBJ_HI else None), False
                seq += 1
                url = write_test_page(topic, command, questions, seq, mixed=mixed, meta_topic=meta)
                made.append((topic, n, url))

        if not git_publish(f"Generate from file: {name} ({len(items)}Q)"):
            raise RuntimeError("GitHub पर publish नहीं हो पाया, link काम नहीं करेगा")

        lines = [f"✅ <b>{len(made)} {info['label']} तैयार!</b>", ""]
        for topic, n, url in made:
            lines.append(f'• {html.escape(topic)} ({n} प्रश्न)\n' + link_line(info["btn_text"], url, tag=topic) + "\n")
        extra = list(warns)
        if solved:
            extra.append(f"⚠️ {solved} प्रश्नों का उत्तर फ़ाइल में नहीं मिला, AI ने खुद हल किया है। एक बार जाँच लें।")
        if failed:
            extra.append(f"⚠️ {failed} हिस्से नहीं पढ़ पाया (Groq error), उनके प्रश्न छूट गए हो सकते हैं।")
        if sec_warn:
            extra.append("⚠️ कुछ विषयों के प्रश्न कम मिले (मिले/माँगे): " + ", ".join(sec_warn))
        if count and available < count:
            extra.append(f"⚠️ आपने {count} प्रश्न माँगे थे, फ़ाइल में सिर्फ़ {available} मिले।")
        if truncated:
            extra.append(f"⚠️ फ़ाइल बहुत बड़ी थी, सिर्फ़ शुरू के {MAX_CHUNKS} हिस्से पढ़े गए।")
        extra.append("(GitHub Pages पर लिंक चालू होने में 1-2 मिनट लग सकते हैं)")
        for part in split_message(lines + [""] + extra):
            send_message(chat_id, part)
    except Exception as e:
        send_message(chat_id, f"❌ फ़ाइल से test नहीं बन पाया\n\n{html.escape(str(e)[:1200])}")


# ----------------------------------- App me Admin ne jo documents save kiye
def _html_text(raw):
    """HTML se saaf text (script/style hata kar)."""
    raw = str(raw or "")
    m = re.search(r'class="content"[^>]*>(.*)', raw, flags=re.S)
    if m:
        raw = m.group(1)
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|h[1-6]|li|tr|ul|ol)>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\n\s*\n+", "\n", html.unescape(raw)).strip()


def _notes_page_path(e):
    url = str(e.get("url", ""))
    if not url.startswith(BASE_URL):
        return None
    rel = unquote(url[len(BASE_URL):].split("?")[0].split("#")[0])
    if rel.startswith("notes/") and rel.endswith(".html") and ".." not in rel and os.path.isfile(rel):
        return rel
    return None


def _material_entries():
    """App me save hui cheezein jinse test ban sakta hai:
    upload ki gayi file, app me likha note, aur bot ke bane notes."""
    try:
        with open(STUDY_MATERIAL_FILE, "r", encoding="utf-8") as f:
            reg = json.load(f)
    except Exception:
        return []
    out = []
    for e in reg if isinstance(reg, list) else []:
        if not isinstance(e, dict) or not e.get("title"):
            continue
        t = e.get("type")
        if t == "file" and str(e.get("path", "")).startswith("materials/"):
            out.append(e)
        elif t == "note" and e.get("html"):
            out.append(e)
        elif t == "notes" and _notes_page_path(e):
            out.append(e)
    return out


def material_titles():
    return [str(e.get("title")) for e in _material_entries()][:40]


_MAT_STOP = {"se", "ka", "ki", "ke", "ko", "me", "mein", "par", "book", "ncert", "pdf", "file", "paper", "papers",
             "notes", "note", "quiz", "test", "mock", "practice", "banao", "bana", "do", "dijiye", "kar", "karo",
             "from", "of", "the", "and", "chapter", "prashn", "sawal", "question", "questions", "mcq",
             "से", "का", "की", "के", "को", "में", "पर", "किताब", "पुस्तक", "बुक", "फ़ाइल", "पेपर", "नोट्स", "प्रश्न",
             "बनाओ", "बनाइए", "टेस्ट", "क्विज़"}


def find_material(query):
    """(mila hua, ek se zyada/sujhav) deta hai."""
    import difflib
    files = _material_entries()
    q = _norm_title(query)
    if not q or not files:
        return [], []
    exact = [e for e in files if _norm_title(e["title"]) == q]
    if exact:
        return exact[:1], []
    part = [e for e in files if q in _norm_title(e["title"]) or _norm_title(e["title"]) in q]
    if len(part) == 1:
        return part, []
    if len(part) > 1:
        return [], part[:8]
    qt = [w for w in re.findall(r"[\w\u0900-\u097F]+", q) if w not in _MAT_STOP and not re.fullmatch(r"\d+q?|q\d+", w)]
    if qt:
        sc = [(sum(1 for w in qt if w in _norm_title(e["title"])), e) for e in files]
        best = max(n for n, _ in sc)
        if best and best / len(qt) >= 0.5:
            top = [e for n, e in sc if n == best]
            return (top[:1], []) if len(top) == 1 else ([], top[:8])
    scored = sorted(files, key=lambda e: -difflib.SequenceMatcher(None, q, _norm_title(e["title"])).ratio())
    ratios = [difflib.SequenceMatcher(None, q, _norm_title(e["title"])).ratio() for e in scored]
    if ratios[0] >= 0.6 and (len(ratios) == 1 or ratios[0] - ratios[1] >= 0.08):
        return scored[:1], []
    return [], scored[:5]


def handle_material(chat_id, query, caption):
    files = _material_entries()
    if not files:
        send_message(chat_id, "❌ ऐप के Study Material में अभी कोई फ़ाइल सेव नहीं है। पहले Admin Login करके फ़ाइल upload करें।")
        return
    match, many = find_material(query)
    if not match:
        names = "\n".join("• " + html.escape(str(e.get("title"))) for e in (many or files[:8]))
        send_message(chat_id, "❓ कौन सी फ़ाइल? पूरा नाम लिखें:\n" + names)
        return
    e = match[0]
    kind = e.get("type")
    title = str(e.get("title"))
    if kind == "file":
        path = str(e.get("path", ""))
        if ".." in path or not path.startswith("materials/") or not os.path.isfile(path):
            send_message(chat_id, "❌ यह फ़ाइल बॉट को अभी नहीं मिली। upload के 1-2 मिनट बाद फिर कोशिश करें।")
            return

        def loader():
            with open(path, "rb") as f:
                return f.read()

        process_document(chat_id, os.path.basename(path), os.path.getsize(path), loader, caption or "", title=title)
        return

    # app me likha note ya bot ka bana notes: HTML se text nikaal kar
    if kind == "note":
        text = _html_text(e.get("html"))
    else:
        with open(_notes_page_path(e), "r", encoding="utf-8") as f:
            text = _html_text(f.read())
    data = text.encode("utf-8")
    process_document(chat_id, "note.txt", len(data), lambda: data, caption or "", title=title)


def rebuild_pages():
    """Purane test/quiz pages ko naye index.html ke design/code me badalta hai (sawal wahi rehte hain)."""
    path = _find_index_html()
    if not path:
        return 0, 0, "index.html nahi mila"
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    a, b = src.find("/*GK_DATA_START*/"), src.find("/*GK_DATA_END*/")
    if a < 0 or b < a:
        return 0, 0, "index.html me GK_DATA marker nahi mila"
    changed = total = 0
    for folder in ("tests", "quiz", "practice"):
        if not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            if not fn.endswith(".html"):
                continue
            p = os.path.join(folder, fn)
            with open(p, "r", encoding="utf-8") as f:
                old = f.read()
            oa, ob = old.find("/*GK_DATA_START*/"), old.find("/*GK_DATA_END*/")
            if oa < 0 or ob < oa:
                continue
            total += 1
            page = src[:a] + old[oa:ob] + src[b:]
            m = re.search(r"<title>.*?</title>", old, flags=re.S)
            if m:
                page = re.sub(r"<title>.*?</title>", lambda _m: m.group(0), page, count=1, flags=re.S)
            if page != old:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(page)
                changed += 1
    return changed, total, ""


def handle_rebuild(chat_id):
    send_message(chat_id, "🔄 purane test/quiz pages naye design me badal raha hoon...")
    changed, total, err = rebuild_pages()
    if err:
        send_message(chat_id, "❌ " + html.escape(err))
        return
    if changed and not git_publish(f"Rebuild {changed} test pages with latest app"):
        send_message(chat_id, "❌ GitHub par publish nahi ho paya")
        return
    send_message(chat_id, f"✅ {changed} pages naye ho gaye (kul {total} me se). Link chalu hone me 1-2 minute lag sakte hain.")


def handle_delete(chat_id, name):
    if not str(name).strip():
        send_message(chat_id, "❓ किसे हटाना है? जैसे: <b>Test 40Q 1 delete कर दो</b>")
        return
    deleted, many = delete_items(name)
    if many:
        send_message(chat_id, "❓ कई मिले, पूरा नाम लिखें:\n" + "\n".join("• " + html.escape(t) for t in many[:10]))
    elif deleted:
        git_publish("Delete: " + ", ".join(deleted)[:80])
        send_message(chat_id, "🗑 हटा दिया: " + ", ".join(html.escape(t) for t in deleted))
    else:
        send_message(chat_id, "❌ इस नाम का कुछ नहीं मिला: " + html.escape(str(name)))


def process_update(update):
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    doc = message.get("document")
    text = (message.get("text") or message.get("caption") or "").strip()
    if not chat_id or (not text and not doc):
        return

    word = text.split()[0].lower().split("@")[0] if text else ""
    if word == "/id":
        send_message(chat_id, f"आपका chat id: {chat_id}")
        return

    if ALLOWED and str(chat_id) not in ALLOWED:
        send_message(chat_id, "यह bot निजी है।")
        return

    if doc:
        return handle_document(chat_id, doc, text)

    if word in ("/start", "/help"):
        send_message(chat_id, HELP_TEXT)
        return
    if word == "/rebuild":
        return handle_rebuild(chat_id)
    if word in ("/doc", "/file"):
        rest = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
        name, _, cap = rest.partition("|")
        return handle_material(chat_id, name.strip(), cap.strip())
    if word == "/delete":
        cmd = {"command": "/delete", "topic": text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""}
        return handle_delete(chat_id, cmd["topic"])

    cmd = parse_command(text)
    if not cmd:
        if text.startswith("/"):
            send_message(chat_id, "❓ यह command मुझे समझ नहीं आया।\n\n" + HELP_TEXT)
            return
        # seedhe likha sandesh: Groq se samajh kar kaam shuru
        send_message(chat_id, "🤖 आपकी बात समझ रहा हूँ...")
        try:
            cmd, reply = natural_command(text)
        except Exception as e:
            send_message(chat_id, f"❌ समझ नहीं पाया: {html.escape(str(e)[:300])}\n\n" + HELP_TEXT)
            return
        if not cmd:
            send_message(chat_id, html.escape(reply)[:3500] if reply != HELP_TEXT else reply)
            return

    if cmd.get("command") == "/delete":
        return handle_delete(chat_id, cmd.get("topic", ""))
    if cmd.get("command") == "/rebuild":
        return handle_rebuild(chat_id)
    if cmd.get("command") == "/doc":
        return handle_material(chat_id, cmd.get("topic", ""), cmd.get("caption", ""))

    if cmd.get("error"):
        send_message(chat_id, "❌ " + cmd["error"])
        return

    topic, count = cmd["topic"], cmd["count"]
    sections = cmd.get("sections")
    note = difficulty_note(cmd.get("difficulty"))
    if sections:
        topic = f"{topic} {next_series_number(topic)}"  # jaise: Test 40Q 1, Test 40Q 2
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
                + link_line("READ NOTES", file_url)
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
        f"📌 <b>प्रकार:</b> {info['label']}\n📝 <b>प्रश्न:</b> {count}\n"
        + ("📚 " + ", ".join(f"{html.escape(n)} {c}" for n, c in sections) + "\n" if sections else "")
        + (f"🎚 <b>कठिनाई:</b> {html.escape(cmd['difficulty'])}\n" if cmd.get("difficulty") else "")
        + "\nथोड़ा समय लगेगा..."
    )
    try:
        questions = generate_sections(sections, info["label"], note) if sections else generate_all(topic + note, count, info["label"])
        if not questions:
            raise RuntimeError("कोई प्रश्न नहीं बन पाया।")

        # kam sawal bane to page me asli count likho (timer bhi usi se banta hai)
        requested = count
        count = len(questions)
        shortfall = (
            f"\n⚠️ माँगे गए {requested} में से {count} प्रश्न ही बन पाए।"
            if count < requested else ""
        )

        if sections and count != requested:  # kam sawal bane to naam me asli ginti
            m = re.fullmatch(r"(\S+) \d+Q \d+", topic)
            if m:
                base = f"{m.group(1)} {count}Q"
                topic = f"{base} {next_series_number(base)}"
                safe_topic = html.escape(topic)

        test_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"{safe_slug(topic)}-{count}q-{test_id}.html"
        folder = info["folder"]
        os.makedirs(folder, exist_ok=True)

        try:
            translations = translate_questions(questions)
        except Exception as e:
            print("Translation skipped:", e)
            translations = {}

        page = make_test_page(topic, count, questions, test_id, translations)
        with open(os.path.join(folder, filename), "w", encoding="utf-8") as f:
            f.write(page)

        test_url = BASE_URL + folder + "/" + quote(filename)
        update_registry(topic, count, cmd["command"], test_url, test_id, mixed=bool(sections))
        if not git_publish(f"Generate test: {topic} ({count}Q)"):
            raise RuntimeError("GitHub पर publish नहीं हो पाया, link काम नहीं करेगा")

        formatted_message = (
            "✅ <b>Test तैयार है!</b>\n\n"
            f"🎯 <b>विषय:</b> {safe_topic}\n"
            f"📌 <b>प्रकार:</b> {info['label']}\n"
            f"📝 <b>प्रश्न:</b> {count}\n\n"
            + link_line(info["btn_text"], test_url)
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
                try:
                    cid = ((u.get("message") or {}).get("chat") or {}).get("id")
                    if cid:
                        send_message(cid, "❌ Bot में error आया: " + html.escape(str(e)[:300]))
                except Exception:
                    pass
    except Exception as e:
        print("Error fetching updates:", e)

    git_publish("Update bot state")


if __name__ == "__main__":
    main()
