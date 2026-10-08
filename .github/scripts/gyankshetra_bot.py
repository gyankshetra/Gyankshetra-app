#!/usr/bin/env python3
"""Gyankshetra Telegram bot  (Gemini)

Telegram command  ->  Gemini MCQs  ->  tests/<slug>-<n>q-<time>.html  ->  live link.
Runs from the repository root (GitHub Actions), reads ./index.html as the master UI.

Commands:  /test  /mock  /quiz  /practice  /pyq   <topic> <count>
Example :  /test इतिहास 25        (25Q भी चलेगा)
           /id                    (आपका chat id बताता है)
"""
import os
import re
import json
import time
import html
import subprocess
from datetime import datetime, timezone
from urllib.parse import quote

import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]

# Model preference
MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "").strip()] if m]
FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-1.5-flash"]

# Allowed Chat IDs
ALLOWED = {
    x.strip()
    for x in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
    if x.strip()
}

BASE_URL = "https://gyankshetra.github.io/Gyankshetra-app/"
TG = "https://api.telegram.org/bot" + BOT_TOKEN
POLL_SECONDS = 210
OFFSET_FILE = "content/telegram_offset.txt"
REGISTRY_FILE = "content/generated_tests.json"

# BATCH size 25 rakha hai taaki 50, 100 questions fast aur bina rate-limit ke banein
BATCH = 25


# ------------------------------------------------------------------ Telegram
def telegram(method, data=None):
    r = requests.post(TG + "/" + method, json=data or {}, timeout=60)
    r.raise_for_status()
    return r.json()


def send_message(chat_id, text):
    try:
        return telegram(
            "sendMessage",
            {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": False},
        )
    except Exception as e:
        print("sendMessage failed:", e)


# ------------------------------------------------------------------ Commands
COMMANDS = ["/test", "/mock", "/quiz", "/practice", "/pyq"]


def parse_command(text):
    parts = (text or "").strip().split()
    if not parts:
        return None
    command = parts[0].lower().split("@")[0]  # /test@botname -> /test
    if command not in COMMANDS:
        return None
    rest = parts[1:]
    count = 10
    if rest:
        m = re.fullmatch(r"(\d{1,3})[qQ]?", rest[-1])  # "25" या "25Q"
        if m:
            count = int(m.group(1))
            rest = rest[:-1]
    topic = " ".join(rest).strip() or "सामान्य विज्ञान"
    count = max(1, min(count, 100))
    return {"command": command, "topic": topic, "count": count}


def command_info(command):
    table = {
        "/quiz": ("quiz", "quiz", "quiz", "Quiz", "🎯"),
        "/practice": ("practice", "practice", "practice", "Practice", "📝"),
        "/pyq": ("practice", "pyq", "pyq", "PYQ Practice", "📚"),
        "/mock": ("tests", "test", "mock", "Mock Test", "🎯"),
        "/test": ("tests", "test", "test", "Test Series", "📝"),
    }
    folder, box, kind, label, emoji = table.get(command, table["/test"])
    return {"folder": folder, "box": box, "type": kind, "label": label, "emoji": emoji}


# -------------------------------------------------------------------- Gemini
_client = None


def get_client():
    global _client
    if _client is None:
        from google import genai  # pip install google-genai

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
8. गलत या मनगढ़ंत तथ्य न बनाओ। पक्का न हो तो वह प्रश्न मत बनाओ।
9. STET / BPSC TRE स्तर का ध्यान रखो।
10. PYQ command में यदि verified source उपलब्ध नहीं है तो वास्तविक PYQ होने का दावा न करो।
11. केवल JSON दो, Markdown code fence नहीं।

सिर्फ इस structure में JSON दो:
{"questions":[{"question":"प्रश्न","options":["A","B","C","D"],"answer":0,"explanation":"व्याख्या","facts":["तथ्य 1","तथ्य 2","तथ्य 3","तथ्य 4","तथ्य 5","तथ्य 6","तथ्य 7","तथ्य 8"]}]}
"""


def discover_models():
    """API से वे flash models लाता है जो generateContent चला सकते हैं।"""
    try:
        found = []
        for m in get_client().models.list():
            name = (getattr(m, "name", "") or "").replace("models/", "")
            actions = getattr(m, "supported_actions", None) or []
            if "flash" not in name or "generateContent" not in actions:
                continue
            if any(x in name for x in ("image", "tts", "live", "audio", "embedding", "robotics", "computer")):
                continue
            found.append(name)

        def version(n):
            mm = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
            return float(mm.group(1)) if mm else 0.0

        found.sort(key=lambda n: (-version(n), "preview" in n or "exp" in n, "lite" in n, n))
        print("Discovered models:", found[:6])
        return found
    except Exception as e:
        print("model discovery failed:", str(e)[:200])
        return []


def is_missing_model(err):
    return "404" in err or "NOT_FOUND" in err or "no longer available" in err


def is_busy(err):
    return any(x in err for x in (
        "503", "UNAVAILABLE", "500", "INTERNAL", "429", "RESOURCE_EXHAUSTED",
        "DEADLINE", "overloaded", "high demand",
    ))


def ask_gemini(prompt):
    from google.genai import types

    if not MODELS:
        MODELS.extend(discover_models()[:4] or FALLBACK_MODELS)

    dead, discovered, last = set(), False, ""
    for attempt in range(10):
        alive = [m for m in MODELS if m not in dead]
        if not alive:
            if discovered:
                break
            discovered = True
            MODELS.extend(m for m in discover_models() if m not in MODELS)
            MODELS.extend(m for m in FALLBACK_MODELS if m not in MODELS)
            continue
        model = alive[attempt % len(alive)]
        try:
            resp = get_client().models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.4, response_mime_type="application/json"
                ),
            )
            if resp.text:
                MODELS.remove(model)
                MODELS.insert(0, model)
                return resp.text
            last = "[%s] खाली जवाब" % model
        except Exception as e:
            last = "[%s] %s" % (model, str(e))
            if is_missing_model(last):
                print("Model unavailable:", model)
                dead.add(model)
                continue
        print("Gemini attempt", attempt + 1, "failed:", last[:300])
        time.sleep(min(8 * (attempt + 1), 20) if is_busy(last) else 3)
    raise RuntimeError("Gemini से जवाब नहीं मिला: " + last[:600])


def clean_questions(data):
    """Gemini का JSON -> app का format [प्रश्न, [4 विकल्प], उत्तर, [facts]]"""
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
    last = ""
    for _ in range(3):
        raw = ask_gemini(prompt).strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        try:
            qs = clean_questions(json.loads(raw))
            if qs:
                return qs
            last = "valid प्रश्न नहीं मिले"
        except Exception as e:
            last = str(e)
        print("Bad Gemini JSON:", last)
    raise RuntimeError("Gemini का JSON सही नहीं आया: " + last)


def norm(text):
    return re.sub(r"\s+", " ", text).strip().lower()


def generate_all(topic, count, mode):
    result, seen = [], []
    max_rounds = (count // BATCH) + 10
    rounds = 0
    while len(result) < count and rounds < max_rounds:
        rounds += 1
        need = min(BATCH, count - len(result))
        try:
            batch_qs = gemini_batch(topic, need, mode, seen)
            for q in batch_qs:
                key = norm(q[0])
                if key in seen or len(result) >= count:
                    continue
                seen.append(key)
                result.append(q)
        except Exception as e:
            print(f"Round {rounds} failed: {e}")
            if result:  # agar pehle kuch questions aa chuke hain to continue karne ki koshish karein
                continue
            else:
                raise e
    return result


# ------------------------------------------------------------ Master UI -> test
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
        raise RuntimeError("index.html में questions array पूरा नहीं मिला")
    return source[: m.start()] + "const questions=" + placeholder + source[end + 1 :]


def replace_function(source, name, replacement):
    m = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", source)
    if not m:
        raise RuntimeError("index.html में function " + name + "() नहीं मिला")
    end = _find_matching(source, m.end() - 1, "{", "}")
    if end < 0:
        raise RuntimeError("index.html में " + name + "() का अंत नहीं मिला")
    return source[: m.start()] + replacement + source[end + 1 :]


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
        "const TOTAL=%d,TIME=%d" % (count, count * 60), s, count=1)
    if not n:
        raise RuntimeError("index.html में 'const TOTAL=..,TIME=..' नहीं मिला")
    s = replace_questions(s, "__GK_QUESTIONS__")
    s = replace_function(s, "startQuiz", START_QUIZ)
    s = replace_function(s, "tick", TICK)

    for old, new in (
        ("'gyankshetraSaved'", "'gyankshetraSaved_%s'" % test_id),
        ("'gyankshetraProgress_'", "'gyankshetraProgress_%s_'" % test_id),
    ):
        if old not in s:
            raise RuntimeError("index.html में " + old + " नहीं मिला")
        s = s.replace(old, new)

    s = s.replace("test:'विलयन'", "test:__GK_TOPIC_JS__")
    s = s.replace("विलयन", "__GK_TOPIC_HTML__")

    for old, new in (
        ("20Q", "%dQ" % count),
        ("20 Questions", "%d Questions" % count),
        ("20 Marks", "%d Marks" % count),
        ("20 Minutes", "%d Minutes" % count),
        ("20 प्रश्न", "%d प्रश्न" % count),
        ("20 अंक", "%d अंक" % count),
        ("20 मिनट", "%d मिनट" % count),
        ("' / 20'", "' / '+TOTAL"),
        ("marks}/20`", "marks}/${TOTAL}`"),
        ("0 / 20</strong>", "0 / %d</strong>" % count),
        ("0 / 20</b>", "0 / %d</b>" % count),
        ('id="timer">20:00', 'id="timer">00:00'),
        (">1/20<", ">1/%d<" % count),
    ):
        s = s.replace(old, new)

    s = s.replace("__GK_TOPIC_JS__", js_safe(topic))
    s = s.replace("__GK_TOPIC_HTML__", html.escape(topic))
    s = s.replace("__GK_QUESTIONS__", js_safe(questions))
    if "__GK_" in s:
        raise RuntimeError("page बनाते समय placeholder बचा रह गया")
    return s


# ------------------------------------------------------------------ Registry
def update_registry(topic, count, command, url, test_id):
    os.makedirs("content", exist_ok=True)
    try:
        with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
            registry = json.load(f)
        if not isinstance(registry, list):
            registry = []
    except Exception:
        registry = []
    info = command_info(command)
    registry.insert(0, {
        "title": info["emoji"] + " " + topic,
        "questions": count,
        "type": info["type"],
        "url": url,
        "id": test_id, "topic": topic, "count": count, "command": command,
        "box": info["box"], "label": info["label"], "emoji": info["emoji"],
        "created": test_id,
    })
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(registry[:200], f, ensure_ascii=False, indent=2)


def git_publish(message):
    if not os.environ.get("GITHUB_ACTIONS"):
        return True
    try:
        run = lambda *a, **k: subprocess.run(a, check=k.get("check", True))
        run("git", "config", "user.name", "gyankshetra")
        run("git", "config", "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com")
        dirs = [d for d in ("content", "tests", "quiz", "practice") if os.path.isdir(d)]
        run("git", "add", *dirs)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return True
        run("git", "commit", "-m", message)
        run("git", "pull", "--rebase", "--autostash", check=False)
        run("git", "push")
        return True
    except Exception as e:
        print("git push failed:", e)
        return False


def safe_slug(text):
    text = re.sub(r"[^\w\u0900-\u097F]+", "-", text, flags=re.UNICODE).strip("-")
    return (text or "test")[:70]


# -------------------------------------------------------------------- Offset
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
HELP = (
    "🤖 Gyankshetra AI Bot तैयार है!\n\n"
    "/test इतिहास 20\n/quiz इतिहास 30\n/practice विज्ञान 50\n"
    "/mock रसायन 100\n/pyq विज्ञान 20\n\n"
    "प्रश्न-संख्या सबसे अंत में लिखें (जैसे: 20, 30, 50, 100)।"
)


def process_update(update):
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return

    word = text.split()[0].lower().split("@")[0]
    if word == "/id":
        send_message(chat_id, "आपका chat id: " + str(chat_id))
        return
    if ALLOWED and str(chat_id) not in ALLOWED:
        send_message(chat_id, "यह bot निजी है।")
        return
    if word in ("/start", "/help"):
        send_message(chat_id, HELP)
        return

    cmd = parse_command(text)
    if not cmd:
        return
    topic, count = cmd["topic"], cmd["count"]
    info = command_info(cmd["command"])

    send_message(
        chat_id,
        "🤖 Gemini AI प्रश्न तैयार कर रहा है...\n\n%s विषय: %s\n📌 प्रकार: %s\n📝 प्रश्न: %d\n\nथोड़ा समय लगेगा..."
        % (info["emoji"], topic, info["label"], count),
    )
    try:
        questions = generate_all(topic, count, info["label"])
        if len(questions) < count:
            raise RuntimeError("माँगे गए %d में केवल %d प्रश्न बन पाए।" % (count, len(questions)))

        test_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = "%s-%dq-%s.html" % (safe_slug(topic), count, test_id)
        folder = info["folder"]
        os.makedirs(folder, exist_ok=True)
        page = make_test_page(topic, count, questions, test_id)
        with open(os.path.join(folder, filename), "w", encoding="utf-8") as f:
            f.write(page)
        update_registry(topic, count, cmd["command"], folder + "/" + filename, test_id)

        pushed = git_publish("Generate test: %s (%dQ)" % (topic, count))
        note = (
            "(GitHub Pages को अपडेट होने में 1-2 मिनट लगते हैं; 404 आए तो थोड़ी देर बाद खोलें।)"
            if pushed
            else "⚠️ फ़ाइल बनी, पर GitHub पर push नहीं हो पाई। Actions का log देखें।"
        )
        send_message(
            chat_id,
            "✅ Test तैयार है!\n\n%s विषय: %s\n📌 प्रकार: %s\n📝 प्रश्न: %d\n\n🔗 LIVE TEST:\n%s\n\n%s"
            % (info["emoji"], topic, info["label"], count,
               BASE_URL + folder + "/" + quote(filename), note),
        )
    except Exception as e:
        send_message(chat_id, "❌ Test generate नहीं हो पाया\n\nError:\n" + str(e)[:2500])


# ---------------------------------------------------------------------- Main
def main():
    print("Gyankshetra Telegram Bot started")
    if not ALLOWED:
        print("WARNING: TELEGRAM_ALLOWED_CHAT_IDS empty!")
    
    offset = load_offset()
    print("Checking updates with offset:", offset)
    
    try:
        res = telegram("getUpdates", {"offset": offset, "timeout": 10})
        updates = res.get("result", [])
        for u in updates:
            process_update(u)
            offset = max(offset, u["update_id"] + 1)
        save_offset(offset)
    except Exception as e:
        print("Error fetching updates:", e)


if __name__ == "__main__":
    main()
