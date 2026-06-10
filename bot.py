import os
import re
import json
import asyncio
import logging
from datetime import datetime, date, timezone, timedelta

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import anthropic

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Конфигурация ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROUP_ID = int(os.environ.get("GROUP_ID", "-1003729022693"))
MY_ID = int(os.environ.get("MY_ID", "642291500"))
AD_ACCOUNT = os.environ.get("AD_ACCOUNT", "act_1322638451268170")
META_ACCESS_TOKEN = os.environ["META_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BITRIX_WEBHOOK = os.environ["BITRIX_WEBHOOK"].rstrip("/")

TZ = timezone(timedelta(hours=5))       # UTC+5 для datetime-вычислений
TZ_NAME = "Asia/Almaty"                 # для APScheduler

GRAPH_API_URL = "https://graph.facebook.com/v19.0"

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# --- Инструменты Meta API для Claude (tool_use) ---

META_TOOLS = [
    {
        "name": "get_account_insights",
        "description": (
            "Получить суммарную статистику рекламного кабинета Meta Ads: "
            "общие затраты, количество лидов (заявки через WhatsApp), цена за лид."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_preset": {
                    "type": "string",
                    "description": "Период выборки. Допустимые значения: today, yesterday, last_7d, last_30d, this_month.",
                    "enum": ["today", "yesterday", "last_7d", "last_30d", "this_month"],
                }
            },
            "required": ["date_preset"],
        },
    },
    {
        "name": "get_ads_breakdown",
        "description": (
            "Получить разбивку по отдельным объявлениям: название, потрачено, количество лидов, цена за лид."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_preset": {
                    "type": "string",
                    "description": "Период выборки. Допустимые значения: today, yesterday, last_7d, last_30d, this_month.",
                    "enum": ["today", "yesterday", "last_7d", "last_30d", "this_month"],
                }
            },
            "required": ["date_preset"],
        },
    },
]


def execute_meta_tool(tool_name: str, tool_input: dict) -> str:
    """Выполняет инструмент Meta API, возвращает результат в виде JSON-строки."""
    preset = tool_input.get("date_preset", "today")
    if tool_name == "get_account_insights":
        data = fetch_insights(date_preset=preset)
        return json.dumps(data, ensure_ascii=False)
    if tool_name == "get_ads_breakdown":
        data = fetch_ads_breakdown(date_preset=preset)
        return json.dumps(data, ensure_ascii=False)
    return json.dumps({"error": f"Неизвестный инструмент: {tool_name}"})


# Целевые заявки храним в памяти: {"YYYY-MM-DD": int}
targets_by_date: dict[str, int] = {}

# Объявление, ожидающее подтверждения отключения от MY_ID: {"ad_id", "ad_name", "spend"} | None
pending_action: dict | None = None

# Объявления, по которым владелец сказал "нет" — не спрашивать повторно 24 часа: {ad_id: datetime_until}
snoozed_until: dict[str, datetime] = {}


# --- Работа с Meta Marketing API ---

def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def detect_period(text: str) -> tuple[str, str]:
    """Определяет период по тексту сообщения: (date_preset для Meta API, человекочитаемая подпись)."""
    t = (text or "").lower()
    if "вчера" in t or "yesterday" in t:
        return "yesterday", "вчера"
    if "недел" in t or "week" in t:
        return "last_7d", "последние 7 дней"
    if "месяц" in t or "month" in t:
        return "last_30d", "последние 30 дней"
    return "today", "сегодня"


def fetch_insights(date_preset: str = "today") -> dict:
    """Берёт суммарные данные по аккаунту: затраты, лиды (сообщения WhatsApp), цена за лид."""
    params = {
        "access_token": META_ACCESS_TOKEN,
        "fields": "spend,actions,cost_per_action_type",
        "date_preset": date_preset,
    }
    resp = requests.get(f"{GRAPH_API_URL}/{AD_ACCOUNT}/insights", params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        return {"spend": 0.0, "leads": 0, "cost_per_lead": 0.0}

    row = data[0]
    spend = float(row.get("spend", 0))
    leads = 0
    for action in row.get("actions", []):
        if action.get("action_type") in ("onsite_conversion.messaging_conversation_started_7d", "messaging_conversation_started_7d"):
            leads += int(float(action.get("value", 0)))

    cost_per_lead = (spend / leads) if leads else 0.0
    return {"spend": spend, "leads": leads, "cost_per_lead": cost_per_lead}


def fetch_ads_breakdown(date_preset: str = "today") -> list[dict]:
    """Возвращает список объявлений с затратами, лидами и ценой за лид."""
    params = {
        "access_token": META_ACCESS_TOKEN,
        "fields": "ad_id,ad_name,spend,actions",
        "date_preset": date_preset,
        "level": "ad",
        "limit": 100,
    }
    resp = requests.get(f"{GRAPH_API_URL}/{AD_ACCOUNT}/insights", params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json().get("data", [])

    ads = []
    for row in data:
        spend = float(row.get("spend", 0))
        leads = 0
        for action in row.get("actions", []):
            if action.get("action_type") in ("onsite_conversion.messaging_conversation_started_7d", "messaging_conversation_started_7d"):
                leads += int(float(action.get("value", 0)))
        cost_per_lead = (spend / leads) if leads else None
        ads.append({
            "id": row.get("ad_id"),
            "name": row.get("ad_name", "Без названия"),
            "spend": spend,
            "leads": leads,
            "cost_per_lead": cost_per_lead,
        })
    return ads


def set_ad_status(ad_id: str, status: str) -> None:
    """Меняет статус объявления в Meta Ads (например, ставит на паузу)."""
    resp = requests.post(
        f"{GRAPH_API_URL}/{ad_id}",
        params={"access_token": META_ACCESS_TOKEN, "status": status},
        timeout=30,
    )
    resp.raise_for_status()


def fetch_month_spend() -> float:
    insights = fetch_insights(date_preset="this_month")
    return insights["spend"]


# --- Bitrix24 CRM ---

def _bitrix_date_range(date_preset: str) -> tuple[str, str]:
    """Возвращает (date_from, date_to) для фильтрации в Bitrix24."""
    now = datetime.now(TZ)
    if date_preset == "yesterday":
        d = now - timedelta(days=1)
    else:
        d = now
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    end = d.replace(hour=23, minute=59, second=59, microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


def fetch_bitrix_leads(date_preset: str = "today") -> dict:
    """Возвращает лиды из Bitrix24 за нужный период."""
    date_from, date_to = _bitrix_date_range(date_preset)
    params = {
        "filter[>=DATE_CREATE]": date_from,
        "filter[<=DATE_CREATE]": date_to,
        "select[]": ["ID", "DATE_CREATE", "STATUS_ID", "SOURCE_ID", "UTM_SOURCE", "TITLE"],
        "start": 0,
    }
    resp = requests.get(f"{BITRIX_WEBHOOK}/crm.lead.list", params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    leads = data.get("result", [])

    # Считаем по статусам
    status_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    for lead in leads:
        st = lead.get("STATUS_ID", "UNKNOWN")
        status_counts[st] = status_counts.get(st, 0) + 1
        src = lead.get("UTM_SOURCE") or lead.get("SOURCE_ID") or "Не указан"
        source_counts[src] = source_counts.get(src, 0) + 1

    return {
        "total": len(leads),
        "by_status": status_counts,
        "by_source": source_counts,
    }


def fetch_bitrix_deals(date_preset: str = "today") -> dict:
    """Возвращает сделки из Bitrix24 за нужный период."""
    date_from, date_to = _bitrix_date_range(date_preset)
    params = {
        "filter[>=DATE_CREATE]": date_from,
        "filter[<=DATE_CREATE]": date_to,
        "select[]": ["ID", "DATE_CREATE", "STAGE_ID", "OPPORTUNITY", "CURRENCY_ID", "TITLE"],
        "start": 0,
    }
    resp = requests.get(f"{BITRIX_WEBHOOK}/crm.deal.list", params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    deals = data.get("result", [])

    total_amount = sum(float(d.get("OPPORTUNITY") or 0) for d in deals)
    stage_counts: dict[str, int] = {}
    closed = 0
    for deal in deals:
        st = deal.get("STAGE_ID", "UNKNOWN")
        stage_counts[st] = stage_counts.get(st, 0) + 1
        if st in ("WON", "C1:WON"):  # Bitrix стадия «Успешно»
            closed += 1

    return {
        "total": len(deals),
        "closed": closed,
        "total_amount": total_amount,
        "by_stage": stage_counts,
    }


# --- Вспомогательные функции по конверсии ---

def get_conversion_stats(d: str | None = None):
    d = d or today_str()
    targets = targets_by_date.get(d, 0)
    insights = fetch_insights(date_preset="today" if d == today_str() else "today")
    leads = insights["leads"]
    conversion = (targets / leads * 100) if leads else 0.0
    cost_per_target = (insights["spend"] / targets) if targets else 0.0
    return {
        "targets": targets,
        "leads": leads,
        "spend": insights["spend"],
        "conversion": conversion,
        "cost_per_target": cost_per_target,
    }


def best_and_worst_ads(n: int = 3, date_preset: str = "today"):
    ads = fetch_ads_breakdown(date_preset)
    ranked = [a for a in ads if a["leads"] > 0]
    ranked.sort(key=lambda a: a["cost_per_lead"])
    best = ranked[:n]

    worst_candidates = [a for a in ads if a["spend"] > 0]
    worst_candidates.sort(key=lambda a: (a["cost_per_lead"] is None, -(a["cost_per_lead"] or 0), -a["spend"]))
    worst = worst_candidates[:n]
    return best, worst


# --- Формирование отчёта ---

async def build_daily_report() -> str:
    """Отчёт по умолчанию для /отчёт: сегодня + вчера для сравнения."""
    d = today_str()
    insights = fetch_insights(date_preset="today")
    yesterday = fetch_insights(date_preset="yesterday")
    ads = fetch_ads_breakdown(date_preset="today")

    spend = insights["spend"]
    leads = insights["leads"]
    cost_per_lead = insights["cost_per_lead"]

    targets = targets_by_date.get(d)

    if ads:
        with_leads = [a for a in ads if a["leads"] > 0]
        if with_leads:
            best = min(with_leads, key=lambda a: a["cost_per_lead"])
            best_line = f"{best['name']} — ${best['cost_per_lead']:.2f}/заявка"
        else:
            best_line = "сегодня пока нет объявлений с заявками"

        spenders = [a for a in ads if a["spend"] > 0]
        if spenders:
            worst = max(spenders, key=lambda a: (a["cost_per_lead"] or float("inf")))
            worst_value = f"${worst['cost_per_lead']:.2f}/заявка" if worst["cost_per_lead"] else "потратило, но без заявок"
            worst_line = f"{worst['name']} — {worst_value}"
        else:
            worst_line = "сегодня пока нет расходов по объявлениям"
    else:
        best_line = "сегодня пока нет объявлений с заявками"
        worst_line = "сегодня пока нет расходов по объявлениям"

    if targets is None:
        target_line = "ожидаю от клиента — напишите /целевые N"
        price_line = "ожидаю данные о целевых заявках"
    else:
        conversion = (targets / leads * 100) if leads else 0.0
        cost_per_target = (spend / targets) if targets else 0.0
        target_line = f"{targets} (конверсия: {conversion:.0f}%)"
        price_line = f"${cost_per_target:.2f} (цель снизить)"

    report = (
        "📊 ЕЖЕДНЕВНЫЙ ОТЧЁТ — Автоквартал\n"
        f"📅 Дата: {d}\n"
        f"💰 Потрачено сегодня: ${spend:.2f} (вчера: ${yesterday['spend']:.2f})\n"
        f"📩 Заявок из рекламы: {leads} (цена: ${cost_per_lead:.2f}) "
        f"— вчера: {yesterday['leads']} (цена: ${yesterday['cost_per_lead']:.2f})\n"
        f"🎯 Целевых (из CRM): {target_line}\n"
        f"💵 Цена целевой заявки: {price_line}\n"
        f"🏆 Лучшее объявление: {best_line}\n"
        f"⚠️ Внимание: {worst_line}"
    )
    return report


async def build_period_report(date_preset: str, label: str) -> str:
    """Отчёт за произвольный период (вчера/неделя/месяц), который запрашивает Meta API напрямую."""
    insights = fetch_insights(date_preset=date_preset)
    ads = fetch_ads_breakdown(date_preset=date_preset)

    spend = insights["spend"]
    leads = insights["leads"]
    cost_per_lead = insights["cost_per_lead"]

    if ads:
        with_leads = [a for a in ads if a["leads"] > 0]
        if with_leads:
            best = min(with_leads, key=lambda a: a["cost_per_lead"])
            best_line = f"{best['name']} — ${best['cost_per_lead']:.2f}/заявка ({best['leads']} заявок)"
        else:
            best_line = "нет объявлений с заявками за этот период"

        spenders = [a for a in ads if a["spend"] > 0]
        if spenders:
            worst = max(spenders, key=lambda a: (a["cost_per_lead"] or float("inf")))
            worst_value = f"${worst['cost_per_lead']:.2f}/заявка" if worst["cost_per_lead"] else "потратило, но без заявок"
            worst_line = f"{worst['name']} — {worst_value}"
        else:
            worst_line = "нет расходов по объявлениям за этот период"
    else:
        best_line = "нет объявлений с заявками за этот период"
        worst_line = "нет расходов по объявлениям за этот период"

    report = (
        f"📊 ОТЧЁТ — Автоквартал ({label})\n"
        f"💰 Потрачено: ${spend:.2f}\n"
        f"📩 Заявок из рекламы: {leads} (цена: ${cost_per_lead:.2f})\n"
        f"🏆 Лучшее объявление: {best_line}\n"
        f"⚠️ Худшее объявление: {worst_line}"
    )
    return report




# --- Алерты ---

async def check_alerts(app: Application):
    global pending_action
    try:
        insights = fetch_insights(date_preset="today")
        ads = fetch_ads_breakdown(date_preset="today")
        d = today_str()
        now = datetime.now(TZ)

        # Объявление тратит бюджет без единой заявки за 24 часа — спрашиваем разрешение отключить.
        if pending_action is None:
            for ad in ads:
                if ad["leads"] == 0 and ad["spend"] > 2000 and ad["id"]:
                    until = snoozed_until.get(ad["id"])
                    if until and now < until:
                        continue
                    pending_action = {"ad_id": ad["id"], "ad_name": ad["name"], "spend": ad["spend"]}
                    await app.bot.send_message(
                        chat_id=MY_ID,
                        text=f"Объявление {ad['name']}: потрачено ${ad['spend']:.2f}, заявок 0 за 24 часа. Отключить?",
                    )
                    break

        # Остальное — короткие факты в личку, без советов.
        if insights["leads"] and insights["cost_per_lead"] > 800:
            await app.bot.send_message(
                chat_id=MY_ID,
                text=f"Цена заявки: ${insights['cost_per_lead']:.2f} (порог $800).",
            )

        targets = targets_by_date.get(d)
        if targets is not None and insights["leads"]:
            conversion = targets / insights["leads"] * 100
            if conversion < 15:
                await app.bot.send_message(
                    chat_id=MY_ID,
                    text=f"Конверсия: {conversion:.0f}% ({targets} целевых из {insights['leads']} заявок, порог 15%).",
                )
    except Exception:
        logger.exception("Ошибка при проверке алертов")


# --- Подтверждение действий владельцем (MY_ID, личка) ---

class OwnerConfirmationFilter(filters.MessageFilter):
    """Срабатывает только если есть открытый запрос на действие и пришёл ответ да/нет от владельца."""

    YES = ("да", "yes", "ага", "угу", "+")
    NO = ("нет", "no", "не", "-")

    def filter(self, message) -> bool:
        if pending_action is None:
            return False
        if message.chat.type != "private" or not message.from_user or message.from_user.id != MY_ID:
            return False
        text = (message.text or "").strip().lower()
        return text in self.YES or text in self.NO


owner_confirmation_filter = OwnerConfirmationFilter()


async def handle_owner_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global pending_action
    action = pending_action
    if action is None:
        return

    text = (update.message.text or "").strip().lower()
    ad_id = action["ad_id"]
    ad_name = action["ad_name"]
    spend = action["spend"]

    if text in OwnerConfirmationFilter.YES:
        try:
            set_ad_status(ad_id, "PAUSED")
            pending_action = None
            await update.message.reply_text(
                f"Готово. Отключил {ad_name}. Бюджет ${spend:.2f}/день освобождён."
            )
            await context.bot.send_message(
                chat_id=GROUP_ID,
                text=f"Оптимизация: отключено объявление {ad_name}. Экономия ${spend:.2f}/день.",
            )
        except Exception:
            logger.exception("Не удалось отключить объявление через Meta API")
            await update.message.reply_text(
                f"Не получилось отключить {ad_name} через Meta API. Попробую ещё раз позже."
            )
    else:
        snoozed_until[ad_id] = datetime.now(TZ) + timedelta(hours=24)
        pending_action = None
        await update.message.reply_text("Понял")


# --- Команды ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот аналитики Meta Ads для Автоквартал.\n\n"
        "Команды:\n"
        "/отчёт — отчёт за сегодня (со сравнением с вчера)\n"
        "/отчёт за вчера / за неделю / за месяц — отчёт за нужный период\n"
        "/целевые N — указать сколько целевых заявок было сегодня\n"
        "/топ — топ-3 лучших объявления\n"
        "/флоп — топ-3 худших объявления\n"
        "/бюджет — потрачено за месяц\n"
        "/конверсия — текущая конверсия\n\n"
        "Можете просто написать вопрос — я отвечу с учётом данных рекламы."
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_preset, label = detect_period(update.message.text)
    try:
        if date_preset == "today":
            report = await build_daily_report()
        else:
            report = await build_period_report(date_preset, label)
        await update.message.reply_text(report)
    except Exception:
        logger.exception("Ошибка при формировании отчёта")
        await update.message.reply_text("Не удалось получить данные из Meta API. Попробуйте позже.")


async def cmd_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Использование: /целевые N, например /целевые 3")
        return
    try:
        n = int(parts[1])
    except ValueError:
        await update.message.reply_text("Нужно указать число, например: /целевые 3")
        return

    d = today_str()
    targets_by_date[d] = n

    try:
        insights = fetch_insights(date_preset="today")
        leads = insights["leads"]
        spend = insights["spend"]
        conversion = (n / leads * 100) if leads else 0.0
        cost_per_target = (spend / n) if n else 0.0
        await update.message.reply_text(
            f"Записал: {n} целевых заявок за {d}.\n"
            f"Конверсия: {conversion:.0f}% ({n} из {leads})\n"
            f"Цена целевой заявки: ${cost_per_target:.2f}"
        )
    except Exception:
        logger.exception("Ошибка при расчёте конверсии")
        await update.message.reply_text(f"Записал: {n} целевых заявок за {d}.")


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        best, _ = best_and_worst_ads(3)
        if not best:
            await update.message.reply_text("Пока нет объявлений с заявками за сегодня.")
            return
        lines = ["🏆 Топ-3 лучших объявления:"]
        for i, ad in enumerate(best, 1):
            lines.append(f"{i}. {ad['name']} — ${ad['cost_per_lead']:.2f}/заявка ({ad['leads']} заявок)")
        await update.message.reply_text("\n".join(lines))
    except Exception:
        logger.exception("Ошибка /топ")
        await update.message.reply_text("Не удалось получить данные из Meta API.")


async def cmd_flop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        _, worst = best_and_worst_ads(3)
        if not worst:
            await update.message.reply_text("Пока нет данных по объявлениям за сегодня.")
            return
        lines = ["📉 Топ-3 худших объявления:"]
        for i, ad in enumerate(worst, 1):
            if ad["cost_per_lead"]:
                lines.append(f"{i}. {ad['name']} — ${ad['cost_per_lead']:.2f}/заявка, потрачено ${ad['spend']:.2f}")
            else:
                lines.append(f"{i}. {ad['name']} — потрачено ${ad['spend']:.2f} без заявок")
        await update.message.reply_text("\n".join(lines))
    except Exception:
        logger.exception("Ошибка /флоп")
        await update.message.reply_text("Не удалось получить данные из Meta API.")


async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        spend = fetch_month_spend()
        await update.message.reply_text(f"💰 Потрачено за месяц: ${spend:.2f}")
    except Exception:
        logger.exception("Ошибка /бюджет")
        await update.message.reply_text("Не удалось получить данные из Meta API.")


async def cmd_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = today_str()
    targets = targets_by_date.get(d)
    if targets is None:
        await update.message.reply_text(
            "Данных о целевых заявках за сегодня ещё нет.\n"
            "Введите /целевые N, чтобы я посчитал конверсию."
        )
        return
    try:
        insights = fetch_insights(date_preset="today")
        leads = insights["leads"]
        conversion = (targets / leads * 100) if leads else 0.0
        await update.message.reply_text(
            f"📈 Конверсия за {d}: {conversion:.0f}%\n"
            f"Целевых: {targets} из {leads} заявок"
        )
    except Exception:
        logger.exception("Ошибка /конверсия")
        await update.message.reply_text("Не удалось получить данные из Meta API.")


# --- Умные ответы через Claude (agentic loop с tool_use) ---

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    logger.info("Текст не распознан как команда, ухожу к Claude: %r", user_text)

    system_prompt = (
        f"Ты аналитик рекламного кабинета Meta Ads {AD_ACCOUNT}.\n"
        f"У тебя есть прямой доступ к Meta Graph API через токен: {META_ACCESS_TOKEN}\n"
        "Когда тебя просят анализ — сам запроси данные через доступные инструменты.\n"
        "Отвечай фактами и цифрами. Без markdown. Без советов — только факты."
    )

    messages = [{"role": "user", "content": user_text}]
    notified_wait = False  # отправили ли уже "подожди 10 секунд"

    for _ in range(10):  # не более 10 итераций агентного цикла
        try:
            response = claude_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system_prompt,
                tools=META_TOOLS,
                messages=messages,
            )
        except Exception:
            logger.exception("Ошибка Claude API")
            await update.message.reply_text("Не получилось обратиться к Claude API. Попробуйте чуть позже.")
            return

        # Claude закончил — отдаём текст пользователю
        if response.stop_reason == "end_turn":
            text_parts = [b.text for b in response.content if hasattr(b, "text")]
            reply = "\n".join(text_parts).strip() or "Нет ответа."
            await update.message.reply_text(reply)
            return

        # Claude хочет вызвать инструмент
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_name = block.name
                tool_input = block.input
                tool_id = block.id
                result_json = None

                for attempt in range(3):
                    try:
                        result_json = execute_meta_tool(tool_name, tool_input)
                        break
                    except Exception as exc:
                        logger.warning("Meta API ошибка (попытка %d/3): %s", attempt + 1, exc)
                        if not notified_wait:
                            await update.message.reply_text("Запрашиваю данные, подожди 10 секунд...")
                            notified_wait = True
                        if attempt < 2:
                            await asyncio.sleep(10)

                if result_json is None:
                    await update.message.reply_text("Проверь токен Meta в настройках.")
                    return

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_json,
                })

            # Добавляем ответ ассистента и результаты инструментов в историю
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        # Неожиданный stop_reason
        logger.warning("Неожиданный stop_reason от Claude: %s", response.stop_reason)
        break

    await update.message.reply_text("Не удалось получить ответ. Попробуйте ещё раз.")


# --- Планировщик ---

def build_morning_report() -> str:
    """Формирует отчёт за вчерашний день: Meta Ads + Bitrix24 CRM."""
    yesterday_date = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")

    # Meta данные
    insights = fetch_insights(date_preset="yesterday")
    ads = fetch_ads_breakdown(date_preset="yesterday")
    spend = insights["spend"]
    meta_leads = insights["leads"]
    cost_per_lead = insights["cost_per_lead"]

    with_leads = [a for a in ads if a["leads"] > 0]
    spenders = [a for a in ads if a["spend"] > 0]
    if with_leads:
        best = min(with_leads, key=lambda a: a["cost_per_lead"])
        best_line = f"{best['name']} — ${best['cost_per_lead']:.2f} за заявку"
    else:
        best_line = "нет объявлений с заявками"

    # Bitrix данные
    try:
        crm_leads = fetch_bitrix_leads(date_preset="yesterday")
        crm_deals = fetch_bitrix_deals(date_preset="yesterday")
        bitrix_total = crm_leads["total"]
        bitrix_closed = crm_deals["closed"]
        conversion = round(bitrix_total / meta_leads * 100) if meta_leads else 0
        cost_per_crm_lead = round(spend / bitrix_total, 2) if bitrix_total else 0
        crm_block = (
            "--- CRM Bitrix ---\n"
            f"Новых лидов: {bitrix_total}\n"
            f"Сделок закрыто: {bitrix_closed}\n"
            f"Конверсия реклама → лид: {conversion}%\n"
            f"Цена целевого лида: ${cost_per_crm_lead}"
        )
    except Exception:
        logger.exception("Bitrix недоступен при формировании отчёта")
        crm_block = "--- CRM Bitrix ---\nДанные недоступны"

    return (
        f"Отчёт Автоквартал — {yesterday_date}\n"
        f"--- Реклама Meta ---\n"
        f"Потрачено: ${spend:.2f}\n"
        f"Заявок из рекламы: {meta_leads}\n"
        f"Цена заявки: ${cost_per_lead:.2f}\n"
        f"{crm_block}\n"
        f"--- Лучшее объявление: {best_line}"
    )


async def cmd_crm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текущие лиды и сделки за сегодня из Bitrix24."""
    try:
        leads = fetch_bitrix_leads(date_preset="today")
        deals = fetch_bitrix_deals(date_preset="today")

        status_lines = "\n".join(f"  {st}: {cnt}" for st, cnt in leads["by_status"].items()) or "  нет данных"
        stage_lines = "\n".join(f"  {st}: {cnt}" for st, cnt in deals["by_stage"].items()) or "  нет данных"

        text = (
            f"CRM Bitrix — {today_str()}\n\n"
            f"Лидов сегодня: {leads['total']}\n"
            f"По статусам:\n{status_lines}\n\n"
            f"Сделок сегодня: {deals['total']}\n"
            f"Закрыто (WON): {deals['closed']}\n"
            f"Сумма сделок: {deals['total_amount']:,.0f}\n"
            f"По стадиям:\n{stage_lines}"
        )
        await update.message.reply_text(text)
    except Exception:
        logger.exception("Ошибка /crm")
        await update.message.reply_text("Не удалось получить данные из Bitrix24. Проверь BITRIX_WEBHOOK.")


async def cmd_funnel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Конверсия по этапам воронки за сегодня."""
    try:
        meta = fetch_insights(date_preset="today")
        leads = fetch_bitrix_leads(date_preset="today")
        deals = fetch_bitrix_deals(date_preset="today")

        meta_leads = meta["leads"]
        crm_leads = leads["total"]
        crm_deals = deals["total"]
        crm_closed = deals["closed"]

        def pct(a, b):
            return f"{round(a / b * 100)}%" if b else "н/д"

        lines = [
            f"Воронка Автоквартал — {today_str()}",
            "",
            f"Клики в рекламе → Заявки Meta:  {pct(meta_leads, meta_leads)} ({meta_leads} шт.)",
            f"Заявки Meta → Лиды в CRM:       {pct(crm_leads, meta_leads)} ({crm_leads} из {meta_leads})",
            f"Лиды → Сделки:                  {pct(crm_deals, crm_leads)} ({crm_deals} из {crm_leads})",
            f"Сделки → Закрыто (WON):         {pct(crm_closed, crm_deals)} ({crm_closed} из {crm_deals})",
            "",
            f"Итог: реклама → продажа         {pct(crm_closed, meta_leads)}",
        ]
        await update.message.reply_text("\n".join(lines))
    except Exception:
        logger.exception("Ошибка /воронка")
        await update.message.reply_text("Не удалось получить данные. Проверь Meta и Bitrix.")


async def send_morning_report(app: Application):
    try:
        text = build_morning_report()
        await app.bot.send_message(chat_id=GROUP_ID, text=text)
        logger.info("Утренний отчёт отправлен")
    except Exception:
        logger.exception("Ошибка отправки утреннего отчёта")


async def cmd_test_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = build_morning_report()
        await context.bot.send_message(chat_id=GROUP_ID, text=text)
        if update.effective_chat.id != GROUP_ID:
            await update.message.reply_text("Отчёт отправлен в группу.")
    except Exception:
        logger.exception("Ошибка /тест_отчёт")
        await update.message.reply_text("Не удалось получить данные из Meta API.")


async def post_init(app: Application):
    scheduler = AsyncIOScheduler(timezone=TZ_NAME)
    scheduler.add_job(send_morning_report, CronTrigger(hour=9, minute=0, timezone=TZ_NAME), args=[app])
    scheduler.add_job(check_alerts, CronTrigger(minute="*/30"), args=[app])
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logger.info("Планировщик: ежедневный отчёт в 09:00 Asia/Almaty, алерты каждые 30 мин")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Telegram распознаёт как команды только латиницу (/[a-zA-Z0-9_]+),
    # поэтому кириллические "команды" обрабатываем как обычный текст по regex.
    app.add_handler(CommandHandler("start", cmd_start))
    def cyrillic_command(word: str) -> filters.Regex:
        return filters.Regex(re.compile(rf"^/?{word}(@\w+)?\b", re.IGNORECASE | re.UNICODE))

    app.add_handler(MessageHandler(cyrillic_command("отч[её]т"), cmd_report))
    app.add_handler(MessageHandler(cyrillic_command("тест_отч[её]т"), cmd_test_report))
    app.add_handler(MessageHandler(cyrillic_command("целевые"), cmd_targets))
    app.add_handler(MessageHandler(cyrillic_command("топ"), cmd_top))
    app.add_handler(MessageHandler(cyrillic_command("флоп"), cmd_flop))
    app.add_handler(MessageHandler(cyrillic_command("бюджет"), cmd_budget))
    app.add_handler(MessageHandler(cyrillic_command("конверсия"), cmd_conversion))
    app.add_handler(MessageHandler(cyrillic_command("crm"), cmd_crm))
    app.add_handler(MessageHandler(cyrillic_command("воронка"), cmd_funnel))
    app.add_handler(MessageHandler(owner_confirmation_filter, handle_owner_confirmation))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запускается...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
