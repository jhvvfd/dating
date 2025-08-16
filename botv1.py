# -*- coding: utf-8 -*-
# path: bot.py (Part 1/3)
from __future__ import annotations
from flask import Flask
import os
import json
import random
import time
import threading
from typing import Optional, Dict, List, Tuple

import telebot
from telebot import types

# --------------------------- Configuration ---------------------------
TOKEN = os.getenv("8083007020:AAFdZ5vsSPJYnaKQImUOVd7IC_JMEPYd66E", "8083007020:AAFdZ5vsSPJYnaKQImUOVd7IC_JMEPYd66E")
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")  # HTML for <b><i>...</i></b>

USER_FILE = "users.json"
MAX_PHOTOS = 3
ANON_MESSAGE_BONUS = 0.01

DEFAULT_FILTERS = {"city": None, "age_min": None, "age_max": None, "pref": None}
WELCOME_IMAGE_URL = ""   # опционально: URL красивой картинки для приветствия
SAFETY_IMAGE_URL = ""    # опционально: URL для сообщений безопасности

# Очередь/пары аноним-чата и статистика беседы
ANON_QUEUE: List[int] = []
ANON_PEERS: Dict[int, int] = {}
ANON_STATS: Dict[int, dict] = {}  # {uid: {partner, start_ts, counts:{text,photo,video,audio,voice,document,sticker}}}

# Сессии и временные состояния
USER_SESSION: Dict[int, dict] = {}
SEARCH_STATE: Dict[int, dict] = {}

# Лента "Кто меня лайкнул": { user_id: {"queue": [ids], "idx": int, "current": int} }
LIKERS_STATE: Dict[int, dict] = {}

app = Flask(name)

@app.route("/")
def home():
    return "Render done"

if name == "piska":
    app.run(host="0.0.0.0", port=5000)

def get_actions_kb() -> types.ReplyKeyboardMarkup:
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row("❤️ Лайк", "⏭ Скип")
    m.row("💬 Сообщение", "🚫 Блок")
    m.row("⚠️ Пожаловаться", "⬅ Назад")
    return m

# --------------------------- Storage ---------------------------
if os.path.exists(USER_FILE):
    with open(USER_FILE, "r", encoding="utf-8") as f:
        users: Dict[str, dict] = json.load(f)
else:
    users = {}

def now_ts() -> float:
    return time.time()

def save_users() -> None:
    with open(USER_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

# --------------------------- Migrations ---------------------------
for uid, u in list(users.items()):
    u.setdefault("likes", [])
    u.setdefault("matches", [])
    u.setdefault("lightnings", 0.0)
    u.setdefault("age", None)
    u.setdefault("name", "Неизвестно")
    u.setdefault("city", "Не указан")
    u.setdefault("about", "Нет описания")
    u.setdefault("photos", [])
    u.setdefault("gender", None)
    u.setdefault("pref", "any")
    u.setdefault("blocked", [])
    u.setdefault("seen", [])
    u.setdefault("reports", 0)
    u.setdefault("last_active", 0.0)
    u.setdefault("filters", dict(DEFAULT_FILTERS))
    u.setdefault("status", "active")  # active|frozen|deactivated
    u.setdefault("incoming_likes_unseen", [])
    u.setdefault("last_inactive_nudge", 0.0)
    u.setdefault("last_safety_nudge", 0.0)
    u.setdefault("pending_rate", [])  # [{partner: int, due: float}]
    u.setdefault("ratings_received_total", 0)
    u.setdefault("ratings_received_count", 0)

# --------------------------- City normalization ---------------------------
CITY_TRIGGERS = {
    # Москва
    "мск": "Москва", "moscow": "Москва", "москва": "Москва",
    # Санкт-Петербург
    "спб": "Санкт-Петербург", "spb": "Санкт-Петербург", "питер": "Санкт-Петербург",
    "санкт-петербург": "Санкт-Петербург", "санкт петербург": "Санкт-Петербург",
    # Новосибирск
    "нск": "Новосибирск", "новосиб": "Новосибирск", "новосибирск": "Новосибирск",
    # Екатеринбург
    "екб": "Екатеринбург", "екатеринбург": "Екатеринбург",
    # Казань
    "казань": "Казань",
    # Нижний Новгород
    "нн": "Нижний Новгород", "нижний новгород": "Нижний Новгород",
    # Самара
    "самара": "Самара",
    # Ростов-на-Дону
    "ростов": "Ростов-на-Дону", "ростов на дону": "Ростов-на-Дону", "rnd": "Ростов-на-Дону",
    # Уфа
    "уфа": "Уфа",
    # Красноярск
    "красноярск": "Красноярск",
    # Пермь
    "пермь": "Пермь",
    # Воронеж
    "воронеж": "Воронеж",
    # Волгоград
    "волгоград": "Волгоград",
    # Омск
    "омск": "Омск",
    # Челябинск
    "челябинск": "Челябинск",
    # Минск/Алматы/Киев
    "минск": "Минск", "алматы": "Алматы", "киев": "Киев", "київ": "Киев",
}

POPULAR_CITIES = list({v for v in CITY_TRIGGERS.values()}) + [
    "Тюмень", "Иркутск", "Томск", "Сочи", "Краснодар"
]

def normalize_city(raw: str) -> Tuple[Optional[str], List[str]]:
    if not raw:
        return None, POPULAR_CITIES[:6]
    txt = raw.strip().lower().replace(".", " ").replace(",", " ")
    if txt in CITY_TRIGGERS:
        return CITY_TRIGGERS[txt], []
    for key, val in CITY_TRIGGERS.items():
        if txt.startswith(key):
            return val, []
    guesses = [c for c in POPULAR_CITIES if c.lower().startswith(txt[:3])]
    return (None, guesses[:6]) if guesses else (None, POPULAR_CITIES[:6])

# --------------------------- Formatting helpers ---------------------------
def decorate(text: str) -> str:
    return f"<b><i>{text}</i></b>"

def stars(avg: float) -> str:
    if avg <= 0:
        return "—"
    full = int(round(avg))
    full = max(1, min(full, 5))
    return "⭐" * full + "☆" * (5 - full)

def profile_caption(u: dict, prefix: str = "👤 Анкета") -> str:
    avg = 0.0
    if u.get("ratings_received_count", 0) > 0:
        avg = u["ratings_received_total"] / float(u["ratings_received_count"])
    gender_h = {"male": "👨", "female": "👩"}.get(u.get("gender"), "❓")
    pref_h = {"male": "парни", "female": "девушки", "any": "все"}.get(u.get("pref", "any"), "все")
    about = (u.get("about") or "Нет описания").strip()
    about = about.replace("\n\n", "\n").replace("\n", "\n\n")
    return (
        f"<b>{prefix}</b>\n"
        f"— — — — — — — — —\n"
        f"👤 <b>{u.get('name','Неизвестно')}</b>, {u.get('age','?')} лет\n"
        f"🌆 {u.get('city','Не указан')}\n"
        f"⚧ Пол: {gender_h}  •  🎯 Предпочтения: {pref_h}\n"
        f"⭐ Рейтинг: <b>{avg:.1f}</b> {stars(avg)}\n"
        f"⚡ Молнии: <b>{u.get('lightnings',0):.2f}</b>\n\n"
        f"📝 <i>{about}</i>"
    )

def send_card(chat_id: int, target_id: int, prefix: str = "👤 Анкета", extra_kb: Optional[types.InlineKeyboardMarkup] = None):
    if str(target_id) not in users:
        return
    u = users[str(target_id)]
    media_ids = u.get("photos", [])
    if media_ids:
        if len(media_ids) == 1:
            bot.send_photo(chat_id, media_ids[0], caption=profile_caption(u, prefix), reply_markup=extra_kb)
        else:
            media = [types.InputMediaPhoto(p) for p in media_ids]
            bot.send_media_group(chat_id, media)
            bot.send_message(chat_id, profile_caption(u, prefix), reply_markup=extra_kb)
    else:
        bot.send_message(chat_id, profile_caption(u, prefix), reply_markup=extra_kb)

def nice_menu_title(title: str) -> str:
    return decorate(f"✨ {title} ✨")

def can_notify(u: dict) -> bool:
    return u.get("status", "active") == "active"

# --------------------------- Scheduler (background jobs) ---------------------------
def scheduler_loop():
    while True:
        try:
            now = now_ts()
            # 1) Inactive 24h reminder
            for uid, u in list(users.items()):
                if u.get("status") != "active":
                    continue
                last = u.get("last_active", 0)
                last_ping = u.get("last_inactive_nudge", 0)
                if last > 0 and now - last >= 24 * 3600 and now - last_ping >= 12 * 3600:
                    try:
                        bot.send_message(int(uid), decorate("👋 Давненько тебя не видели! Появилось много новых анкет. Загляни — вдруг там твой идеальный собеседник 😉"))
                        u["last_inactive_nudge"] = now
                    except Exception:
                        pass

            # 2) Safety reminder every 30h
            for uid, u in list(users.items()):
                if u.get("status") != "active":
                    continue
                last_s = u.get("last_safety_nudge", 0)
                if now - last_s >= 30 * 3600:
                    try:
                        if SAFETY_IMAGE_URL:
                            bot.send_photo(int(uid), SAFETY_IMAGE_URL, caption=decorate("🔐 Будь аккуратнее: не отправляй личные данные и не переходи по подозрительным ссылкам.\nЕсли что-то насторожило — пожалуйся через кнопку ⚠️."))
                        else:
                            bot.send_message(int(uid), decorate("🔐 Напоминание безопасности: береги личные данные, проверяй профили, не отправляй деньги и ссылки сомнительным пользователям."))
                        u["last_safety_nudge"] = now
                    except Exception:
                        pass

            # 3) Rating prompts
            for uid, u in list(users.items()):
                if not can_notify(u):
                    continue
                pending = u.get("pending_rate", [])
                new_pending = []
                for item in pending:
                    partner = item.get("partner")
                    due = item.get("due", 0)
                    if now >= due:
                        try:
                            kb = types.InlineKeyboardMarkup()
                            row = [types.InlineKeyboardButton(f"{i} ⭐", callback_data=f"rate:{partner}:{i}") for i in range(1, 6)]
                            kb.row(*row)
                            bot.send_message(int(uid), decorate("📝 Оцените собеседника по итогам знакомства:"), reply_markup=kb)
                        except Exception:
                            pass
                    else:
                        new_pending.append(item)
                if len(new_pending) != len(pending):
                    u["pending_rate"] = new_pending
            save_users()
        except Exception:
            pass
        time.sleep(600)  # каждые 10 минут

threading.Thread(target=scheduler_loop, daemon=True).start()
# path: bot.py (Part 2/3)

# --------------------------- Menus ---------------------------
def main_menu(chat_id: int) -> None:
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row("🔍 Поиск", "📝 Мой профиль")
    m.row("💘 Кто меня лайкнул", "🔥 Мэтчи")
    m.row("🕵️ Анонимный чат", "🏆 Топ активности")
    m.row("⚙ Настройки")
    bot.send_message(chat_id, nice_menu_title("Главное меню"), reply_markup=m)

def settings_menu(chat_id: int) -> None:
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.add("✏ Имя", "🎂 Возраст", "🌆 Город", "📝 Описание")
    m.add("🎯 Предпочтения", "🔎 Фильтры поиска")
    m.add("⚡ Молнии", "🚫 Чёрный список")
    m.add("🧊 Заморозить / Разморозить", "🗑 Деактивировать / Активировать")
    m.add("⬅ Назад", "🏠 Главное меню")
    bot.send_message(chat_id, nice_menu_title("Настройки"), reply_markup=m)

def filters_menu(chat_id: int) -> None:
    f = users[str(chat_id)].get("filters", dict(DEFAULT_FILTERS))
    human_city = f.get("city") or "Любой"
    human_age = "Любой"
    if f.get("age_min") is not None or f.get("age_max") is not None:
        amin = f.get("age_min") or "?"
        amax = f.get("age_max") or "?"
        human_age = f"{amin}-{amax}"
    human_pref = {"male": "Парни", "female": "Девушки", None: "Как в профиле"}.get(f.get("pref"), "Все")

    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.add("🏙️ Фильтр по городу", "🎚 Фильтр по возрасту")
    m.add("🎯 Кого показывать", "🔄 Сброс просмотра")
    m.add("🧹 Сбросить фильтры")
    m.add("⬅ Назад", "🏠 Главное меню")
    bot.send_message(
        chat_id,
        decorate(
            f"🔎 Фильтры поиска\n\n"
            f"Город: <b>{human_city}</b>\n"
            f"Возраст: <b>{human_age}</b>\n"
            f"Кого показывать: <b>{human_pref}</b>\n\n"
            f"Выбери, что настроить:"
        ),
        reply_markup=m,
    )

def show_lightnings_info(chat_id: int) -> None:
    u = users[str(chat_id)]
    bot.send_message(
        chat_id,
        decorate(
            "⚡ Система Молний\n\n"
            "Молнии — бонусные очки за активность:\n"
            "❤️ Лайк: +0.1\n"
            "💬 Сообщение: +0.2\n"
            f"🕵️ Анонимный чат — каждое сообщение: +{ANON_MESSAGE_BONUS:.2f}\n\n"
            f"Текущий баланс: <b>{u.get('lightnings',0):.2f}</b> ⚡"
        ),
    )

# --------------------------- Start / Welcome ---------------------------
@bot.message_handler(commands=["start"])
def start_message(message):
    chat_id = message.chat.id
    user = users.get(str(chat_id))
    if user and user.get("status") != "deactivated":
        users[str(chat_id)].setdefault("filters", dict(DEFAULT_FILTERS))
        save_users()
        m = types.ReplyKeyboardMarkup(resize_keyboard=True)
        m.row("Продолжить с анкетой")
        m.row("Создать новую анкету")
        if WELCOME_IMAGE_URL:
            bot.send_photo(chat_id, WELCOME_IMAGE_URL, caption=decorate("Добро пожаловать обратно! 🎉"))
        bot.send_message(
            chat_id,
            decorate("✨ Я помню твою анкету. Можешь продолжить или создать новую."),
            reply_markup=m,
        )
    else:
        if WELCOME_IMAGE_URL:
            bot.send_photo(chat_id, WELCOME_IMAGE_URL, caption=decorate("Добро пожаловать в Marsellio — место, где встречаются новые друзья и симпатии 💫"))
        bot.send_message(
            chat_id,
            decorate(
                "🌟 Привет! Добро пожаловать в Marsellio.\n\n"
                "Здесь ты сможешь знакомиться, общаться и находить друзей. "
                "Мы поможем оформить красивую анкету и подобрать собеседников по твоим параметрам. "
                "Готов(а) начать?"
            ),
        )
        start_registration(message)

# --------------------------- Registration ---------------------------
def start_registration(message) -> None:
    chat_id = message.chat.id
    USER_SESSION[chat_id] = {"state": "registration", "step": "age", "data": {"photos": []}}
    bot.send_message(chat_id, decorate("🗓 Укажи свой возраст:"))

@bot.message_handler(func=lambda m: m.chat.id in USER_SESSION and USER_SESSION[m.chat.id].get("state") == "registration")
def process_registration(message) -> None:
    chat_id = message.chat.id
    session = USER_SESSION[chat_id]
    step = session["step"]
    data = session["data"]

    if step == "age":
        if not (message.text or "").isdigit():
            bot.send_message(chat_id, decorate("⚠ Введи число, например: 18"))
            return
        age = int(message.text)
        if age < 14:
            bot.send_message(chat_id, decorate("🚫 Бот доступен с 14 лет."))
            return
        data["age"] = age
        session["step"] = "gender"
        m = types.ReplyKeyboardMarkup(resize_keyboard=True)
        m.row("👩 Женский", "👨 Мужской")
        m.row("Пропустить")
        bot.send_message(chat_id, decorate("Выбери свой пол (или 'Пропустить'):"), reply_markup=m)
        return

    if step == "gender":
        txt = (message.text or "").lower().strip()
        if txt == "пропустить":
            data["gender"] = None
        elif "жен" in txt:
            data["gender"] = "female"
        elif "муж" in txt:
            data["gender"] = "male"
        else:
            bot.send_message(chat_id, decorate("⚠ Выбери одну из кнопок или 'Пропустить'."))
            return
        session["step"] = "pref"
        m = types.ReplyKeyboardMarkup(resize_keyboard=True)
        m.row("👩 Девушки", "👨 Парни")
        m.row("Все")
        bot.send_message(chat_id, decorate("Кого показывать?"), reply_markup=m)
        return

    if step == "pref":
        txt = (message.text or "").lower().strip()
        if "девуш" in txt:
            data["pref"] = "female"
        elif "парн" in txt:
            data["pref"] = "male"
        elif txt == "все":
            data["pref"] = "any"
        else:
            bot.send_message(chat_id, decorate("⚠ Выбери с кнопок: Девушки/Парни/Все."))
            return
        session["step"] = "name"
        bot.send_message(chat_id, decorate("✏️ Напиши своё имя или ник:"))
        return

    if step == "name":
        data["name"] = (message.text or "").strip() or "Без имени"
        session["step"] = "city"
        m = types.ReplyKeyboardMarkup(resize_keyboard=True)
        m.row("Москва", "Санкт-Петербург", "Екатеринбург")
        m.row("Новосибирск", "Казань", "Любой")
        m.row("Отмена")
        bot.send_message(chat_id, decorate("🌆 Укажи свой город (можно выбрать из подсказок):"), reply_markup=m)
        return

    if step == "city":
        txt = (message.text or "").strip()
        if txt.lower() in ["отмена", "пропустить"]:
            data["city"] = "Не указан"
        elif txt.lower() == "любой":
            data["city"] = "Не указан"
        else:
            normalized, suggestions = normalize_city(txt)
            if normalized:
                data["city"] = normalized
            else:
                m = types.ReplyKeyboardMarkup(resize_keyboard=True)
                for i in range(0, len(suggestions), 3):
                    m.row(*suggestions[i:i+3])
                m.row("Любой", "Отмена")
                bot.send_message(chat_id, decorate("Не понял город. Выбери из подсказок или введи ещё раз:"), reply_markup=m)
                return
        session["step"] = "photos"
        send_photo_instructions(chat_id)
        return

    if step == "photos":
        text = (message.text or "").strip().lower()
        if text in ["✅ готово", "готово"]:
            if not data["photos"]:
                bot.send_message(chat_id, decorate("⚠ Нужно добавить хотя бы одно фото!"))
                return
            session["step"] = "about"
            m = types.ReplyKeyboardMarkup(resize_keyboard=True)
            m.row("Пропустить")
            bot.send_message(chat_id, decorate("📝 Напиши пару слов о себе (или 'Пропустить'):"), reply_markup=m)
            return
        bot.send_message(chat_id, decorate(f"📸 Прикрепи фото или нажми '✅ Готово'. ({len(data['photos'])}/{MAX_PHOTOS})"))
        return

    if step == "about":
        if (message.text or "").lower().strip() == "пропустить":
            data["about"] = "Нет описания"
        else:
            data["about"] = message.text
        finish_registration(chat_id)
        return

def send_photo_instructions(chat_id: int) -> None:
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row("✅ Готово", "Пропустить")
    bot.send_message(chat_id, decorate(f"📸 Прикрепи до {MAX_PHOTOS} фото."), reply_markup=m)

@bot.message_handler(content_types=["photo"])
def handle_photo(message) -> None:
    chat_id = message.chat.id
    # Регистрация: добавляем фото
    if chat_id in USER_SESSION and USER_SESSION[chat_id].get("state") == "registration" and USER_SESSION[chat_id].get("step") == "photos":
        photos = USER_SESSION[chat_id]["data"]["photos"]
        if len(photos) >= MAX_PHOTOS:
            bot.send_message(chat_id, decorate(f"⚠ Максимум {MAX_PHOTOS} фото."))
            return
        file_id = message.photo[-1].file_id
        photos.append(file_id)
        bot.send_message(chat_id, decorate(f"Фото добавлено! ({len(photos)}/{MAX_PHOTOS})"))
        return
    # Анонимный чат: пересылка фото
    if chat_id in ANON_PEERS:
        anon_forward_any(message)
        return

def finish_registration(chat_id: int) -> None:
    data = USER_SESSION[chat_id]["data"]
    users[str(chat_id)] = {
        "age": data["age"],
        "name": data["name"],
        "city": data.get("city", "Не указан"),
        "about": data.get("about", "Нет описания"),
        "photos": data["photos"],
        "gender": data.get("gender"),
        "pref": data.get("pref", "any"),
        "likes": [],
        "matches": [],
        "blocked": [],
        "seen": [],
        "reports": 0,
        "lightnings": 0.0,
        "last_active": now_ts(),
        "filters": dict(DEFAULT_FILTERS),
        "status": "active",
        "incoming_likes_unseen": [],
        "last_inactive_nudge": 0.0,
        "last_safety_nudge": 0.0,
        "pending_rate": [],
        "ratings_received_total": 0,
        "ratings_received_count": 0,
    }
    save_users()
    USER_SESSION.pop(chat_id, None)
    bot.send_message(chat_id, decorate("✅ Анкета создана!"))
    main_menu(chat_id)

# --------------------------- Matching & Filters ---------------------------
def gender_fits(my_pref: str, target_gender: Optional[str]) -> bool:
    if my_pref == "any":
        return True
    return (target_gender or "") == my_pref

def profile_matches_for(chat_id: int) -> List[int]:
    me = users[str(chat_id)]
    if me.get("status") != "active":
        return []
    f = me.get("filters", dict(DEFAULT_FILTERS))
    pref = f.get("pref") or me.get("pref", "any")

    def city_match(u_city: str) -> bool:
        if not f.get("city"):
            return True
        return (u_city or "").strip().lower() == f["city"].strip().lower()

    def age_match(u_age: Optional[int]) -> bool:
        if u_age is None:
            return True
        amin = f.get("age_min")
        amax = f.get("age_max")
        if amin is not None and u_age < amin:
            return False
        if amax is not None and u_age > amax:
            return False
        return True

    blocked_by_me = set(me.get("blocked", []))
    seen = set(me.get("seen", []))

    candidates: List[int] = []
    for uid, u in users.items():
        if uid == str(chat_id):
            continue
        if u.get("status") != "active":
            continue
        tuid = int(uid)
        if tuid in blocked_by_me:
            continue
        if chat_id in set(u.get("blocked", [])):
            continue
        if tuid in seen:
            continue
        if not gender_fits(pref, u.get("gender")):
            continue
        if not age_match(u.get("age")):
            continue
        if not city_match(u.get("city")):
            continue
        candidates.append(tuid)
    return candidates

def search_profile(chat_id: int) -> None:
    candidates = profile_matches_for(chat_id)
    if not candidates:
        m = types.ReplyKeyboardMarkup(resize_keyboard=True)
        m.row("🔁 Изменить фильтры", "🔄 Сброс просмотра")
        m.row("⬅ Назад")
        bot.send_message(chat_id, decorate("❌ По выбранным фильтрам анкеты не найдены."), reply_markup=m)
        return
    target_id = random.choice(candidates)
    SEARCH_STATE[chat_id] = {"current": target_id}
    users[str(chat_id)].setdefault("seen", []).append(target_id)
    users[str(chat_id)]["last_active"] = now_ts()
    save_users()

    act_kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    act_kb.row("❤️ Лайк", "⏭ Скип")
    act_kb.row("💬 Сообщение", "🚫 Блок")
    act_kb.row("⚠️ Пожаловаться", "🔁 Изменить фильтры")
    act_kb.row("⬅ Назад")
    send_card(chat_id, target_id, prefix="🎯 Рекомендация")
    bot.send_message(chat_id, decorate("Выбери действие:"), reply_markup=act_kb)

# --------------------------- Helpers ---------------------------
def leaderboard_top(n: int = 10) -> List[Tuple[int, float]]:
    pairs = [(int(uid), u.get("lightnings", 0.0)) for uid, u in users.items() if u.get("status") != "deactivated"]
    pairs.sort(key=lambda x: x[1], reverse=True)
    return pairs[:n]

def clear_unseen_likes(chat_id: int) -> None:
    users[str(chat_id)]["incoming_likes_unseen"] = []
    save_users()

# --------------------------- Blacklist UI ---------------------------
def show_blacklist(chat_id: int) -> None:
    me = users[str(chat_id)]
    bl = me.get("blocked", [])
    if not bl:
        bot.send_message(chat_id, decorate("Чёрный список пуст."))
        return
    bot.send_message(chat_id, decorate(f"В чёрном списке {len(bl)} человек(а). Ниже карточки:"))
    for uid in bl[:15]:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Разблокировать", callback_data=f"unblock:{uid}"))
        send_card(chat_id, uid, prefix="🚫 В Чёрном списке", extra_kb=kb)

# --------------------------- Profile ---------------------------
def show_my_profile(chat_id: int) -> None:
    send_card(chat_id, chat_id, prefix="👤 Мой профиль")
    main_menu(chat_id)

# --------------------------- Likes / Matches flow ---------------------------
def notify_like_received(target_id: int, liker_id: int) -> None:
    u = users.get(str(target_id))
    if not u:
        return
    unseen = u.setdefault("incoming_likes_unseen", [])
    if liker_id not in unseen:
        unseen.append(liker_id)
        save_users()
    if can_notify(u):
        cnt = len(unseen)
        try:
            bot.send_message(target_id, decorate(f"💌 Твоя анкета понравилась {cnt} пользовател{'ю' if cnt==1 else 'ям'}. Нажми «Кто меня лайкнул», чтобы посмотреть."))
        except Exception:
            pass

def handle_mutual_match(a_id: int, b_id: int) -> None:
    A = users[str(a_id)]
    B = users[str(b_id)]
    if b_id not in A.get("matches", []):
        A["matches"].append(b_id)
    if a_id not in B.get("matches", []):
        B["matches"].append(a_id)
    save_users()
    def mention(uid: int, u: dict) -> str:
        if u and u.get("username"):
            return f"@{u['username']}"
        return f'<a href="tg://user?id={uid}">{u.get("name","Пользователь")}</a>'
    A["last_active"] = now_ts()
    B["last_active"] = now_ts()
    try:
        bot.send_message(a_id, decorate(f"🔥 У вас метч с {mention(b_id, B)}!"))
    except Exception:
        pass
    try:
        bot.send_message(b_id, decorate(f"🔥 У вас метч с {mention(a_id, A)}!"))
    except Exception:
        pass
    due = now_ts() + 3600
    A.setdefault("pending_rate", []).append({"partner": b_id, "due": due})
    B.setdefault("pending_rate", []).append({"partner": a_id, "due": due})
    save_users()

def like_current(chat_id: int) -> None:
    if chat_id not in SEARCH_STATE:
        return
    target_id = SEARCH_STATE[chat_id]["current"]
    my = users[str(chat_id)]
    trg = users[str(target_id)]
    if target_id not in my.get("likes", []):
        my["likes"].append(target_id)
    my["lightnings"] = my.get("lightnings", 0) + 0.1
    save_users()
    notify_like_received(target_id, chat_id)
    if chat_id in trg.get("likes", []):
        handle_mutual_match(chat_id, target_id)
    else:
        bot.send_message(chat_id, decorate("💌 Лайк отправлен! Ждём взаимности."))
    search_profile(chat_id)

# ---------- Лента «Кто меня лайкнул» (как в поиске) ----------
def _likers_list(chat_id: int) -> List[int]:
    me = users[str(chat_id)]
    res = []
    for uid, u in users.items():
        if int(uid) == chat_id:
            continue
        if me.get("status") != "active":
            continue
        if int(uid) in me.get("blocked", []):
            continue
        if chat_id in u.get("likes", []):
            res.append(int(uid))
    return list(dict.fromkeys(res))

def _likers_start(chat_id: int) -> None:
    queue = _likers_list(chat_id)
    if not queue:
        bot.send_message(chat_id, decorate("Пока никто не лайкнул. Но всё впереди! 😊"))
        return
    LIKERS_STATE[chat_id] = {"queue": queue, "idx": 0, "current": queue[0]}
    users[str(chat_id)]["incoming_likes_unseen"] = []
    save_users()
    _likers_show_current(chat_id)

def _likers_show_current(chat_id: int) -> None:
    st = LIKERS_STATE.get(chat_id)
    if not st:
        bot.send_message(chat_id, decorate("Анкеты лайкнувших закончились."))
        main_menu(chat_id)
        return
    queue, idx = st["queue"], st["idx"]
    if idx >= len(queue):
        LIKERS_STATE.pop(chat_id, None)
        bot.send_message(chat_id, decorate("Ты посмотрел(а) все лайки. Возвращаю в меню."))
        main_menu(chat_id)
        return
    cur = queue[idx]
    st["current"] = cur
    send_card(chat_id, cur, prefix="💘 Тебя лайкнули")
    bot.send_message(chat_id, decorate("Выбери действие:"), reply_markup=get_actions_kb())

def _likers_remove_current(chat_id: int) -> Optional[int]:
    st = LIKERS_STATE.get(chat_id)
    if not st:
        return None
    queue, idx = st["queue"], st["idx"]
    if not queue or idx >= len(queue):
        return None
    return queue.pop(idx)

def _likers_next(chat_id: int) -> None:
    st = LIKERS_STATE.get(chat_id)
    if not st:
        main_menu(chat_id); return
    _likers_show_current(chat_id)

def like_from_likers(chat_id: int) -> None:
    st = LIKERS_STATE.get(chat_id)
    if not st:
        return
    target_id = st["current"]
    my = users[str(chat_id)]
    trg = users[str(target_id)]
    if target_id not in my.get("likes", []):
        my["likes"].append(target_id)
    my["lightnings"] = my.get("lightnings", 0) + 0.1
    save_users()
    if chat_id in trg.get("likes", []):
        handle_mutual_match(chat_id, target_id)
    else:
        bot.send_message(chat_id, decorate("💌 Лайк отправлен! Ждём взаимности."))
    _likers_remove_current(chat_id)
    _likers_next(chat_id)

def skip_from_likers(chat_id: int) -> None:
    _likers_remove_current(chat_id)
    _likers_next(chat_id)

def block_from_likers(chat_id: int) -> None:
    st = LIKERS_STATE.get(chat_id)
    if not st:
        return
    target_id = st["current"]
    me = users[str(chat_id)]
    if target_id not in me.get("blocked", []):
        me.setdefault("blocked", []).append(target_id)
    save_users()
    bot.send_message(chat_id, decorate("Пользователь добавлен в чёрный список."))
    _likers_remove_current(chat_id)
    _likers_next(chat_id)

def report_from_likers(chat_id: int) -> None:
    st = LIKERS_STATE.get(chat_id)
    if not st:
        return
    target_id = st["current"]
    users[str(target_id)]["reports"] = users[str(target_id)].get("reports", 0) + 1
    me = users[str(chat_id)]
    if target_id not in me.get("blocked", []):
        me.setdefault("blocked", []).append(target_id)
    save_users()
    bot.send_message(chat_id, decorate("Спасибо за жалобу. Мы скрыли этого пользователя для тебя."))
    _likers_remove_current(chat_id)
    _likers_next(chat_id)
# path: bot.py (Part 3/3)

# --------------------------- Anon chat ---------------------------
def anon_waiting_menu(chat_id: int) -> None:
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row("⏭ Следующий собеседник", "🔚 Выйти из чата")
    m.row("🤝 Поделиться профилем (@username)")
    bot.send_message(chat_id, nice_menu_title("Поиск собеседника…"), reply_markup=m)

def anon_chat_menu(chat_id: int) -> None:
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row("⏭ Следующий собеседник", "🔚 Выйти из чата")
    m.row("🤝 Поделиться профилем (@username)")
    bot.send_message(chat_id, decorate("🕵️ Вы в анонимном чате.\nНе делитесь личными данными без необходимости."), reply_markup=m)

def _init_anon_stats(a: int, b: int) -> None:
    for uid, partner in [(a, b), (b, a)]:
        ANON_STATS[uid] = {
            "partner": partner,
            "start_ts": now_ts(),
            "counts": {"text": 0, "photo": 0, "video": 0, "audio": 0, "voice": 0, "document": 0, "sticker": 0},
        }

def _finish_anon_stats(uid: int) -> dict:
    return ANON_STATS.pop(uid, None) or {}

def _send_anon_summary(uid: int, stats: dict) -> None:
    if not stats:
        return
    start = stats.get("start_ts", now_ts())
    dur = int(now_ts() - start)
    mins = dur // 60
    secs = dur % 60
    c = stats.get("counts", {})
    total = sum(c.values())
    bot.send_message(
        uid,
        decorate(
            "📊 Итоги анонимного чата:\n\n"
            f"⏱ Длительность: <b>{mins}м {secs}с</b>\n"
            f"✉ Сообщений всего: <b>{total}</b>\n"
            f"• Текст: <b>{c.get('text',0)}</b>\n"
            f"• Фото: <b>{c.get('photo',0)}</b>  • Видео: <b>{c.get('video',0)}</b>\n"
            f"• Голос: <b>{c.get('voice',0)}</b>  • Аудио: <b>{c.get('audio',0)}</b>\n"
            f"• Документы: <b>{c.get('document',0)}</b>  • Стикеры: <b>{c.get('sticker',0)}</b>"
        ),
    )

def start_anon_chat(chat_id: int) -> None:
    if users.get(str(chat_id), {}).get("status") != "active":
        bot.send_message(chat_id, decorate("🧊 Анкета неактивна. Разморозь/активируй её в настройках."))
        return
    if chat_id in ANON_PEERS:
        anon_chat_menu(chat_id)
        return
    if chat_id in ANON_QUEUE:
        anon_waiting_menu(chat_id)
        return
    if ANON_QUEUE:
        partner = ANON_QUEUE.pop(0)
        if partner == chat_id:
            start_anon_chat(chat_id)
            return
        ANON_PEERS[chat_id] = partner
        ANON_PEERS[partner] = chat_id
        _init_anon_stats(chat_id, partner)
        bot.send_message(partner, decorate("🔗 Нашёлся собеседник!"))
        anon_chat_menu(partner)
        bot.send_message(chat_id, decorate("🔗 Нашёлся собеседник!"))
        anon_chat_menu(chat_id)
        return
    ANON_QUEUE.append(chat_id)
    anon_waiting_menu(chat_id)

def _end_chat_for(uid: int, notify_partner: bool = True):
    partner = ANON_PEERS.pop(uid, None)
    stats_uid = _finish_anon_stats(uid)
    _send_anon_summary(uid, stats_uid)
    if partner is not None:
        ANON_PEERS.pop(partner, None)
        stats_partner = _finish_anon_stats(partner)
        _send_anon_summary(partner, stats_partner)
        if notify_partner:
            bot.send_message(partner, decorate("❗ Собеседник покинул чат."))
            main_menu(partner)
    main_menu(uid)

def stop_anon_chat(chat_id: int) -> None:
    if chat_id in ANON_QUEUE:
        ANON_QUEUE.remove(chat_id)
        bot.send_message(chat_id, decorate("🚪 Вы вышли из поиска анонимного чата."))
        main_menu(chat_id)
        return
    if chat_id in ANON_PEERS:
        _end_chat_for(chat_id, notify_partner=True)
        return
    bot.send_message(chat_id, decorate("Вы не в анонимном чате."))

def anon_next_partner(chat_id: int) -> None:
    if chat_id in ANON_QUEUE:
        return
    if chat_id in ANON_PEERS:
        _end_chat_for(chat_id, notify_partner=True)
    start_anon_chat(chat_id)

def anon_forward_any(message) -> None:
    chat_id = message.chat.id
    if chat_id not in ANON_PEERS:
        return
    partner = ANON_PEERS.get(chat_id)
    try:
        bot.copy_message(partner, chat_id, message.message_id)
        if str(chat_id) in users:
            users[str(chat_id)]["lightnings"] = users[str(chat_id)].get("lightnings", 0) + ANON_MESSAGE_BONUS
        st = ANON_STATS.get(chat_id)
        if st:
            if message.content_type in ("video", "audio", "voice", "document", "sticker", "photo"):
                key = "photo" if message.content_type == "photo" else message.content_type
            else:
                key = "text"
            st["counts"][key] = st["counts"].get(key, 0) + 1
        save_users()
    except Exception:
        bot.send_message(chat_id, decorate("⚠ Не удалось отправить сообщение."))

@bot.message_handler(content_types=["video", "audio", "voice", "document", "sticker", "video_note", "location", "contact"])
def anon_media_router(message):
    anon_forward_any(message)

# --------------------------- Callbacks (inline buttons) ---------------------------
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("unblock:"))
def cb_unblock(call):
    try:
        uid = call.from_user.id
        target = int(call.data.split(":")[1])
        me = users.get(str(uid))
        if me and target in me.get("blocked", []):
            me["blocked"].remove(target)
            save_users()
            bot.answer_callback_query(call.id, "Разблокировано")
            bot.send_message(uid, decorate("✅ Пользователь разблокирован и снова может появляться в поиске."))
        else:
            bot.answer_callback_query(call.id, "Не найдено")
    except Exception:
        pass

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("rate:"))
def cb_rate(call):
    try:
        uid = call.from_user.id
        _, partner_str, val_str = call.data.split(":")
        partner = int(partner_str)
        val = int(val_str)
        if str(partner) in users and str(uid) in users:
            users[str(partner)]["ratings_received_total"] = users[str(partner)].get("ratings_received_total", 0) + val
            users[str(partner)]["ratings_received_count"] = users[str(partner)].get("ratings_received_count", 0) + 1
            pending = users[str(uid)].get("pending_rate", [])
            users[str(uid)]["pending_rate"] = [j for j in pending if j.get("partner") != partner]
            save_users()
            bot.answer_callback_query(call.id, "Спасибо за оценку!")
            bot.send_message(uid, decorate("🙏 Спасибо! Ваша оценка учтена."))
        else:
            bot.answer_callback_query(call.id, "Профиль не найден")
    except Exception:
        pass

# --------------------------- Text router ---------------------------
@bot.message_handler(func=lambda m: True, content_types=["text"])
def menu_handler(message) -> None:
    chat_id = message.chat.id
    text = (message.text or "").strip()

    if str(chat_id) in users:
        users[str(chat_id)]["username"] = message.from_user.username or users[str(chat_id)].get("username")

    if chat_id in USER_SESSION and USER_SESSION[chat_id].get("state") == "registration":
        return

    if str(chat_id) not in users or users[str(chat_id)].get("status") == "deactivated":
        start_registration(message)
        return

    u = users.get(str(chat_id))
    if u:
        last = u.get("last_active", 0)
        if now_ts() - last < 0.4:
            return
        u["last_active"] = now_ts()
        save_users()

    # Анонимный чат
    if text == "🕵️ Анонимный чат":
        start_anon_chat(chat_id); return
    if text == "🔚 Выйти из чата":
        stop_anon_chat(chat_id); return
    if text == "⏭ Следующий собеседник":
        anon_next_partner(chat_id); return
    if text == "🤝 Поделиться профилем (@username)":
        if chat_id in ANON_PEERS:
            partner = ANON_PEERS.get(chat_id)
            uname = message.from_user.username
            if uname:
                bot.send_message(partner, decorate(f"🤝 Собеседник поделился профилем: @{uname}"))
                bot.send_message(chat_id, decorate("Отправили ваш @username собеседнику."))
            else:
                link = f'tg://user?id={chat_id}'
                bot.send_message(partner, decorate(f'🤝 Собеседник поделился профилем: <a href="{link}">открыть профиль</a>'))
                bot.send_message(chat_id, decorate("У вас нет @username. Отправили ссылку-профиль собеседнику."))
        else:
            bot.send_message(chat_id, decorate("Вы не в анонимном чате."))
        return
    if chat_id in ANON_PEERS:
        anon_forward_any(message); return

    # Главные пункты
    if text == "🔍 Поиск":
        search_profile(chat_id); return
    if text in ("📝 Мой профиль", "Мой профиль"):
        show_my_profile(chat_id); return
    if text == "🔥 Мэтчи":
        me = users[str(chat_id)]
        matches = me.get("matches", [])
        if not matches:
            bot.send_message(chat_id, decorate("🔥 Пока нет совпадений."))
        else:
            bot.send_message(chat_id, decorate("🔥 Ваши метчи:"))
            for uid in matches[:20]:
                send_card(chat_id, uid, prefix="🔥 Метч")
        return
    if text == "💘 Кто меня лайкнул":
        _likers_start(chat_id); return
    if text == "🏆 Топ активности":
        top = leaderboard_top()
        if not top:
            bot.send_message(chat_id, decorate("Пока пусто."))
        else:
            lines = [f"{idx+1}. {users[str(uid)]['name']} — {score:.2f}⚡" for idx, (uid, score) in enumerate(top)]
            bot.send_message(chat_id, decorate("🏆 Топ по молниям:\n" + "\n".join(lines)))
        return
    if text == "⚙ Настройки":
        settings_menu(chat_id); return
    if text == "⚡ Молнии":
        show_lightnings_info(chat_id); return
    if text in ["⬅ Назад", "🏠 Главное меню"]:
        if chat_id in LIKERS_STATE:
            LIKERS_STATE.pop(chat_id, None)
            bot.send_message(chat_id, decorate("Выход из раздела «Кто меня лайкнул»."))
        main_menu(chat_id); return

    # Настройки: редактирование
    if text == "✏ Имя":
        bot.send_message(chat_id, decorate("✏ Введи новое имя:"))
        USER_SESSION[chat_id] = {"state": "edit", "field": "name"}; return
    if text == "🎂 Возраст":
        bot.send_message(chat_id, decorate("🎂 Введи новый возраст:"))
        USER_SESSION[chat_id] = {"state": "edit", "field": "age"}; return
    if text == "🌆 Город":
        USER_SESSION[chat_id] = {"state": "edit_city"}
        m = types.ReplyKeyboardMarkup(resize_keyboard=True)
        m.row("Москва", "Санкт-Петербург", "Екатеринбург")
        m.row("Новосибирск", "Казань", "Любой")
        m.row("Отмена")
        bot.send_message(chat_id, decorate("🌆 Введи город или выбери подсказку:"), reply_markup=m); return
    if text == "📝 Описание":
        bot.send_message(chat_id, decorate("📝 Введи новое описание:"))
        USER_SESSION[chat_id] = {"state": "edit", "field": "about"}; return
    if text == "🎯 Предпочтения":
        USER_SESSION[chat_id] = {"state": "edit", "field": "pref"}
        m = types.ReplyKeyboardMarkup(resize_keyboard=True)
        m.row("👩 Девушки", "👨 Парни", "Все")
        bot.send_message(chat_id, decorate("Выбери, кого показывать:"), reply_markup=m); return

    # Фильтры
    if text in ["🔎 Фильтры поиска", "🔁 Изменить фильтры"]:
        filters_menu(chat_id); return
    if text == "🏙️ Фильтр по городу":
        USER_SESSION[chat_id] = {"state": "filters", "field": "city"}
        m = types.ReplyKeyboardMarkup(resize_keyboard=True)
        for i in range(0, len(POPULAR_CITIES[:9]), 3):
            m.row(*POPULAR_CITIES[i:i+3])
        m.row("Любой", "Отмена")
        bot.send_message(chat_id, decorate("🏙️ Введи город для фильтра (или выбери):"), reply_markup=m); return
    if text == "🎚 Фильтр по возрасту":
        USER_SESSION[chat_id] = {"state": "filters", "field": "age"}
        m = types.ReplyKeyboardMarkup(resize_keyboard=True)
        m.row("Любой", "Отмена")
        bot.send_message(chat_id, decorate("🎚 Введи диапазон возраста, например 18-30 (или 'Любой'):"), reply_markup=m); return
    if text == "🎯 Кого показывать":
        USER_SESSION[chat_id] = {"state": "filters", "field": "pref"}
        m = types.ReplyKeyboardMarkup(resize_keyboard=True)
        m.row("👩 Девушки", "👨 Парни")
        m.row("Все", "Как в профиле")
        m.row("Отмена")
        bot.send_message(chat_id, decorate("🎯 Кого показывать?"), reply_markup=m); return
    if text == "🧹 Сбросить фильтры":
        users[str(chat_id)]["filters"] = dict(DEFAULT_FILTERS); save_users()
        bot.send_message(chat_id, decorate("🧹 Фильтры сброшены: показываем всех.")); filters_menu(chat_id); return
    if text == "🔄 Сброс просмотра":
        users[str(chat_id)]["seen"] = []; save_users()
        bot.send_message(chat_id, decorate("🔄 История просмотра очищена.")); return

    # Действия в карточках (поиск/лента лайков)
    if text == "❤️ Лайк":
        if chat_id in LIKERS_STATE:
            like_from_likers(chat_id)
        else:
            like_current(chat_id)
        return
    if text == "⏭ Скип":
        if chat_id in LIKERS_STATE:
            skip_from_likers(chat_id)
        else:
            users[str(chat_id)]["lightnings"] = users[str(chat_id)].get("lightnings", 0) + 0.05; save_users()
            search_profile(chat_id)
        return
    if text == "💬 Сообщение":
        if chat_id in LIKERS_STATE:
            target_id = LIKERS_STATE[chat_id]["current"]
            bot.send_message(chat_id, decorate("💬 Введи сообщение для отправки:"))
            USER_SESSION[chat_id] = {"state": "send_message", "target": target_id}
        elif chat_id in SEARCH_STATE:
            target_id = SEARCH_STATE[chat_id]["current"]
            bot.send_message(chat_id, decorate("💬 Введи сообщение для отправки:"))
            USER_SESSION[chat_id] = {"state": "send_message", "target": target_id}
        return
    if text == "🚫 Блок":
        if chat_id in LIKERS_STATE:
            block_from_likers(chat_id)
        elif chat_id in SEARCH_STATE:
            target_id = SEARCH_STATE[chat_id]["current"]
            me = users[str(chat_id)]
            if target_id not in me.get("blocked", []):
                me.setdefault("blocked", []).append(target_id)
            save_users()
            bot.send_message(chat_id, decorate("Пользователь добавлен в чёрный список. Больше не будет показан."))
            search_profile(chat_id)
        return
    if text == "⚠️ Пожаловаться":
        if chat_id in LIKERS_STATE:
            report_from_likers(chat_id)
        elif chat_id in SEARCH_STATE:
            target_id = SEARCH_STATE[chat_id]["current"]
            users[str(target_id)]["reports"] = users[str(target_id)].get("reports", 0) + 1
            me = users[str(chat_id)]
            if target_id not in me.get("blocked", []):
                me.setdefault("blocked", []).append(target_id)
            save_users()
            bot.send_message(chat_id, decorate("Спасибо за жалобу. Мы скрыли этого пользователя для тебя."))
            search_profile(chat_id)
        return

    # Чёрный список
    if text == "🚫 Чёрный список":
        show_blacklist(chat_id); return

    # Фриз/деактивация
    if text == "🧊 Заморозить / Разморозить":
        s = users[str(chat_id)]["status"]
        users[str(chat_id)]["status"] = "active" if s != "active" else "frozen"; save_users()
        bot.send_message(chat_id, decorate(f"Статус профиля: <b>{users[str(chat_id)]['status']}</b>.")); return
    if text == "🗑 Деактивировать / Активировать":
        s = users[str(chat_id)]["status"]
        users[str(chat_id)]["status"] = "active" if s == "deactivated" else "deactivated"; save_users()
        bot.send_message(chat_id, decorate(f"Статус профиля: <b>{users[str(chat_id)]['status']}</b>.")); return

    # States: edit / send_message / filters / edit_city
    if chat_id in USER_SESSION:
        st = USER_SESSION[chat_id].get("state")

        if st == "edit":
            field = USER_SESSION[chat_id]["field"]
            value = text
            if field == "age" and not value.isdigit():
                bot.send_message(chat_id, decorate("⚠ Введи число.")); return
            if field == "age": value = int(value)
            if field == "pref":
                low = (text or "").lower()
                if "девуш" in low: value = "female"
                elif "парн" in low: value = "male"
                else: value = "any"
            users[str(chat_id)][field] = value; save_users()
            bot.send_message(chat_id, decorate("✅ Изменения применены!"))
            USER_SESSION.pop(chat_id, None); settings_menu(chat_id); return

        if st == "send_message":
            target_id = USER_SESSION[chat_id]["target"]
            users[str(chat_id)]["lightnings"] = users[str(chat_id)].get("lightnings", 0) + 0.2
            bot.send_message(target_id, decorate(f"💬 Новое сообщение от <b>{users[str(chat_id)]['name']}</b>:\n\n{text}"))
            bot.send_message(chat_id, decorate("✅ Сообщение отправлено!")); save_users()
            USER_SESSION.pop(chat_id, None)
            if chat_id in LIKERS_STATE:
                _likers_show_current(chat_id)
            else:
                search_profile(chat_id)
            return

        if st == "filters":
            field = USER_SESSION[chat_id].get("field")
            f = users[str(chat_id)].setdefault("filters", dict(DEFAULT_FILTERS))
            if text.lower() == "отмена":
                USER_SESSION.pop(chat_id, None); filters_menu(chat_id); return

            if field == "city":
                if text.lower() == "любой":
                    f["city"] = None
                else:
                    city, sugg = normalize_city(text)
                    if not city:
                        m = types.ReplyKeyboardMarkup(resize_keyboard=True)
                        for i in range(0, len(sugg), 3):
                            m.row(*sugg[i:i+3])
                        m.row("Любой", "Отмена")
                        bot.send_message(chat_id, decorate("Не понял город. Выбери из подсказок или введи ещё раз:"), reply_markup=m)
                        return
                    f["city"] = city
                save_users(); USER_SESSION.pop(chat_id, None)
                bot.send_message(chat_id, decorate("✅ Фильтр по городу обновлён.")); filters_menu(chat_id); return

            if field == "age":
                if text.lower() == "любой":
                    f["age_min"], f["age_max"] = None, None
                else:
                    rng = text.replace(" ", "").split("-")
                    if len(rng) != 2 or not all(p.isdigit() for p in rng):
                        bot.send_message(chat_id, decorate("⚠ Неверный формат. Например: 18-30 или 'Любой'.")); return
                    amin, amax = int(rng[0]), int(rng[1])
                    if amin < 14 or amax < 14 or amin > amax:
                        bot.send_message(chat_id, decorate("⚠ Некорректный диапазон. Минимум 14 лет, min ≤ max.")); return
                    f["age_min"], f["age_max"] = amin, amax
                save_users(); USER_SESSION.pop(chat_id, None)
                bot.send_message(chat_id, decorate("✅ Фильтр по возрасту обновлён.")); filters_menu(chat_id); return

            if field == "pref":
                low = text.lower()
                if "девуш" in low: f["pref"] = "female"
                elif "парн" in low: f["pref"] = "male"
                elif "как в профиле" in low: f["pref"] = None
                else: f["pref"] = "any"
                save_users(); USER_SESSION.pop(chat_id, None)
                bot.send_message(chat_id, decorate("✅ Предпочтения поиска обновлены.")); filters_menu(chat_id); return

        if st == "edit_city":
            if text.lower() in ["отмена", "пропустить"]:
                USER_SESSION.pop(chat_id, None); settings_menu(chat_id); return
            if text.lower() == "любой":
                users[str(chat_id)]["city"] = "Не указан"; save_users()
                bot.send_message(chat_id, decorate("Город обновлён: <b>Не указан</b>.")); USER_SESSION.pop(chat_id, None); settings_menu(chat_id); return
            city, sugg = normalize_city(text)
            if not city:
                m = types.ReplyKeyboardMarkup(resize_keyboard=True)
                for i in range(0, len(sugg), 3):
                    m.row(*sugg[i:i+3])
                m.row("Любой", "Отмена")
                bot.send_message(chat_id, decorate("Не понял город. Выбери из подсказок или введи ещё раз:"), reply_markup=m)
                return
            users[str(chat_id)]["city"] = city; save_users()
            bot.send_message(chat_id, decorate(f"Город обновлён: <b>{city}</b>.")); USER_SESSION.pop(chat_id, None); settings_menu(chat_id); return

    # Фолбэк
    main_menu(chat_id)

# --------------------------- Bot entrypoint ---------------------------
print("Бот запущен…")
bot.polling(none_stop=True)
