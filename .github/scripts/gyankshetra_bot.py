import os
import re
import json
import time
import html
import requests
from datetime import datetime
from urllib.parse import quote

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_KEY = os.environ["GROQ_API_KEY"]

TG = "https://api.telegram.org/bot" + BOT_TOKEN
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

MODEL = "openai/gpt-oss-20b"

BASE_URL = (
    "https://gyankshetra.github.io/"
    "Gyankshetra-app/"
)


# --------------------------------------------------
# TELEGRAM
# --------------------------------------------------

def telegram(method, data=None):
    r = requests.post(
        TG + "/" + method,
        json=data or {},
        timeout=60
    )
    r.raise_for_status()
    return r.json()


def send_message(chat_id, text):
    return telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": False
        }
    )


# --------------------------------------------------
# COMMAND PARSER
# --------------------------------------------------

def parse_command(text):

    parts = text.strip().split()

    if not parts:
        return None

    command = parts[0].lower()

    allowed = [
        "/test",
        "/mock",
        "/quiz",
        "/practice",
        "/pyq"
    ]

    if command not in allowed:
        return None

    count = 10
    topic = ""

    if len(parts) >= 2:

        try:
            count = int(parts[-1])
            topic = " ".join(parts[1:-1]).strip()

        except ValueError:
            topic = " ".join(parts[1:]) .strip()

    if not topic:
        topic = "सामान्य विज्ञान"

    count = max(1, min(count, 100))

    return {
        "command": command,
        "topic": topic,
        "count": count
    }


# --------------------------------------------------
# COMMAND ROUTING
# --------------------------------------------------

def command_info(command):

    if command == "/quiz":
        return {
            "folder": "quiz",
            "box": "quiz",
            "label": "Quiz",
            "emoji": "🎯"
        }

    if command == "/practice":
        return {
            "folder": "practice",
            "box": "practice",
            "label": "Practice",
            "emoji": "📝"
        }

    if command == "/pyq":
        return {
            "folder": "practice",
            "box": "pyq",
            "label": "PYQ Practice",
            "emoji": "📚"
        }

    if command == "/mock":
        return {
            "folder": "tests",
            "box": "test",
            "label": "Mock Test",
            "emoji": "🎯"
        }

    return {
        "folder": "tests",
        "box": "test",
        "label": "Test Series",
        "emoji": "📝"
    }


# --------------------------------------------------
# GROQ
# --------------------------------------------------

def groq_questions(topic, count, mode, previous):

    old = ""

    if previous:
        old = (
            "\nइन प्रश्नों को दोबारा न बनाएं:\n"
            + "\n".join(previous[-20:])
        )

    prompt = """
Gyankshetra परीक्षा ऐप के लिए MCQ तैयार करो।

विषय: TOPIC
प्रश्न संख्या: COUNT
प्रकार: MODE

नियम:

1. सभी प्रश्न उच्च-स्तरीय और परीक्षा उपयोगी हों।
2. प्रश्न हिन्दी में हों।
3. प्रत्येक प्रश्न के चार विकल्प हों।
4. सही उत्तर 0, 1, 2 या 3 हो।
5. प्रत्येक प्रश्न की स्पष्ट व्याख्या हो।
6. प्रत्येक प्रश्न के साथ 8 महत्वपूर्ण तथ्य हों।
7. प्रश्न एक-दूसरे से अलग हों।
8. गलत या मनगढ़ंत तथ्य न बनाओ।
9. STET / BPSC TRE स्तर का ध्यान रखो।
10. PYQ command में यदि verified source उपलब्ध नहीं है तो वास्तविक PYQ होने का दावा न करो।
11. केवल JSON दो।
12. Markdown code fence मत दो।

सिर्फ इस structure में JSON दो:

{
  "questions": [
    {
      "question": "प्रश्न",
      "options": [
        "विकल्प A",
        "विकल्प B",
        "विकल्प C",
        "विकल्प D"
      ],
      "answer": 0,
      "explanation": "व्याख्या",
      "facts": [
        "तथ्य 1",
        "तथ्य 2",
        "तथ्य 3",
        "तथ्य 4",
        "तथ्य 5",
        "तथ्य 6",
        "तथ्य 7",
        "तथ्य 8"
      ]
    }
  ]
}
"""

    prompt = prompt.replace("TOPIC", topic)
    prompt = prompt.replace("COUNT", str(count))
    prompt = prompt.replace("MODE", mode)
    prompt += old

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.35,
        "reasoning_effort": "low",
        "reasoning_format": "hidden",
        "max_completion_tokens": 7000,
        "response_format": {
            "type": "json_object"
        }
    }

    last_error = ""

    for attempt in range(5):

        try:

            response = requests.post(
                GROQ_URL,
                headers={
                    "Authorization": "Bearer " + GROQ_KEY,
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=120
            )

            if response.status_code == 429:
                time.sleep(8 + attempt * 5)
                continue

            if response.status_code >= 400:
                raise RuntimeError(
                    "Groq HTTP "
                    + str(response.status_code)
                    + ": "
                    + response.text[:1500]
                )

            data = response.json()

            content = data["choices"][0]["message"]["content"]

            content = content.strip()

            if content.startswith("```"):
                content = re.sub(
                    r"^```(?:json)?",
                    "",
                    content
                )
                content = re.sub(
                    r"```$",
                    "",
                    content
                )
                content = content.strip()

            parsed = json.loads(content)

            questions = parsed.get("questions")

            if not isinstance(questions, list):
                raise RuntimeError(
                    "Groq response में questions array नहीं मिला"
                )

            clean = []

            for q in questions:

                if not isinstance(q, dict):
                    continue

                question = str(
                    q.get("question", "")
                ).strip()

                options = q.get("options", [])

                explanation = str(
                    q.get("explanation", "")
                ).strip()

                facts = q.get("facts", [])

                try:
                    answer = int(q.get("answer", 0))
                except Exception:
                    answer = 0

                if not question:
                    continue

                if not isinstance(options, list):
                    continue

                if len(options) != 4:
                    continue

                if answer < 0 or answer > 3:
                    continue

                if not isinstance(facts, list):
                    facts = []

                facts = [
                    str(x).strip()
                    for x in facts
                    if str(x).strip()
                ]

                clean.append(
                    {
                        "question": question,
                        "options": [
                            str(x).strip()
                            for x in options
                        ],
                        "answer": answer,
                        "explanation": explanation,
                        "facts": facts[:8]
                    }
                )

            if not clean:
                raise RuntimeError(
                    "Groq ने valid questions नहीं दिए"
                )

            return clean

        except Exception as e:

            last_error = str(e)

            print(
                "Groq attempt",
                attempt + 1,
                "failed:",
                last_error
            )

            time.sleep(3)

    raise RuntimeError(last_error)


# --------------------------------------------------
# GENERATE ALL QUESTIONS
# --------------------------------------------------

def generate_all(topic, count, mode):

    result = []
    signatures = []

    while len(result) < count:

        remaining = count - len(result)

        batch_size = min(5, remaining)

        questions = groq_questions(
            topic,
            batch_size,
            mode,
            signatures
        )

        for q in questions:

            if len(result) >= count:
                break

            signature = (
                q["question"]
                .strip()
                .lower()
            )

            if signature in signatures:
                continue

            signatures.append(signature)
            result.append(q)

        if not questions:
            break

    return result[:count]


# --------------------------------------------------
# SLUG
# --------------------------------------------------

def safe_slug(text):

    text = re.sub(
        r"[^\w\u0900-\u097F]+",
        "-",
        text,
        flags=re.UNICODE
    )

    text = text.strip("-")

    if not text:
        text = "test"

    return text[:70]


# --------------------------------------------------
# QUESTIONS → MASTER UI ARRAY
# --------------------------------------------------

def make_arrays(questions):

    output = []

    for q in questions:

        output.append(
            [
                q["question"],
                q["options"],
                q["answer"],
                q.get("facts", [])
            ]
        )

    return output


# --------------------------------------------------
# MASTER UI HELPERS
# --------------------------------------------------

def _find_matching_js_bracket(text, start, opening="[", closing="]"):
    """Find the matching JS bracket while ignoring strings/comments."""
    depth = 0
    i = start
    n = len(text)
    quote = None
    escaped = False
    line_comment = False
    block_comment = False

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if line_comment:
            if ch == "\n":
                line_comment = False
            i += 1
            continue

        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
            else:
                i += 1
            continue

        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue

        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue

        if ch == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue

        if ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return i

        i += 1

    return -1


def _replace_questions_array(source, questions_json):
    m = re.search(r"\bconst\s+questions\s*=", source)
    if not m:
        raise RuntimeError("Master UI में questions array नहीं मिला")

    start = source.find("[", m.end())
    if start < 0:
        raise RuntimeError("Master UI में questions array का [ नहीं मिला")

    end = _find_matching_js_bracket(source, start, "[", "]")
    if end < 0:
        raise RuntimeError("Master UI में questions array बंद नहीं मिला")

    replacement = "const questions=" + questions_json
    source = source[:m.start()] + replacement + source[end + 1:]

    if source.count("const questions=") != 1:
        raise RuntimeError("Master UI में questions array replacement सुरक्षित नहीं है")

    return source


def _replace_js_function(source, function_name, replacement):
    pattern = re.compile(
        r"\bfunction\s+" + re.escape(function_name) + r"\s*\([^)]*\)\s*\{"
    )
    m = pattern.search(source)
    if not m:
        raise RuntimeError(
            "Master UI में " + function_name + "() नहीं मिला"
        )

    brace_start = source.find("{", m.start(), m.end())
    brace_end = _find_matching_js_bracket(source, brace_start, "{", "}")
    if brace_end < 0:
        raise RuntimeError(
            "Master UI में " + function_name + "() का end नहीं मिला"
        )

    return source[:m.start()] + replacement + source[brace_end + 1:]


# --------------------------------------------------
# MAKE MASTER TEST
# --------------------------------------------------

def make_master_test(topic, count, questions):

    master = "index.html"

    if not os.path.exists(master):
        raise RuntimeError(
            "index.html Master UI नहीं मिला"
        )

    with open(
        master,
        "r",
        encoding="utf-8"
    ) as f:
        source = f.read()

    arrays = make_arrays(questions)

    questions_json = json.dumps(
        arrays,
        ensure_ascii=False,
        separators=(",", ":")
    )

    # 1. TITLE
    source = re.sub(
        r"<title>.*?</title>",
        "<title>Gyankshetra — "
        + html.escape(topic)
        + " "
        + str(count)
        + "Q</title>",
        source,
        count=1,
        flags=re.S
    )

    # 2. TOTAL / TIME (ROBUST REGEX MATCHING)
    source = re.sub(
        r"const\s+TOTAL\s*=\s*\d+\s*,\s*TIME\s*=\s*[^;]+;",
        "const TOTAL=" + str(count) + ",TIME=" + str(count) + "*60;",
        source,
        count=1
    )

    # 3. QUESTIONS ARRAY (BRACKET MATCHING)
    source = _replace_questions_array(
        source,
        questions_json
    )

    # 4. TIMER REPLACEMENT (Replace entire startQuiz function)
    start_quiz_replacement = """function startQuiz(){
  qi=0;ans=Array(TOTAL).fill(null);time=TIME;reviewIndex=0;running=true;
  clearInterval(timerHandle);go('quiz');render();saveProgress();
  timerHandle=setInterval(tick,1000);
}"""

    try:
        source = _replace_js_function(
            source,
            "startQuiz",
            start_quiz_replacement
        )
    except Exception:
        pass

    # 5. TOPIC REPLACEMENT
    source = source.replace(
        "विलयन",
        topic
    )

    # 6. COUNT & MARKS TEXT REPLACEMENTS
    replacements = {
        "20Q": str(count) + "Q",
        "20 Questions": str(count) + " Questions",
        "20 प्रश्न": str(count) + " प्रश्न",
        "20 Marks": str(count) + " Marks",
        "20 अंक": str(count) + " अंक",
        "20 Minutes": str(count) + " Minutes",
        "20 मिनट": str(count) + " मिनट",
        "c+' / 20'": "c+' / " + str(count) + "'",
        "marks.textContent=c+' / 20'": "marks.textContent=c+' / " + str(count) + "'",
        "test:'विलयन'": "test:" + json.dumps(topic, ensure_ascii=False)
    }

    for old, new in replacements.items():
        source = source.replace(old, new)

    # 7. REMOVE QUESTION PALETTE
    source += """
<style>
#popupPalette{display:none !important;}
#paletteHint{display:none !important;}
.questionPopup .palette{display:none !important;}
</style>
"""

    return source


# --------------------------------------------------
# REGISTRY
# --------------------------------------------------

def update_registry(
    topic,
    count,
    command,
    relative_url,
    timestamp
):

    os.makedirs(
        "content",
        exist_ok=True
    )

    path = "content/generated_tests.json"

    if os.path.exists(path):

        try:

            with open(
                path,
                "r",
                encoding="utf-8"
            ) as f:
                registry = json.load(f)

        except Exception:
            registry = []

    else:
        registry = []

    if not isinstance(registry, list):
        registry = []

    info = command_info(command)

    item = {
        "id": timestamp,
        "topic": topic,
        "count": count,
        "command": command,
        "box": info["box"],
        "label": info["label"],
        "emoji": info["emoji"],
        "url": relative_url,
        "created": timestamp
    }

    registry.insert(
        0,
        item
    )

    registry = registry[:200]

    with open(
        path,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            registry,
            f,
            ensure_ascii=False,
            indent=2
        )


# --------------------------------------------------
# ADD GENERATED TESTS TO APP
# --------------------------------------------------

def update_master_index():

    path = "index.html"

    if not os.path.exists(path):
        return

    with open(
        path,
        "r",
        encoding="utf-8"
    ) as f:
        source = f.read()

    marker = "GYANKSHETRA_AUTO_GENERATED_TESTS"

    if marker in source:
        return

    script = r"""
<!-- GYANKSHETRA_AUTO_GENERATED_TESTS -->
<script>
(function(){

  fetch('content/generated_tests.json')
    .then(function(r){
      if(!r.ok) return [];
      return r.json();
    })
    .then(function(items){

      if(!Array.isArray(items)) return;

      var testsBox =
        document.getElementById('tests');

      var practiceBox =
        document.getElementById('practice');

      if(!testsBox && !practiceBox) return;

      function addCard(box,item){

        if(!box) return;

        var card =
          document.createElement('div');

        card.className = 'card';

        var title =
          document.createElement('b');

        title.textContent =
          (item.emoji || '📝')
          + ' '
          + item.topic
          + ' '
          + item.count
          + 'Q';

        var small =
          document.createElement('small');

        small.textContent =
          item.label
          + ' • '
          + item.count
          + ' Questions';

        var button =
          document.createElement('button');

        button.className = 'primary';

        button.textContent = 'Start Test';

        button.onclick = function(){

          location.href =
            item.url;

        };

        card.appendChild(title);
        card.appendChild(small);
        card.appendChild(button);

        box.appendChild(card);
      }

      items.forEach(function(item){

        if(item.box === 'test'){

          addCard(
            testsBox,
            item
          );

        }else{

          addCard(
            practiceBox,
            item
          );

        }

      });

    })
    .catch(function(){});

})();
</script>
"""

    source = source.replace(
        "</body>",
        script + "\n</body>"
    )

    with open(
        path,
        "w",
        encoding="utf-8"
    ) as f:
        f.write(source)


# --------------------------------------------------
# TELEGRAM OFFSET
# --------------------------------------------------

OFFSET_FILE = (
    "content/telegram_offset.txt"
)


def load_offset():

    if not os.path.exists(
        OFFSET_FILE
    ):
        return 0

    try:

        with open(
            OFFSET_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return int(
                f.read().strip()
            )

    except Exception:

        return 0


def save_offset(offset):

    os.makedirs(
        "content",
        exist_ok=True
    )

    with open(
        OFFSET_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        f.write(
            str(offset)
        )


# --------------------------------------------------
# PROCESS TELEGRAM MESSAGE
# --------------------------------------------------

def process_update(update):

    message = update.get(
        "message",
        {}
    )

    chat = message.get(
        "chat",
        {}
    )

    chat_id = chat.get(
        "id"
    )

    text = message.get(
        "text",
        ""
    )

    if not chat_id:
        return

    if text.strip().lower() == "/start":

        send_message(
            chat_id,
            "🤖 Gyankshetra AI Bot तैयार है!\n\n"
            "/test इतिहास 20\n"
            "/quiz इतिहास 10\n"
            "/practice विज्ञान 20\n"
            "/mock रसायन 30\n"
            "/pyq विज्ञान 20"
        )

        return

    command = parse_command(
        text
    )

    if not command:
        return

    cmd = command["command"]
    topic = command["topic"]
    count = command["count"]

    info = command_info(
        cmd
    )

    send_message(
        chat_id,
        "🤖 Groq AI प्रश्न तैयार कर रहा है...\n\n"
        + info["emoji"]
        + " विषय: "
        + topic
        + "\n"
        + "📌 प्रकार: "
        + info["label"]
        + "\n"
        + "📝 प्रश्न: "
        + str(count)
        + "\n\n"
        + "थोड़ा समय लगेगा..."
    )

    try:

        questions = generate_all(
            topic,
            count,
            info["label"]
        )

        if len(questions) < count:

            raise RuntimeError(
                "मांगे गए "
                + str(count)
                + " प्रश्नों में केवल "
                + str(len(questions))
                + " बने।"
            )

        timestamp = datetime.utcnow().strftime(
            "%Y%m%d-%H%M%S"
        )

        slug = safe_slug(
            topic
        )

        filename = (
            slug
            + "-"
            + str(count)
            + "q-"
            + timestamp
            + ".html"
        )

        folder = info["folder"]

        os.makedirs(
            folder,
            exist_ok=True
        )

        page = make_master_test(
            topic,
            count,
            questions
        )

        output_path = os.path.join(
            folder,
            filename
        )

        with open(
            output_path,
            "w",
            encoding="utf-8"
        ) as f:

            f.write(page)

        relative_url = (
            folder
            + "/"
            + filename
        )

        update_registry(
            topic,
            count,
            cmd,
            relative_url,
            timestamp
        )

        update_master_index()

        live_url = (
            BASE_URL
            + folder
            + "/"
            + quote(filename)
        )

        send_message(
            chat_id,
            "✅ Test तैयार है!\n\n"
            + info["emoji"]
            + " विषय: "
            + topic
            + "\n"
            + "📌 प्रकार: "
            + info["label"]
            + "\n"
            + "📝 प्रश्न: "
            + str(count)
            + "\n"
            + "🎯 Generated: "
            + str(len(questions))
            + "\n\n"
            + "🔗 LIVE TEST:\n"
            + live_url
        )

    except Exception as e:

        error = str(e)

        if len(error) > 2500:
            error = error[:2500]

        send_message(
            chat_id,
            "❌ Test generate नहीं हो पाया\n\n"
            "Error:\n"
            + error
        )


# --------------------------------------------------
# MAIN POLLING
# --------------------------------------------------

def main():

    print(
        "Gyankshetra Telegram Bot started"
    )

    offset = load_offset()

    start_time = time.time()

    while time.time() - start_time < 210:

        try:

            result = telegram(
                "getUpdates",
                {
                    "timeout": 20,
                    "offset": offset
                }
            )

            updates = result.get(
                "result",
                []
            )

            for update in updates:

                offset = (
                    update["update_id"]
                    + 1
                )

                save_offset(
                    offset
                )

                try:

                    process_update(
                        update
                    )

                except Exception as e:

                    print(
                        "PROCESS ERROR:",
                        str(e)
                    )

        except Exception as e:

            print(
                "POLL ERROR:",
                str(e)
            )

            time.sleep(4)

    save_offset(
        offset
    )

    print(
        "Gyankshetra Bot finished"
    )


if __name__ == "__main__":
    main()
