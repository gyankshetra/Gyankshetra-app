import os
import json
import re
import requests
import google.generativeai as genai

# 1. Environment Variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)

# 2. Telegram Send Message Function
def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)

# 3. Generate Quiz Function using Gemini
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

    # Try gemini-2.5-flash first, fallback to gemini-1.5-flash if needed
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

# 4. Main Processing Function
def main():
    # Telegram Update / Command Parsing
    # Telegram updates ke mutabiq command handle karein
    print("Gyankshetra Bot Script Running...")

if __name__ == "__main__":
    main()
