import sys
import subprocess

# 1. GitHub Actions me zaroori packages install karne ke liye
try:
    import google.generativeai as genai
    import requests
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "google-generativeai", "requests"])
    import google.generativeai as genai
    import requests

import os
import json
import re

# 2. Environment Variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# 3. Telegram Send Message Function
def send_telegram_message(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN missing")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Error sending message: {e}")

# 4. Generate Quiz Function using Gemini
def generate_quiz(subject, num_questions):
    prompt = f"""
    Create a multiple-choice quiz on '{subject}' with {num_questions} questions in Hindi.
    Return ONLY a raw JSON array of objects. Do not include markdown tags like ```json, extra text, or explanations outside JSON.
    Each object must have:
    "id" (number),
    "question" (string),
    "options" (array of 5 strings),
    "correct" (index 0-4),
    "explanation" (string)
    """

    # Gemini model fallback structure
    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content(prompt)
    except Exception as e:
        print(f"Fallback to gemini-1.5-flash due to: {e}")
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)

    raw_text = response.text.strip()
    raw_text = re.sub(r'^```json\s*', '', raw_text)
    raw_text = re.sub(r'\s*```$', '', raw_text)
    
    return json.loads(raw_text)

# 5. Main Processing Function
def main():
    print("Gyankshetra Bot Script Executed Successfully!")

if __name__ == "__main__":
    main()
