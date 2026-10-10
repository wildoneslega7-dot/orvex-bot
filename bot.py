import html
import json
import os
import random
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# ================= Настройки (переменные окружения) =================
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
VK_TOKEN = os.environ.get("VK_TOKEN", "")
VK_GROUP_ID = os.environ.get("VK_GROUP_ID", "")
ADMIN_ID = os.environ.get("ADMIN_ID", "")  # ваш Telegram ID (необязательно)

# Модели: MODEL, MODEL_2 ... MODEL_5 (до 5 штук)
MODELS = [
    os.environ.get(k, "").strip()
    for k in ["MODEL", "MODEL_2", "MODEL_3", "MODEL_4", "MODEL_5"]
    if os.environ.get(k, "").strip()
]
FREE_MODELS = int(os.environ.get("FREE_MODELS", "2"))  # первые N моделей: бесплатно
PREMIUM_MODELS = int(os.environ.get("PREMIUM_MODELS", "4"))  # первые N: Premium; остальные: только Pro
FREE_LIMIT = int(os.environ.get("FREE_LIMIT", "10"))  # запросов в день
PREMIUM_LIMIT = int(os.environ.get("PREMIUM_LIMIT", "100"))
PRO_LIMIT = int(os.environ.get("PRO_LIMIT", "300"))
DATA_FILE = os.environ.get("DATA_FILE", "users.json")

# ---- Тарифы: цены в Telegram Stars (меняйте здесь) ----
PLANS = {"premium": "💎 Premium", "pro": "👑 Pro"}
PRICES = {
    "premium": {1: 150, 3: 400, 6: 750, 12: 1400},
    "pro": {1: 400, 3: 1100, 6: 2000, 12: 3700},
}
DAYS = {1: 30, 3: 90, 6: 180, 12: 365}
MONTH_LABEL = {1: "1 месяц", 3: "3 месяца", 6: "6 месяцев", 12: "12 месяцев"}

# ---- Встроенные промокоды (создаются автоматически) ----
DEFAULT_PROMOS = {
    "ORVEXPRO": {"plan": "pro", "days": 180, "total": 10},  # Pro на 6 мес, 10 активаций
    "ORVEXAI": {"plan": "premium", "days": 90, "total": 150},  # Premium на 3 мес, 150 активаций
}

SYSTEM_PROMPT = (
    "Ты Orvex, дружелюбный ИИ-помощник. Отвечай на языке пользователя. "
    "Хорошо пишешь код: веб, боты, мобильные приложения. "
    "Пиши коротко и по делу, код давай целыми блоками."
)

history = {}
MAX_HISTORY = 10


def model_name(model_id):
    return model_id.split("/")[-1].replace(":free", "")


# ================= Хранилище пользователей =================
lock = threading.RLock()


def load_db():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    promos = data.setdefault("_promos", {})
    for code, d in DEFAULT_PROMOS.items():
        promos.setdefault(
            code, {"plan": d["plan"], "days": d["days"], "left": d["total"], "used_by": []}
        )
    return data


db = load_db()


def save_db():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False)
    except Exception as e:
        print("save_db error:", e)


def get_user(uid):
    u = db.setdefault(
        str(uid), {"model": 0, "premium_until": 0, "pro_until": 0, "day": "", "used": 0}
    )
    u.setdefault("pro_until", 0)
    u.setdefault("premium_until", 0)
    return u


def tier(u):
    """0 — бесплатно, 1 — Premium, 2 — Pro."""
    now = time.time()
    if u["pro_until"] > now:
        return 2
    if u["premium_until"] > now:
        return 1
    return 0


def tier_name(t):
    return ["Бесплатный", PLANS["premium"], PLANS["pro"]][t]


def limit_for(u):
    return [FREE_LIMIT, PREMIUM_LIMIT, PRO_LIMIT][tier(u)]


def left_today(u):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if u["day"] != day:
        u["day"], u["used"] = day, 0
    return max(0, limit_for(u) - u["used"])


def model_level(i):
    if i < FREE_MODELS:
        return 0
    if i < PREMIUM_MODELS:
        return 1
    return 2


def model_allowed(u, i):
    return tier(u) >= model_level(i)


def add_plan(uid, plan, days):
    with lock:
        u = get_user(uid)
        key = f"{plan}_until"
        u[key] = max(time.time(), u[key]) + days * 86400
        save_db()
        return u[key]


def fmt_date(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%Y")


waiting_promo = set()  # пользователи, от которых ждём промокод


def redeem_promo(uid, code):
    code = code.strip().upper()
    with lock:
        p = db.get("_promos", {}).get(code)
        if not p or p["left"] <= 0:
            return False, "Промокод не найден или закончился 😕"
        if uid in p["used_by"]:
            return False, "Вы уже использовали этот промокод"
        p["left"] -= 1
        p["used_by"].append(uid)
        save_db()
    plan = p.get("plan", "premium")
    until = add_plan(uid, plan, p["days"])
    return True, f"🎉 Промокод применён! {PLANS[plan]} до {fmt_date(until)}"


# ================= ИИ =================
def ask_ai(chat_key, text, model):
    msgs = history.setdefault(chat_key, [])
    msgs.append({"role": "user", "content": text})
    del msgs[:-MAX_HISTORY]
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
            json={
                "model": model,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + msgs,
            },
            timeout=300,
        )
        answer = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print("AI error:", e)
        if msgs:
            msgs.pop()
        return None
    msgs.append({"role": "assistant", "content": answer})
    return answer


ERR_TEXT = "Сейчас не получилось ответить, попробуйте ещё раз чуть позже."


def chunks(text, size=3900):
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def keep_typing(send_action, stop):
    while not stop.is_set():
        try:
            send_action()
        except Exception:
            pass
        stop.wait(4)


# ================= Оформление кода для Telegram =================
def md_to_tg_html(text):
    parts = re.split(r"(```[\s\S]*?```)", text)
    out = []
    for p in parts:
        if p.startswith("```") and p.endswith("```") and len(p) >= 6:
            inner = p[3:-3]
            lang = ""
            m = re.match(r"([A-Za-z0-9_+#.-]*)\n", inner)
            if m:
                lang = m.group(1)
                inner = inner[m.end():]
            code = html.escape(inner.strip("\n"), quote=False)
            cls = f' class="language-{lang}"' if lang else ""
            out.append(f"<pre><code{cls}>{code}</code></pre>")
        else:
            t = html.escape(p, quote=False)
            t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
            t = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", t, flags=re.M)
            t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
            out.append(t)
    return "".join(out)


def split_markdown(text, limit=3500):
    result, cur, cur_len = [], [], 0
    in_code, lang = False, ""
    lines = []
    for line in text.split("\n"):
        while len(line) > limit:
            lines.append(line[:limit])
            line = line[limit:]
        lines.append(line)
    for line in lines:
        closing = in_code and line.strip() == "```"
        if cur and cur_len + len(line) + 1 > limit and not closing:
            if in_code:
                cur.append("```")
            result.append("\n".join(cur))
            cur = ["```" + lang] if in_code else []
            cur_len = sum(len(x) + 1 for x in cur)
        cur.append(line)
        cur_len += len(line) + 1
        if line.strip().startswith("```"):
            if in_code:
                in_code = False
            else:
                in_code = True
                lang = line.strip()[3:].strip()
    if cur:
        result.append("\n".join(cur))
    return result or [""]


# ================= Telegram: вспомогательное =================
def tg_api(base, method, **payload):
    try:
        return requests.post(f"{base}/{method}", json=payload, timeout=30)
    except Exception as e:
        print(method, "error:", e)
        return None


def tg_send(base, chat_id, text, markup=None):
    parts = split_markdown(text)
    for i, part in enumerate(parts):
        extra = {"reply_markup": markup} if markup and i == len(parts) - 1 else {}
        r = tg_api(
            base, "sendMessage",
            chat_id=chat_id, text=md_to_tg_html(part), parse_mode="HTML", **extra
        )
        if r is None or not r.ok:  # не приняли форматирование, шлём простым текстом
            tg_api(base, "sendMessage", chat_id=chat_id, text=part, **extra)


# ================= Экраны и кнопки =================
def kb(rows):
    return {"inline_keyboard": rows}


BACK = [{"text": "⬅️ Меню", "callback_data": "menu"}]
BACK_SUB = [{"text": "⬅️ Назад", "callback_data": "sub"}]


def screen_menu():
    return "Выберите действие:", kb([
        [{"text": "🤖 Выбрать модель", "callback_data": "models"}],
        [{"text": "💎 Подписка", "callback_data": "sub"},
         {"text": "👤 Профиль", "callback_data": "profile"}],
        [{"text": "🗑 Новый диалог", "callback_data": "clear"},
         {"text": "ℹ️ Помощь", "callback_data": "help"}],
    ])


def screen_models(u):
    rows = []
    for i, m in enumerate(MODELS):
        lvl = model_level(i)
        lock_mark = "" if model_allowed(u, i) else ("🔒 " if lvl == 1 else "👑 ")
        label = ("✅ " if i == u["model"] else "") + lock_mark + model_name(m)
        rows.append([{"text": label, "callback_data": f"m:{i}"}])
    rows.append(BACK)
    return "🤖 Выберите модель.\n🔒 нужен Premium, 👑 нужен Pro.", kb(rows)


def plan_status(u):
    t = tier(u)
    if t == 2:
        return f"✅ {PLANS['pro']} активен до {fmt_date(u['pro_until'])}"
    if t == 1:
        return f"✅ {PLANS['premium']} активен до {fmt_date(u['premium_until'])}"
    return "Сейчас у вас бесплатный тариф"


def screen_sub(u):
    n = len(MODELS)
    text = (
        f"💎 Подписки Orvex\n\n{plan_status(u)}\n\n"
        f"Бесплатно: {FREE_LIMIT} запросов в день, моделей: {min(FREE_MODELS, n)}\n"
        f"{PLANS['premium']}: {PREMIUM_LIMIT} в день, моделей: {min(PREMIUM_MODELS, n)}\n"
        f"{PLANS['pro']}: {PRO_LIMIT} в день, все модели ({n})\n\n"
        "Выберите тариф:"
    )
    return text, kb([
        [{"text": f"{PLANS['premium']} — от {PRICES['premium'][1]} ⭐", "callback_data": "plan:premium"}],
        [{"text": f"{PLANS['pro']} — от {PRICES['pro'][1]} ⭐", "callback_data": "plan:pro"}],
        [{"text": "🎟 Ввести промокод", "callback_data": "promo"}],
        BACK,
    ])


def screen_plan(plan):
    n = len(MODELS)
    if plan == "premium":
        desc = f"{PREMIUM_LIMIT} запросов в день, моделей: {min(PREMIUM_MODELS, n)}"
    else:
        desc = f"{PRO_LIMIT} запросов в день, все модели ({n})"
    text = f"{PLANS[plan]}\n{desc}\n\nВыберите срок:"
    rows = []
    base_price = PRICES[plan][1]
    for months, price in PRICES[plan].items():
        label = f"{MONTH_LABEL[months]} — {price} ⭐"
        if months > 1:
            pct = round(100 * (1 - price / (months * base_price)))
            if pct > 0:
                label += f" (−{pct}%)"
        rows.append([{"text": label, "callback_data": f"buy:{plan}:{months}"}])
    rows.append(BACK_SUB)
    return text, kb(rows)


def screen_profile(u):
    cur = MODELS[u["model"]] if u["model"] < len(MODELS) else MODELS[0]
    text = (
        f"👤 Профиль\n\nТариф: {tier_name(tier(u))}\n"
        f"{plan_status(u) if tier(u) else ''}\n"
        f"Модель: {model_name(cur)}\n"
        f"Осталось запросов сегодня: {left_today(u)}"
    ).replace("\n\n\n", "\n\n")
    return text, kb([[{"text": "💎 Подписка", "callback_data": "sub"}], BACK])


def screen_help():
    text = (
        "ℹ️ Просто напишите задачу, например: «сделай HTML-страницу входа».\n\n"
        "Команды:\n/menu — меню\n/model — выбор модели\n/sub — подписки\n"
        "/profile — профиль и лимиты\n/promo — ввести промокод\n/new — новый диалог"
    )
    return text, kb([BACK])


# ================= Telegram: платежи (Telegram Stars) =================
def tg_invoice(base, chat_id, plan, months):
    tg_api(
        base, "sendInvoice",
        chat_id=chat_id,
        title=f"Orvex {PLANS[plan][2:]}",
        description=f"Подписка {PLANS[plan][2:]} на {MONTH_LABEL[months]}",
        payload=f"{plan}:{months}",
        provider_token="",
        currency="XTR",
        prices=[{"label": PLANS[plan][2:], "amount": PRICES[plan][months]}],
    )


def on_paid(base, msg):
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]
    sp = msg["successful_payment"]
    try:
        plan, months = sp.get("invoice_payload", "").split(":")
        months = int(months)
        assert plan in PLANS and months in DAYS
    except Exception:
        print("Неизвестный платёж:", sp)
        return
    until = add_plan(uid, plan, DAYS[months])
    with lock:
        get_user(uid).setdefault("payments", []).append(sp.get("telegram_payment_charge_id", ""))
        save_db()
    text, markup = screen_menu()
    tg_api(
        base, "sendMessage", chat_id=chat_id,
        text=f"🎉 Оплата прошла! {PLANS[plan]} активен до {fmt_date(until)}.\n\n{text}",
        reply_markup=markup,
    )


# ================= Telegram: нажатия кнопок =================
def tg_callback(base, cq):
    uid = cq["from"]["id"]
    msg = cq.get("message") or {}
    chat_id = msg.get("chat", {}).get("id", uid)
    message_id = msg.get("message_id")
    data = cq.get("data", "")
    notice, invoice = "", None
    with lock:
        u = get_user(uid)
        if data == "models":
            text, markup = screen_models(u)
        elif data.startswith("m:"):
            try:
                i = int(data[2:])
            except ValueError:
                i = -1
            if 0 <= i < len(MODELS):
                if not model_allowed(u, i):
                    need = PLANS["premium"] if model_level(i) == 1 else PLANS["pro"]
                    notice = f"Эта модель доступна с тарифом {need}"
                else:
                    u["model"] = i
                    save_db()
                    notice = "Модель: " + model_name(MODELS[i])
            text, markup = screen_models(u)
        elif data == "sub":
            text, markup = screen_sub(u)
        elif data.startswith("plan:") and data[5:] in PLANS:
            text, markup = screen_plan(data[5:])
        elif data.startswith("buy:"):
            try:
                _, plan, months = data.split(":")
                months = int(months)
                assert plan in PLANS and months in PRICES[plan]
                invoice = (plan, months)
            except Exception:
                pass
            text, markup = screen_sub(u)
        elif data == "promo":
            waiting_promo.add(uid)
            notice = "Отправьте промокод"
            text, markup = "🎟 Отправьте промокод одним сообщением.", kb([BACK_SUB])
        elif data == "profile":
            text, markup = screen_profile(u)
        elif data == "help":
            text, markup = screen_help()
        elif data == "clear":
            history.pop(f"tg{chat_id}", None)
            notice = "Диалог очищен"
            text, markup = screen_menu()
        else:
            text, markup = screen_menu()
    args = {"callback_query_id": cq["id"]}
    if notice:
        args["text"] = notice
    tg_api(base, "answerCallbackQuery", **args)
    if invoice:
        tg_invoice(base, chat_id, *invoice)
    elif message_id:
        tg_api(
            base, "editMessageText",
            chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup,
        )


# ================= Telegram: команды и сообщения =================
def is_admin(uid):
    return bool(ADMIN_ID) and str(uid) == ADMIN_ID


def tg_command(base, chat_id, uid, text):
    cmd = text.split()[0].split("@")[0].lower()
    if cmd == "/grant" and is_admin(uid):
        try:
            parts = text.split()
            plan = parts[3].lower() if len(parts) > 3 else "premium"
            assert plan in PLANS
            until = add_plan(int(parts[1]), plan, int(parts[2]))
            reply = f"Готово: {parts[1]} — {PLANS[plan]} до {fmt_date(until)}"
        except Exception:
            reply = "Формат: /grant ID дни [premium|pro]"
        tg_api(base, "sendMessage", chat_id=chat_id, text=reply)
        return
    if cmd == "/newpromo" and is_admin(uid):
        try:
            _, code, plan, months, uses = text.split()
            plan = plan.lower()
            assert plan in PLANS and int(months) in DAYS
            with lock:
                db["_promos"][code.upper()] = {
                    "plan": plan, "days": DAYS[int(months)], "left": int(uses), "used_by": []
                }
                save_db()
            reply = f"Промокод {code.upper()}: {PLANS[plan]}, {MONTH_LABEL[int(months)]}, активаций: {uses}"
        except Exception:
            reply = "Формат: /newpromo КОД premium|pro месяцы(1,3,6,12) активации"
        tg_api(base, "sendMessage", chat_id=chat_id, text=reply)
        return
    if cmd == "/promo":
        parts = text.split()
        if len(parts) < 2:
            waiting_promo.add(uid)
            reply = "🎟 Отправьте промокод одним сообщением."
        else:
            _, reply = redeem_promo(uid, parts[1])
        tg_api(base, "sendMessage", chat_id=chat_id, text=reply)
        return
    if cmd == "/new":
        history.pop(f"tg{chat_id}", None)
        tg_api(base, "sendMessage", chat_id=chat_id, text="Новый диалог начат ✨")
        return
    with lock:
        u = get_user(uid)
        if cmd == "/start":
            _, markup = screen_menu()
            text_out = "Привет! Я Orvex, ИИ-помощник для кода.\nОпишите задачу или откройте меню."
        elif cmd == "/model":
            text_out, markup = screen_models(u)
        elif cmd == "/sub":
            text_out, markup = screen_sub(u)
        elif cmd == "/profile":
            text_out, markup = screen_profile(u)
        elif cmd == "/help":
            text_out, markup = screen_help()
        else:
            text_out, markup = screen_menu()
    tg_api(base, "sendMessage", chat_id=chat_id, text=text_out, reply_markup=markup)


def tg_handle(base, chat_id, uid, text):
    with lock:
        u = get_user(uid)
        if left_today(u) <= 0:
            limit_hit = True
        else:
            limit_hit = False
            idx = u["model"]
            if idx >= len(MODELS) or not model_allowed(u, idx):
                idx = 0
            u["used"] += 1
            save_db()
    if limit_hit:
        tg_api(
            base, "sendMessage", chat_id=chat_id,
            text="На сегодня запросы закончились 😔\nОформите подписку или приходите завтра.",
            reply_markup=kb([[{"text": "💎 Подписка", "callback_data": "sub"}]]),
        )
        return
    stop = threading.Event()
    threading.Thread(
        target=keep_typing,
        args=(
            lambda: tg_api(base, "sendChatAction", chat_id=chat_id, action="typing"),
            stop,
        ),
        daemon=True,
    ).start()
    try:
        reply = ask_ai(f"tg{chat_id}", text, MODELS[idx])
    finally:
        stop.set()
    if reply is None:
        with lock:
            get_user(uid)["used"] = max(0, get_user(uid)["used"] - 1)
            save_db()
        reply = ERR_TEXT
    tg_send(base, chat_id, reply)


def process_update(base, upd):
    if "callback_query" in upd:
        tg_callback(base, upd["callback_query"])
        return
    if "pre_checkout_query" in upd:
        tg_api(
            base, "answerPreCheckoutQuery",
            pre_checkout_query_id=upd["pre_checkout_query"]["id"], ok=True,
        )
        return
    msg = upd.get("message")
    if not msg or "from" not in msg:
        return
    if "successful_payment" in msg:
        on_paid(base, msg)
        return
    text = msg.get("text")
    if not text:
        return
    chat_id, uid = msg["chat"]["id"], msg["from"]["id"]
    if text.startswith("/"):
        waiting_promo.discard(uid)
        tg_command(base, chat_id, uid, text)
    elif uid in waiting_promo:
        waiting_promo.discard(uid)
        _, reply = redeem_promo(uid, text)
        tg_api(base, "sendMessage", chat_id=chat_id, text=reply)
    else:
        threading.Thread(
            target=tg_handle, args=(base, chat_id, uid, text), daemon=True
        ).start()


def telegram_loop():
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    offset = 0
    tg_api(base, "setMyCommands", commands=[
        {"command": "menu", "description": "Меню"},
        {"command": "model", "description": "Выбрать модель"},
        {"command": "sub", "description": "Подписки"},
        {"command": "promo", "description": "Ввести промокод"},
        {"command": "profile", "description": "Профиль и лимиты"},
        {"command": "new", "description": "Новый диалог"},
    ])
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
                try:
                    process_update(base, upd)
                except Exception as e:
                    print("update error:", e)
        except Exception as e:
            print("Telegram error:", e)
            time.sleep(5)


# ================= VK =================
VK_V = "5.131"


def vk_call(method, **params):
    params.update(access_token=VK_TOKEN, v=VK_V)
    return requests.post(
        f"https://api.vk.com/method/{method}", data=params, timeout=30
    ).json()


def vk_handle(peer, text):
    stop = threading.Event()
    threading.Thread(
        target=keep_typing,
        args=(
            lambda: vk_call(
                "messages.setActivity", peer_id=peer, type="typing", group_id=VK_GROUP_ID
            ),
            stop,
        ),
        daemon=True,
    ).start()
    try:
        reply = ask_ai(f"vk{peer}", text, MODELS[0])
    finally:
        stop.set()
    for part in chunks(reply or ERR_TEXT):
        vk_call(
            "messages.send",
            peer_id=peer,
            message=part,
            random_id=random.randint(1, 2**31),
        )


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
                    break
                ts = r["ts"]
                for ev in r.get("updates", []):
                    if ev.get("type") != "message_new":
                        continue
                    m = ev["object"]["message"]
                    text = m.get("text", "")
                    if not text:
                        continue
                    threading.Thread(
                        target=vk_handle, args=(m["peer_id"], text), daemon=True
                    ).start()
        except Exception as e:
            print("VK error:", e)
            time.sleep(5)


# ================= Мини-сервер (нужен хостингу) =================
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Orvex is running")

    def log_message(self, *args):
        pass


def main():
    if not OPENROUTER_KEY or not MODELS:
        raise SystemExit("Нужны OPENROUTER_API_KEY и MODEL")
    if TG_TOKEN:
        threading.Thread(target=telegram_loop, daemon=True).start()
    if VK_TOKEN and VK_GROUP_ID:
        threading.Thread(target=vk_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


if __name__ == "__main__":
    main()
