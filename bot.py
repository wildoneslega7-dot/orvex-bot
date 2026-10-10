import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# ---- Настройки (берутся из переменных окружения, в коде ключей нет) ----
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("MODEL", "")  # например: qwen/qwen3-coder:free
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
VK_TOKEN = os.environ.get("VK_TOKEN", "")
VK_GROUP_ID = os.environ.get("VK_GROUP_ID", "")

SYSTEM_PROMPT = (
    "Ты Orvex, дружелюбный ИИ-помощник. Отвечай на языке пользователя. "
    "Хорошо пишешь код: веб, боты, мобильные приложения. "
    "Пиши коротко и по делу, код давай целыми блоками."
)

history = {}  # память диалогов: ключ чата -> список сообщений
MAX_HISTORY = 10


def ask_ai(chat_key, text):
    msgs = history.setdefault(chat_key, [])
    msgs.append({"role": "user", "content": text})
    del msgs[:-MAX_HISTORY]
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
            json={
                "model": MODEL,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + msgs,
            },
            timeout=120,
        )
        data = r.json()
        answer = data["choices"][0]["message"]["content"]
    except Exception as e:
        print("AI error:", e)
        return "Сейчас не получилось ответить, попробуйте ещё раз чуть позже."
    msgs.append({"role": "assistant", "content": answer})
    return answer


def chunks(text, size=3900):
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


# ---------------- Telegram ----------------
def telegram_loop():
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    offset = 0
    print("Telegram запущен")
    while True:
        try:
            r = requests.get(
                f"{base}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=40,
            ).json()
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if not msg or "text" not in msg:
                    continue
                chat_id = msg["chat"]["id"]
                if msg["text"].startswith("/start"):
                    reply = "Привет! Я Orvex. Опишите задачу, и я помогу с кодом."
                else:
                    reply = ask_ai(f"tg{chat_id}", msg["text"])
                for part in chunks(reply):
                    requests.post(
                        f"{base}/sendMessage",
                        json={"chat_id": chat_id, "text": part},
                        timeout=30,
                    )
        except Exception as e:
            print("Telegram error:", e)
            time.sleep(5)


# ---------------- VK ----------------
VK_V = "5.131"


def vk_call(method, **params):
    params.update(access_token=VK_TOKEN, v=VK_V)
    return requests.post(
        f"https://api.vk.com/method/{method}", data=params, timeout=30
    ).json()


def vk_loop():
    print("VK запущен")
    while True:
        try:
            lp = vk_call("groups.getLongPollServer", group_id=VK_GROUP_ID)["response"]
            server, key, ts = lp["server"], lp["key"], lp["ts"]
            while True:
                r = requests.get(
                    server,
                    params={"act": "a_check", "key": key, "ts": ts, "wait": 25},
                    timeout=40,
                ).json()
                if "failed" in r:
                    if r["failed"] == 1:
                        ts = r["ts"]
                        continue
                    break  # получить сервер заново
                ts = r["ts"]
                for ev in r.get("updates", []):
                    if ev.get("type") != "message_new":
                        continue
                    m = ev["object"]["message"]
                    text = m.get("text", "")
                    if not text:
                        continue
                    peer = m["peer_id"]
                    reply = ask_ai(f"vk{peer}", text)
                    for part in chunks(reply, 3900):
                        vk_call(
                            "messages.send",
                            peer_id=peer,
                            message=part,
                            random_id=random.randint(1, 2**31),
                        )
        except Exception as e:
            print("VK error:", e)
            time.sleep(5)


# ---------------- Мини-сервер (нужен хостингу) ----------------
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Orvex is running")

    def log_message(self, *args):
        pass


def main():
    if not OPENROUTER_KEY or not MODEL:
        raise SystemExit("Нужны OPENROUTER_API_KEY и MODEL")
    if TG_TOKEN:
        threading.Thread(target=telegram_loop, daemon=True).start()
    if VK_TOKEN and VK_GROUP_ID:
        threading.Thread(target=vk_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


if __name__ == "__main__":
    main()
