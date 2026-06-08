import os
import re
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

TZ = timezone(timedelta(hours=5))  # UTC+5 (Алматы/Астана)

GRAPH_API_URL = "https://graph.facebook.com/v19.0"

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Целевые заявки храним в памяти: {"YYYY-MM-DD": int}
targets_by_date: dict[str, int] = {}


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
        "fields": "ad_name,spend,actions",
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
            "name": row.get("ad_name", "Без названия"),
            "spend": spend,
            "leads": leads,
            "cost_per_lead": cost_per_lead,
        })
    return ads


def fetch_month_spend() -> float:
    insights = fetch_insights(date_preset="this_month")
    return insights["spend"]


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
            worst_line = f"{worst['name']} — {worst_value} (рекомендую отключить)"
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


def build_meta_context(date_preset: str, label: str) -> str:
    """Собирает свежую сводку из Meta API за нужный период для передачи в Claude."""
    insights = fetch_insights(date_preset=date_preset)
    ads = fetch_ads_breakdown(date_preset=date_preset)
    d = today_str()
    targets = targets_by_date.get(d)

    lines = [
        f"Период: {label}.",
        f"Потрачено: ${insights['spend']:.2f}.",
        f"Заявок из рекламы (WhatsApp): {insights['leads']} (цена за заявку: ${insights['cost_per_lead']:.2f}).",
    ]

    with_leads = [a for a in ads if a["leads"] > 0]
    if with_leads:
        best = min(with_leads, key=lambda a: a["cost_per_lead"])
        lines.append(
            f"Лучшее объявление: «{best['name']}» — ${best['cost_per_lead']:.2f}/заявка, {best['leads']} заявок."
        )
    spenders = [a for a in ads if a["spend"] > 0]
    if spenders:
        worst = max(spenders, key=lambda a: (a["cost_per_lead"] or float("inf")))
        worst_val = f"${worst['cost_per_lead']:.2f}/заявка" if worst["cost_per_lead"] else "потратило без единой заявки"
        lines.append(f"Худшее объявление: «{worst['name']}» — {worst_val}, потрачено ${worst['spend']:.2f}.")

    lines.append(
        "Целевых заявок за сегодня по CRM: "
        + (str(targets) if targets is not None else "клиент ещё не передал — можно попросить через /целевые N")
        + "."
    )
    return "\n".join(lines)


# --- Алерты ---

async def check_alerts(app: Application):
    try:
        insights = fetch_insights(date_preset="today")
        ads = fetch_ads_breakdown()
        d = today_str()

        alerts = []

        if insights["leads"] and insights["cost_per_lead"] > 800:
            alerts.append(f"⚠️ Цена заявки выросла: ${insights['cost_per_lead']:.2f} (выше $800)")

        for ad in ads:
            if ad["leads"] == 0 and ad["spend"] > 2000:
                alerts.append(f"⚠️ Объявление «{ad['name']}» потратило ${ad['spend']:.2f} без единой заявки — рекомендую отключить")

        targets = targets_by_date.get(d)
        if targets is not None and insights["leads"]:
            conversion = targets / insights["leads"] * 100
            if conversion < 15:
                alerts.append(f"⚠️ Конверсия упала до {conversion:.0f}% (ниже 15%)")

        for alert in alerts:
            await app.bot.send_message(chat_id=MY_ID, text=alert)
    except Exception:
        logger.exception("Ошибка при проверке алертов")


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


# --- Умные ответы через Claude ---

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    logger.info("Текст не распознан как команда, ухожу к Claude: %r", user_text)

    date_preset, label = detect_period(user_text)
    try:
        meta_context = build_meta_context(date_preset, label)
    except Exception:
        logger.exception("Не удалось получить данные Meta API для контекста Claude")
        meta_context = (
            f"Период: {label}. Не получилось обратиться к Meta API прямо сейчас "
            "(возможно, временная сетевая ошибка) — предупреди об этом и предложи повторить запрос через минуту."
        )

    system_prompt = (
        "Ты опытный таргетолог, который ведёт рекламу в Meta (Facebook/Instagram) для жилого комплекса "
        "«Автоквартал» (реклама ведёт на WhatsApp, целевая заявка — реальный покупатель по данным CRM клиента). "
        "Анализируй данные из Meta Ads и давай конкретные советы. "
        "Отвечай на русском, коротко и по делу. Не говори что данных нет — иди и получи их сам."
    )

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": f"Свежие данные из Meta Ads:\n{meta_context}\n\nВопрос пользователя: {user_text}",
                }
            ],
        )
        reply = message.content[0].text
    except Exception:
        logger.exception("Ошибка обращения к Claude API")
        reply = "Не получилось обратиться к Claude API. Попробуйте чуть позже."

    await update.message.reply_text(reply)


# --- Планировщик ---

async def scheduled_daily_report(app: Application):
    try:
        report = await build_daily_report()
        await app.bot.send_message(chat_id=GROUP_ID, text=report)
    except Exception:
        logger.exception("Ошибка отправки ежедневного отчёта")


async def post_init(app: Application):
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(scheduled_daily_report, CronTrigger(hour=20, minute=0), args=[app])
    scheduler.add_job(check_alerts, CronTrigger(minute="*/30"), args=[app])
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logger.info("Планировщик запущен: ежедневный отчёт в 20:00 (UTC+5), проверка алертов каждые 30 минут")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Telegram распознаёт как команды только латиницу (/[a-zA-Z0-9_]+),
    # поэтому кириллические "команды" обрабатываем как обычный текст по regex.
    app.add_handler(CommandHandler("start", cmd_start))
    def cyrillic_command(word: str) -> filters.Regex:
        return filters.Regex(re.compile(rf"^/?{word}(@\w+)?\b", re.IGNORECASE | re.UNICODE))

    app.add_handler(MessageHandler(cyrillic_command("отч[её]т"), cmd_report))
    app.add_handler(MessageHandler(cyrillic_command("целевые"), cmd_targets))
    app.add_handler(MessageHandler(cyrillic_command("топ"), cmd_top))
    app.add_handler(MessageHandler(cyrillic_command("флоп"), cmd_flop))
    app.add_handler(MessageHandler(cyrillic_command("бюджет"), cmd_budget))
    app.add_handler(MessageHandler(cyrillic_command("конверсия"), cmd_conversion))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запускается...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
