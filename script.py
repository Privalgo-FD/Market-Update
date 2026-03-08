import os
import smtplib
import ssl
import json
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import feedparser
import requests
from openai import OpenAI

RECIPIENTS = [
    "rwallroth@privalgo.eu",
    "mheijnen@privalgo.eu",
    "Bhessels@privalgo.eu",
    "ton@stadsmeester.nl",
    "plucassen@privalgo.eu",
    "whendriks@privalgo.co.uk",
]

RSS_FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/news/wealth",
    "https://www.ecb.europa.eu/press/rss/press_release.en.rss",
    "https://www.federalreserve.gov/feeds/press_monetary.xml",
]

FX_API_URL = "https://api.exchangerate.host/latest?base=EUR&symbols=USD,GBP,CAD"

SMTP_SERVER = os.environ["SMTP_SERVER"]
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ["SMTP_USERNAME"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
MAIL_FROM = os.environ["MAIL_FROM"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

client = OpenAI(api_key=OPENAI_API_KEY)


def fetch_rss_items(max_items_per_feed=4):
    items = []
    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:max_items_per_feed]:
            items.append({
                "title": entry.get("title", ""),
                "summary": entry.get("summary", ""),
                "link": entry.get("link", "")
            })
    return items[:12]


def fetch_fx_rates():
    response = requests.get(FX_API_URL, timeout=20)
    response.raise_for_status()
    data = response.json()
    rates = data.get("rates", {})

    return {
        "EURUSD": round(rates.get("USD", 0), 4),
        "EURGBP": round(rates.get("GBP", 0), 4),
        "EURCAD": round(rates.get("CAD", 0), 4),
    }

def build_market_prompt(headlines, fx_rates):
    headlines_text = "\n".join(
        [f"- {item['title']} | {item['summary']}" for item in headlines]
    )

    return f"""
You are a financial markets analyst preparing an internal daily market update for Privalgo.

Use ONLY the information below.
Write concise, factual, professional output.
Avoid hype, avoid repetition, avoid generic filler.

INPUT HEADLINES:
{headlines_text}

FX SNAPSHOT:
EUR/USD: {fx_rates['EURUSD']}
EUR/GBP: {fx_rates['EURGBP']}
EUR/CAD: {fx_rates['EURCAD']}

Return valid JSON with exactly this structure:
{{
  "en": {{
    "key_market": "...",
    "fx": "...",
    "central_banks": "...",
    "data_watch": "...",
    "impact": "..."
  }},
  "nl": {{
    "key_market": "...",
    "fx": "...",
    "central_banks": "...",
    "data_watch": "...",
    "impact": "..."
  }}
}}

Rules:
- English and Dutch must both read naturally.
- Each field max 2 sentences.
- Keep it useful for colleagues speaking to internationally active clients.
- If central bank news is thin, say so briefly instead of inventing detail.
- If economic data is thin, mention what markets are likely watching.
"""


def generate_market_update(headlines, fx_rates):
    prompt = build_market_prompt(headlines, fx_rates)

    response = client.chat.completions.create(
    model="gpt-5",
    messages=[
        {
            "role": "system",
            "content": "You produce precise internal market briefings in strict JSON."
        },
        {
            "role": "user",
            "content": prompt
        }
    ]
)

    content = response.choices[0].message.content
    return json.loads(content)


def load_template(path="email_template.html"):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def fill_template(template, market_update, fx_rates):
    today = datetime.now().strftime("%d %B %Y")

    replacements = {
        "{{DATE}}": today,
        "{{EURUSD}}": fx_rates["EURUSD"],
        "{{EURGBP}}": fx_rates["EURGBP"],
        "{{EURCAD}}": fx_rates["EURCAD"],

        "{{EN_KEY_MARKET}}": market_update["en"]["key_market"],
        "{{EN_FX}}": market_update["en"]["fx"],
        "{{EN_CB}}": market_update["en"]["central_banks"],
        "{{EN_DATA}}": market_update["en"]["data_watch"],
        "{{EN_IMPACT}}": market_update["en"]["impact"],

        "{{NL_KEY_MARKET}}": market_update["nl"]["key_market"],
        "{{NL_FX}}": market_update["nl"]["fx"],
        "{{NL_CB}}": market_update["nl"]["central_banks"],
        "{{NL_DATA}}": market_update["nl"]["data_watch"],
        "{{NL_IMPACT}}": market_update["nl"]["impact"],
    }

    html = template
    for key, value in replacements.items():
        html = html.replace(key, value)
    return html


def send_email(html_body):
    subject = f"Privalgo Daily Market Update – {datetime.now().strftime('%d %B %Y')}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(RECIPIENTS)
    msg["Reply-To"] = "whendriks@privalgo.co.uk"

    text_body = "This email contains an HTML market update. Please view it in an HTML-compatible email client."
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(MAIL_FROM, RECIPIENTS, msg.as_string())


def main():
    headlines = fetch_rss_items()
    fx_rates = fetch_fx_rates()
    market_update = generate_market_update(headlines, fx_rates)
    template = load_template()
    html_body = fill_template(template, market_update, fx_rates)
    send_email(html_body)
    print("Market update email sent successfully.")


if __name__ == "__main__":
    main()
