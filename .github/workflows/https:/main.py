#!/usr/bin/env python3
"""Автопостер для Telegram-канала «Новости мира и технологии».

RSS -> Gemini (запасной Groq) -> Telegram.
Один запуск = один пост.

Переменные окружения:
  TELEGRAM_TOKEN, CHANNEL_ID       обязательные
  GEMINI_API_KEY, GROQ_API_KEY     нужен хотя бы один
  GEMINI_MODELS                    через запятую
  GROQ_MODELS                      через запятую
  GEMINI_THINKING                  low / medium / high
  FOOTER                            HTML-подпись под каждым постом
  NO_PREVIEW=1                      отключить карточку-превью
  DRY_RUN=1                         показать пост без отправки
"""
import html
import os
import random
import re
import sys
import tempfile
import time
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import feedparser
import requests

# ---------------------------------------------------------------- настройки


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


TELEGRAM_TOKEN = env("TELEGRAM_TOKEN")
CHANNEL_ID = env("CHANNEL_ID")
GEMINI_API_KEY = env("GEMINI_API_KEY")
GROQ_API_KEY = env("GROQ_API_KEY")

GEMINI_MODELS = [
    m.strip()
    for m in env(
        "GEMINI_MODELS",
        "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash-lite",
    ).split(",")
    if m.strip()
]
GROQ_MODELS = [
    m.strip()
    for m in env(
        "GROQ_MODELS",
        "llama-3.3-70b-versatile,llama-3.1-8b-instant",
    ).split(",")
    if m.strip()
]

GEMINI_THINKING = env("GEMINI_THINKING").lower()
FOOTER = env("FOOTER")
NO_PREVIEW = env("NO_PREVIEW") == "1"
DRY_RUN = env("DRY_RUN") == "1"

HISTORY_FILE = "history.txt"
HISTORY_LIMIT = 1500
MAX_PER_FEED = 15
MAX_BODY_CHARS = 3300

# (RSS, язык, категория)
FEEDS = [
    # 🌍 Мир
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "en", "🌍 МИР"),

    # 💻 Технологии
    ("https://techcrunch.com/feed/", "en", "💻 ТЕХНОЛОГИИ"),
    ("https://www.theverge.com/rss/index.xml", "en", "💻 ТЕХНОЛОГИИ"),
    ("https://feeds.arstechnica.com/arstechnica/index", "en", "💻 ТЕХНОЛОГИИ"),

    # 🤖 AI
    (
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        "en",
        "🤖 AI",
    ),

    # 🚀 Наука и космос
    ("https://www.nasa.gov/rss/dyn/breaking_news.rss", "en", "🚀 НАУКА"),

    # 🇷🇺 Русскоязычные технологии
    ("https://habr.com/ru/rss/all/all/", "ru", "💻 ТЕХНОЛОГИИ"),
]

STYLES = [
    "Начни с самого важного факта. Пиши быстро, понятно и без журналистской воды.",
    "Начни с неожиданной детали новости, затем объясни, что произошло и почему это важно.",
    "Подавай новость через практический смысл: что произошло, кого это затронет и что изменится.",
    "Начни с короткой эмоциональной реакции, затем спокойно разложи событие по фактам.",
    "Сделай главным одно самое интересное изменение. Не пересказывай всю статью.",
    "Объясни сложную тему простым языком, чтобы её понял человек, который не следит за ней каждый день.",
    "Покажи контекст: что произошло сейчас и почему событие заслуживает внимания.",
    "Напиши короткую новость в стиле современного Telegram-канала: сильное начало, конкретные факты и короткий вывод.",
    "Для технологий объясни, что изменилось для обычного пользователя.",
    "Для мировой новости дай факты и контекст без политической агитации и без выбора стороны.",
]

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "Mozilla/5.0 (compatible; WorldTechNewsBot/4.0)"

gemini_client = None
gemini_config = None

if GEMINI_API_KEY:
    try:
        from google import genai
        from google.genai import types as genai_types

        gemini_client = genai.Client(api_key=GEMINI_API_KEY)

        if GEMINI_THINKING in ("low", "medium", "high"):
            gemini_config = genai_types.GenerateContentConfig(
                thinking_config=genai_types.ThinkingConfig(
                    thinking_level=GEMINI_THINKING
                )
            )
    except Exception as exc:
        print(f"Gemini отключён или настроен не полностью: {type(exc).__name__}")


def safe(exc):
    """Текст ошибки без токенов."""
    text = str(exc)
    for secret in (TELEGRAM_TOKEN, GEMINI_API_KEY, GROQ_API_KEY):
        if secret:
            text = text.replace(secret, "***")
    return text


# ---------------------------------------------------------------- история


def normalize(link):
    """Убирает utm-метки, якоря и хвостовой слэш."""
    try:
        p = urlparse(link.strip())
        q = [
            (k, v)
            for k, v in parse_qsl(p.query)
            if not k.lower().startswith("utm_")
        ]
        path = p.path.rstrip("/") or "/"
        return urlunparse(
            (p.scheme, p.netloc.lower(), path, "", urlencode(q), "")
        )
    except Exception:
        return link.strip()


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return [normalize(line) for line in f.read().splitlines() if line.strip()]


def save_history(link):
    lines = load_history() + [normalize(link)]
    lines = lines[-HISTORY_LIMIT:]

    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(os.path.abspath(HISTORY_FILE))
    )

    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    os.replace(tmp, HISTORY_FILE)


# ---------------------------------------------------------------- статьи


def clean_text(raw, limit=1800):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def detect_category(title, text, default_category):
    """Определяет категорию по ключевым словам."""
    value = f"{title} {text}".lower()

    ai_words = (
        "artificial intelligence",
        " ai ",
        "openai",
        "chatgpt",
        "gemini",
        "claude",
        "anthropic",
        "neural network",
        "нейросет",
        "искусственн",
        "машинное обучение",
    )

    science_words = (
        "nasa",
        "space",
        "spacex",
        "rocket",
        "moon",
        "mars",
        "satellite",
        "космос",
        "ракета",
        "спутник",
        "луна",
        "марс",
        "наука",
    )

    security_words = (
        "hack",
        "hacker",
        "cyber",
        "security",
        "malware",
        "ransomware",
        "vulnerability",
        "взлом",
        "хакер",
        "кибер",
        "уязвим",
        "вредонос",
    )

    if any(word in value for word in ai_words):
        return "🤖 AI"

    if any(word in value for word in science_words):
        return "🚀 НАУКА"

    if any(word in value for word in security_words):
        return "🔐 БЕЗОПАСНОСТЬ"

    return default_category


def collect_candidates(history):
    seen = set(history)
    result = {}

    for url, lang, feed_category in FEEDS:
        try:
            r = SESSION.get(url, timeout=20)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as exc:
            print(f"Лента недоступна {url}: {safe(exc)}")
            continue

        count = 0

        for e in feed.entries[:MAX_PER_FEED]:
            link = getattr(e, "link", "")
            title = clean_text(getattr(e, "title", ""), 300)
            key = normalize(link) if link else ""

            if not key or not title or key in seen or key in result:
                continue

            body = e.content[0].value if e.get("content") else e.get("summary", "")
            body = clean_text(body)

            category = detect_category(title, body, feed_category)

            result[key] = {
                "title": title,
                "link": link,
                "text": body,
                "lang": lang,
                "category": category,
                "domain": re.sub(
                    r"^(www|feed|feeds)\.",
                    "",
                    urlparse(link).netloc,
                ),
            }

            count += 1

        print(f"{url}: новых {count}")

    return list(result.values())


# ---------------------------------------------------------------- генерация


def build_prompt(article):
    if article["lang"] == "en":
        lang_note = (
            "Источник на английском. Переведи смысл на естественный русский "
            "и перескажи своими словами."
        )
    else:
        lang_note = "Источник на русском. Перескажи его своими словами."

    return f"""Ты ведёшь современный Telegram-канал «Новости мира и технологии».

Категория этой новости: {article["category"]}

Основные темы канала:
🌍 мировые события
💻 технологии и IT
🤖 искусственный интеллект
📱 смартфоны и гаджеты
🚀 космос и наука
🔐 кибербезопасность
💼 технологические компании
🎮 игры и игровая индустрия

Материал:

Заголовок: {article["title"]}
Описание: {article["text"] or "нет"}
Источник: {article["domain"]}

Стиль подачи:
{random.choice(STYLES)}

ТРЕБОВАНИЯ:

1. Пиши только на русском языке.
2. Объём примерно 400-900 символов.
3. Начни с сильной первой строки, которая сразу сообщает главное.
4. Не начинай каждый пост одинаково. Не используй шаблонные вступления:
   «Стало известно», «Компания объявила», «Разработчики представили»,
   «В мире технологий», «Представьте».
5. Выбери самую интересную мысль и раскрой её, но обязательно сохрани
   2-3 конкретных факта из исходного материала.
6. Не выдумывай факты, цифры, даты, цитаты, причины или последствия.
7. Если информация предварительная или подтверждена не полностью,
   прямо укажи это.
8. Если новость политическая или связана с конфликтом, не занимай сторону.
   Дай только проверяемые сведения из материала.
9. Для технологических новостей объясни:
   что произошло → почему это важно → кого это касается.
10. Для мировой новости объясни:
    что произошло → где → кто связан с событием → последствия,
    только если они есть в источнике.
11. Используй 2-5 уместных эмодзи. Не перегружай ими текст.
12. Короткие абзацы по 1-3 предложения.
13. Можно использовать лёгкую иронию, но без мата, оскорблений и кринжа.
14. Финал должен быть естественным: факт, вывод, последствие,
    практический совет или иногда вопрос. Не заканчивай каждый пост вопросом.
15. Запрещены слова и обороты:
    «Вывод», «Итак», «Подробнее», «магия», «секрет кроется»,
    «погрузимся», «Представьте», «Казалось бы», «зачем изобретать велосипед».
16. Не используй Markdown, звёздочки и блоки ``` .
17. Разрешены только HTML-теги:
    <b>, <i>, <code>
18. Первую строку и 1-2 ключевые мысли можно выделить через <b>.
19. Команды, названия файлов и технические идентификаторы можно писать в <code>.
20. Ссылку на источник не добавляй — бот добавит её автоматически.
21. Не приписывай людям, компаниям или разработчикам мотивы,
    которых нет в исходном материале.
22. Не копируй предложения из исходной статьи дословно.
23. Не добавляй отдельные заголовки вроде «Новости», «Факты» или «Мнение».
24. Не используй Linux как обязательную тему. Упоминай Linux только тогда,
    когда он реально относится к исходной новости.

{lang_note}

Выдай только готовый текст поста, без пояснений."""


def looks_valid(text):
    if not text or len(text) < 200:
        return False

    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", text)
    cyr = re.findall(r"[А-Яа-яЁё]", text)

    return bool(letters) and len(cyr) / len(letters) > 0.6


FATAL_MARKERS = (
    "429",
    "RESOURCE_EXHAUSTED",
    "quota",
    "404",
    "NOT_FOUND",
    "403",
    "401",
    "PERMISSION",
    "API key",
    "API_KEY",
)


def generate_gemini(prompt):
    if not gemini_client:
        return None

    for model in GEMINI_MODELS:
        for attempt in (1, 2):
            try:
                print(f"Gemini {model}, попытка {attempt}")

                kwargs = {"config": gemini_config} if gemini_config else {}

                r = gemini_client.models.generate_content(
                    model=model,
                    contents=prompt,
                    **kwargs,
                )

                text = (r.text or "").strip()

                if looks_valid(text):
                    return text

                print("Ответ Gemini не подошёл")

            except Exception as exc:
                msg = safe(exc)
                print(f"Ошибка Gemini ({model}): {msg[:300]}")

                if any(m in msg for m in FATAL_MARKERS):
                    break

                time.sleep(10 * attempt)

    return None


def generate_groq(prompt):
    if not GROQ_API_KEY:
        return None

    for model in GROQ_MODELS:
        for attempt in (1, 2):
            try:
                print(f"Groq {model}, попытка {attempt}")

                r = SESSION.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "user", "content": prompt}
                        ],
                        "temperature": 0.9,
                        "max_tokens": 1500,
                    },
                    timeout=60,
                )

                if r.status_code == 200:
                    text = (
                        r.json()["choices"][0]["message"]["content"] or ""
                    ).strip()

                    if looks_valid(text):
                        return text

                    print("Ответ Groq не подошёл")
                    break

                print(
                    f"Ошибка Groq ({model}): "
                    f"{r.status_code} {r.text[:200]}"
                )

                if r.status_code in (429, 500, 502, 503) and attempt == 1:
                    time.sleep(15)
                    continue

                break

            except Exception as exc:
                print(f"Ошибка Groq ({model}): {safe(exc)[:200]}")
                time.sleep(5)

    return None


# ---------------------------------------------------------------- форматирование


TAG_SPLIT = re.compile(r"(</?(?:b|i|code)>)", re.I)


def sanitize(text):
    """Приводит ответ нейросети к HTML Telegram."""
    text = re.sub(r"```[a-zA-Z]*", "", text)
    text = re.sub(r"^\s*[\*\-]\s+", "• ", text, flags=re.M)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"<(/?)strong>", r"<\1b>", text, flags=re.I)
    text = re.sub(r"<(/?)em>", r"<\1i>", text, flags=re.I)

    out, stack = [], []

    for part in TAG_SPLIT.split(text):
        if not part:
            continue

        if TAG_SPLIT.fullmatch(part):
            tag = part.strip("</>").lower()

            if part.startswith("</"):
                if tag in stack:
                    while stack[-1] != tag:
                        out.append(f"</{stack.pop()}>")
                    out.append(f"</{stack.pop()}>")
            else:
                stack.append(tag)
                out.append(f"<{tag}>")
        else:
            out.append(
                html.escape(
                    html.unescape(part),
                    quote=False,
                )
            )

    while stack:
        out.append(f"</{stack.pop()}>")

    retuуrn re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def trim(text, limit=MAX_BODY_CHARS):
    if len(text) <= limit:
        return text

    cut = text[:limit]
    pos = max(cut.rfind("\n\n"), cut.rfind(". "))

    return (
        cut[:pos + 1]
        if pos > limit // 2
        else cut
    ).strip()


def to_plain(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def build_message(body, article):
    link = html.escape(normalize(article["link"]), quote=True)

    msg = (
        f'{sanitize(trim(body))}\n\n'
        f'🔗 <a href="{link}">Источник: '
        f'{html.escape(article["domain"])}</a>'
    )

    if FOOTER:
        msg += f"\n\n{FOOTER}"

    return msg


def raw_post(article):
    """Fallback без AI."""
    category = article.get("category", "📰 НОВОСТИ")

    text = (
        f"{category}\n\n"
        f"<b>{html.escape(article['title'])}</b>"
    )

    if article["text"]:
        text += f"\n\n{html.escape(article['text'][:700])}"

    return text


# ---------------------------------------------------------------- Telegram


def telegram_send(text, parse_mode=None):
    """Возвращает (успех, описание ошибки)."""
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text[:4096],
    }

    if parse_mode:
        payload["parse_mode"] = parse_mode

    if NO_PREVIEW:
        payload["link_preview_options"] = {"is_disabled": True}

    error = ""

    for _ in range(3):
        try:
            r = SESSION.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json=payload,
                timeout=30,
            )
        except requests.RequestException as exc:
            error = safe(exc)
            time.sleep(5)
            continue

        if r.ok:
            return True, ""

        try:
            data = r.json()
        except ValueError:
            data = {}

        error = f"{r.status_code} {data.get('description', r.text[:200])}"

        if r.status_code == 429:
            time.sleep(
                min(
                    data.get("parameters", {}).get("retry_after", 5),
                    60,
                )
            )
            continue

        if r.status_code >= 500:
            time.sleep(5)
            continue

        break

    return False, error


def publish(message):
    ok, err = telegram_send(message, "HTML")

    if ok:
        return True

    print(f"HTML не прошёл ({err}), пробую обычным текстом")

    ok, err = telegram_send(to_plain(message))

    if not ok:
        print(f"Telegram отказал: {err}")

    return ok


# ---------------------------------------------------------------- main


def main():
    missing = [
        n
        for n, v in (
            ("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
            ("CHANNEL_ID", CHANNEL_ID),
        )
        if not v
    ]

    if missing and not DRY_RUN:
        print("Не заданы переменные: " + ", ".join(missing))
        return 1

    if not gemini_client and not GROQ_API_KEY:
        print("Нужен хотя бы один ключ: GEMINI_API_KEY или GROQ_API_KEY")
        return 1

    candidates = collect_candidates(load_history())

    if not candidates:
        print("Новых статей нет.")
        return 0

    article = random.choice(candidates)

    print(
        f"Выбрана: {article['category']} | "
        f"{article['title']} ({article['domain']})"
    )

    prompt = build_prompt(article)

    body = generate_gemini(prompt) or generate_groq(prompt)

    if body:
        message = build_message(body, article)
    else:
        # Если AI недоступен, публикуем исходный материал.
        print("Нейросети не ответили, публикую fallback-пост.")
        message = build_message(raw_post(article), article)

    if DRY_RUN:
        print("\n----- DRY RUN -----\n" + message)
        return 0

    if publish(message):
        save_history(article["link"])
        print("Пост опубликован.")
        return 0

    print("Пост не отправлен, статья останется в очереди.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
