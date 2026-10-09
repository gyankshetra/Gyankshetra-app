#!/usr/bin/env python3
"""Gyankshetra Study Bot
Telegram में लिखें: "BSSC previous year paper"
-> बॉट वेब पर खोजता है -> PDF GitHub repo में स्थायी रूप से सेव करता है
-> study.json में "Bihar SSC Previous Year Paper" नाम से जोड़ता है
-> ऐप के Study Material > Previous Papers में हमेशा दिखता है (Download / Print)

ज़रूरी environment variables:
  TELEGRAM_BOT_TOKEN   BotFather से
  ADMIN_IDS            आपकी Telegram user id (कई हों तो कॉमा से अलग)
  GROQ_API_KEY         Groq की key
  TAVILY_API_KEY       (वैकल्पिक) बेहतर वेब सर्च के लिए; न हो तो बिना key वाली DuckDuckGo खोज चलती है
  GITHUB_TOKEN         repo में contents:write वाला token
  GITHUB_REPO          जैसे  gyankshetra/Gyankshetra-app
  GITHUB_BRANCH        (वैकल्पिक) default: main
"""
import base64, json, os, re, time, urllib.error, urllib.parse, urllib.request

E = os.environ
TG = "https://api.telegram.org/bot" + E.get("TELEGRAM_BOT_TOKEN", "")
GROQ_MODEL = "llama-3.3-70b-versatile"
BRANCH = E.get("GITHUB_BRANCH", "main")
MAX_PDF = 25 * 1024 * 1024
SECTIONS = ("notes", "ncert", "papers")
UA = {"User-Agent": "Mozilla/5.0 (GyankshetraBot)"}


def http(url, data=None, headers=None, method=None, timeout=90):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def jpost(url, payload, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    return json.loads(http(url, json.dumps(payload).encode(), h))


# ---------- AI ----------
def groq(system, user):
    r = jpost("https://api.groq.com/openai/v1/chat/completions",
              {"model": GROQ_MODEL, "temperature": 0.2,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
              {"Authorization": "Bearer " + E["GROQ_API_KEY"]})
    return r["choices"][0]["message"]["content"]


def understand(text):
    """संदेश से तय करता है: कौन-सा सेक्शन, सेव होने वाला नाम, खोज query।"""
    out = groq('केवल JSON दें: {"section":"papers|notes|ncert","title":"...","query":"..."}. '
               'title साफ़ अंग्रेज़ी/हिन्दी नाम हो, जैसे "Bihar SSC Previous Year Paper". '
               'पूरे नाम खोलें (BSSC = Bihar SSC). query में "filetype:pdf" और "official" जोड़ें।', text)
    d = json.loads(re.search(r"\{[\s\S]*\}", out).group(0))
    if d.get("section") not in SECTIONS:
        d["section"] = "papers"
    return d


# ---------- खोज ----------
def search_tavily(query):
    r = jpost("https://api.tavily.com/search", {"query": query, "max_results": 10},
              {"Authorization": "Bearer " + E["TAVILY_API_KEY"]})
    return [(x["url"], x.get("title", ""), x.get("content", "")) for x in r.get("results", [])]


def parse_ddg(page):
    """DuckDuckGo के HTML नतीजों से असली लिंक निकालता है।"""
    out = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
        href = m.group(1)
        if "uddg=" in href:
            href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
        elif href.startswith("//"):
            href = "https:" + href
        out.append((href, re.sub(r"<[^>]+>", "", m.group(2)), ""))
    return out


def search_ddg(query):
    """बिना किसी key के खोज (DuckDuckGo)। क्लाउड सर्वर पर कभी-कभी रोक सकता है।"""
    page = http("https://html.duckduckgo.com/html/", data=urllib.parse.urlencode({"q": query}).encode(),
                headers=dict(UA, **{"Content-Type": "application/x-www-form-urlencoded"})).decode("utf8", "ignore")
    return parse_ddg(page)


def search(query):
    if E.get("TAVILY_API_KEY"):
        return search_tavily(query)
    return search_ddg(query)


def rank(results):
    """आधिकारिक (.gov.in / .nic.in) और सीधे PDF लिंक को आगे रखता है।"""
    def score(item):
        u = item[0].lower()
        host = urllib.parse.urlparse(u).netloc
        return (3 if host.endswith((".gov.in", ".nic.in")) else 0) + (2 if u.split("?")[0].endswith(".pdf") else 0)
    return sorted(results, key=score, reverse=True)


def fetch_pdf(url):
    data = http(url, headers=UA, timeout=120)
    if len(data) > MAX_PDF or not data.startswith(b"%PDF"):
        return None
    return data


# ---------- GitHub ----------
def gh(path, method="GET", payload=None):
    url = f"https://api.github.com/repos/{E['GITHUB_REPO']}/contents/{urllib.parse.quote(path)}"
    h = {"Authorization": "Bearer " + E["GITHUB_TOKEN"], "Accept": "application/vnd.github+json", "User-Agent": "gyankshetra-bot"}
    if method == "GET":
        url += "?ref=" + BRANCH
        return json.loads(http(url, headers=h))
    h["Content-Type"] = "application/json"
    return json.loads(http(url, json.dumps(payload).encode(), h, method="PUT"))


def gh_put(path, raw, message):
    try:
        sha = gh(path)["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        sha = None
    body = {"message": message, "content": base64.b64encode(raw).decode(), "branch": BRANCH}
    if sha:
        body["sha"] = sha
    gh(path, "PUT", body)


def slugify(title):
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "item-%d" % int(time.time())


def add_entry(section, entry):
    """study.json में आइटम जोड़ता है; इसी नाम का आइटम हो तो उसे बदल देता है।"""
    try:
        data = json.loads(base64.b64decode(gh("study.json")["content"]))
    except urllib.error.HTTPError:
        data = {}
    for k in SECTIONS:
        data.setdefault(k, [])
    data[section] = [x for x in data[section] if x.get("id") != entry["id"]] + [entry]
    gh_put("study.json", json.dumps(data, ensure_ascii=False, indent=1).encode(), "Study: " + entry["title"])


# ---------- मुख्य काम ----------
def handle(text):
    d = understand(text)
    title, section = d["title"], d["section"]
    sid = slugify(title)

    if section == "papers":
        cands = rank(search(d["query"]))
        for url, _t, _c in cands:
            if not url.lower().split("?")[0].endswith(".pdf"):
                continue
            try:
                pdf = fetch_pdf(url)
            except Exception:
                continue
            if pdf:
                gh_put(f"papers/{sid}.pdf", pdf, "Paper: " + title)
                add_entry("papers", {"id": sid, "title": title, "url": f"papers/{sid}.pdf", "source": url})
                return f"✅ सेव हो गया: {title}\nस्रोत: {url}\nऐप में: Study Material → Previous Papers"
        links = "\n".join(u for u, _t, _c in cands[:3])
        return "⚠️ कोई सीधा PDF नहीं मिला, इसलिए कुछ सेव नहीं किया।\nये पेज खुद देखें:\n" + links

    html = groq("आप STET/BPSC TRE के अनुभवी हिन्दी शिक्षक हैं। केवल सही तथ्य लिखें। केवल HTML fragment दें "
                "(<h3>,<p>,<ul>,<li>,<table>,<b>), markdown या <script> नहीं।",
                f"टॉपिक: {title}\nहिन्दी में परीक्षा-उपयोगी सामग्री लिखें, अंत में 'याद रखने योग्य बातें' दें।")
    html = re.sub(r"```(?:html)?", "", html)
    html = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.I).strip()
    add_entry(section, {"id": sid, "title": title, "html": html})
    return f"✅ सेव हो गया: {title}\nऐप में: Study Material"


# ---------- Telegram ----------
def say(chat, text):
    jpost(TG + "/sendMessage", {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True})


def main():
    admins = {int(x) for x in E["ADMIN_IDS"].split(",") if x.strip()}
    offset = 0
    print("Study bot चालू है…")
    while True:
        try:
            ups = json.loads(http(f"{TG}/getUpdates?timeout=50&offset={offset}", timeout=70))["result"]
        except Exception:
            time.sleep(5)
            continue
        for u in ups:
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            text, uid = m.get("text"), (m.get("from") or {}).get("id")
            if not text or uid not in admins:      # सिर्फ़ admin repo में कुछ लिख सकता है
                continue
            say(m["chat"]["id"], "⏳ खोज रहा हूँ…")
            try:
                say(m["chat"]["id"], handle(text))
            except Exception as e:
                say(m["chat"]["id"], f"❌ नहीं हो पाया: {e}")


if __name__ == "__main__":
    main()
