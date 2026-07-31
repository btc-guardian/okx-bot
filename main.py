import os
import time
import asyncio
import logging
import datetime
import zoneinfo
import aiohttp
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = 120  # seconds

TRACKED_CURRENCIES = ["USDT", "BTC", "ETH"]

OKX_STATUS_URL = "https://www.okx.com/api/v5/system/status"
DEFILLAMA_URL = "https://api.llama.fi/protocol/okx"

# Hours in Europe/Warsaw time at which daily status reports are sent.
REPORT_HOURS = {8, 12, 16, 20}
WARSAW_TZ = zoneinfo.ZoneInfo("Europe/Warsaw")

# ── State ─────────────────────────────────────────────────────────────────────

RESERVE_HISTORY: dict[str, float] = {}

# Keys of alerts already sent – prevents re-sending the same alert every 2 min.
SENT_ALERT_KEYS: set[str] = set()

# Track which report hours have already been sent today.
SENT_REPORT_HOURS: set[tuple[datetime.date, int]] = set()

# Titles containing any of these substrings are silently ignored (case-insensitive).
IGNORED_MAINTENANCE_KEYWORDS = ["copy trading"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_critical(alert_key: str) -> bool:
    """Return True for alerts that require the KRYTYCZNY ALERT prefix."""
    return alert_key.startswith(("reserve_drop:", "okx_withdrawal_disabled:"))


def format_alert(alert_key: str, message: str) -> str:
    prefix = "🔴 <b>[KRYTYCZNY ALERT - DZIAŁAJ]</b>\n" if is_critical(alert_key) else ""
    return prefix + message


# ── Telegram ──────────────────────────────────────────────────────────────────

async def send_telegram(
    session: aiohttp.ClientSession,
    text: str,
    disable_notification: bool = False,
) -> None:
    """
    Send a Telegram message.
    - disable_notification=False  → normal delivery with sound (for critical alerts)
    - disable_notification=True   → silent delivery (for status reports)
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set – skipping message.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": disable_notification,
    }
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.error("Telegram error %s: %s", resp.status, body)
            else:
                mode = "silent" if disable_notification else "with sound"
                log.info("Telegram message sent (%s).", mode)
    except Exception as exc:
        log.error("Failed to send Telegram message: %s", exc)


# ── Daily status report ───────────────────────────────────────────────────────

async def maybe_send_status_report(session: aiohttp.ClientSession) -> None:
    """Send a silent status report at configured hours in Europe/Warsaw time (once per slot)."""
    now = datetime.datetime.now(WARSAW_TZ)
    slot = (now.date(), now.hour)
    if now.hour in REPORT_HOURS and slot not in SENT_REPORT_HOURS:
        SENT_REPORT_HOURS.add(slot)
        log.info("Sending scheduled status report for %s (Warsaw time).", slot)
        await send_telegram(
            session,
            "🟢 <b>[STATUS]</b> Bot działa poprawnie. Monitoring OKX i rezerw jest aktywny.",
            disable_notification=True,
        )


# ── OKX system status ─────────────────────────────────────────────────────────

async def check_okx_status(session: aiohttp.ClientSession) -> tuple[str, list[tuple[str, str]]]:
    """
    Query OKX public /api/v5/system/status.
    Returns (summary, list_of_(alert_key, alert_message)).
    Ignores maintenance entries matching IGNORED_MAINTENANCE_KEYWORDS.
    """
    t0 = time.monotonic()
    try:
        async with session.get(OKX_STATUS_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status != 200:
                key = f"okx_api_error:{resp.status}"
                msg = f"🚨 OKX API niedostępne: HTTP {resp.status}"
                return f"⚠️ OKX API error: HTTP {resp.status} (latency {latency_ms} ms)", [(key, msg)]
            data = await resp.json()
    except asyncio.TimeoutError:
        key = "okx_api_timeout"
        return "⚠️ OKX API timeout after 15 s", [(key, "🚨 OKX API timeout")]
    except Exception as exc:
        key = "okx_api_exception"
        return f"⚠️ OKX API request failed: {exc}", [(key, f"🚨 OKX API błąd: {exc}")]

    entries = data.get("data", [])
    DISRUPTION = {"scheduled", "ongoing", "pre_open"}
    active = [e for e in entries if e.get("state") in DISRUPTION]

    active_maintenance_keys: set[str] = set()
    lines = [f"📡 <b>OKX system status</b> (latency: {latency_ms} ms)"]
    alerts: list[tuple[str, str]] = []

    visible_active = []
    for e in active:
        title = e.get("title", "Nieznane zdarzenie")
        if any(kw in title.lower() for kw in IGNORED_MAINTENANCE_KEYWORDS):
            log.debug("Ignoring maintenance: %s", title)
            continue
        visible_active.append(e)

    if not visible_active:
        lines.append("  ✅ Brak aktywnych przerw technicznych")
    else:
        for e in visible_active:
            title = e.get("title", "Nieznane zdarzenie")
            state = e.get("state", "?")
            begin = e.get("begin", "?")
            end = e.get("end", "?")
            lines.append(f"  🔴 [{state.upper()}] {title}  ({begin} → {end})")
            key = f"okx_maintenance:{title}"
            active_maintenance_keys.add(key)
            alerts.append((key, f"🚨 OKX maintenance: {title} (stan: {state})"))

    lines.append("  ℹ️ Status wypłat USDT/BTC/ETH: wymaga klucza API OKX")

    # Remove keys for resolved maintenance so future recurrences alert again.
    stale = {k for k in SENT_ALERT_KEYS if k.startswith("okx_maintenance:")} - active_maintenance_keys
    for k in stale:
        SENT_ALERT_KEYS.discard(k)
        log.info("Cleared resolved maintenance alert key: %s", k)

    return "\n".join(lines), alerts


# ── DefiLlama reserves ─────────────────────────────────────────────────────────

async def check_defillama_reserves(session: aiohttp.ClientSession) -> tuple[str, list[tuple[str, str]]]:
    """
    Fetch OKX on-chain reserves from DefiLlama.
    Alerts (once) if TVL drops more than 3% vs the previous reading.
    """
    try:
        async with session.get(DEFILLAMA_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return f"⚠️ DefiLlama API error: HTTP {resp.status}", []
            data = await resp.json()
    except asyncio.TimeoutError:
        return "⚠️ DefiLlama API timeout after 15 s", []
    except Exception as exc:
        return f"⚠️ DefiLlama request failed: {exc}", []

    tvl_current = data.get("currentChainTvls") or {}
    total_tvl = sum(tvl_current.values()) if tvl_current else data.get("tvl", 0)
    if total_tvl == 0:
        tvl_list = data.get("tvl", [])
        if tvl_list:
            total_tvl = tvl_list[-1].get("totalLiquidityUSD", 0)

    alerts: list[tuple[str, str]] = []
    prev = RESERVE_HISTORY.get("total_tvl")
    direction = ""

    if prev and prev > 0:
        change_pct = (total_tvl - prev) / prev * 100
        direction = f" ({change_pct:+.2f}% vs prev)"
        if change_pct <= -3:
            key = f"reserve_drop:{prev:.0f}:{total_tvl:.0f}"
            msg = (
                f"🚨 OKX rezerwy on-chain spadły o <b>{change_pct:.2f}%</b>\n"
                f"Poprzednio: <b>${prev:,.0f}</b> → Teraz: <b>${total_tvl:,.0f}</b>"
            )
            alerts.append((key, msg))

    RESERVE_HISTORY["total_tvl"] = total_tvl

    summary = (
        f"🏦 <b>OKX on-chain reserves (DefiLlama)</b>\n"
        f"  Total TVL: <b>${total_tvl:,.0f}</b>{direction}"
    )
    return summary, alerts


# ── Keep-alive HTTP server ────────────────────────────────────────────────────

async def handle_ping(request: web.Request) -> web.Response:
    now = datetime.datetime.now(WARSAW_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    return web.Response(
        text=f"OK — OKX Monitor działa | {now}",
        content_type="text/plain",
    )

async def start_http_server() -> None:
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    log.info("Keep-alive HTTP server running on port 8080")


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run_checks(session: aiohttp.ClientSession) -> None:
    okx_summary, okx_alerts = await check_okx_status(session)
    dl_summary, dl_alerts = await check_defillama_reserves(session)

    log.info("\n%s\n%s", okx_summary, dl_summary)

    all_alerts = okx_alerts + dl_alerts
    new_alerts = [(key, msg) for key, msg in all_alerts if key not in SENT_ALERT_KEYS]

    if new_alerts:
        for key, msg in new_alerts:
            formatted = format_alert(key, msg)
            log.warning("NEW ALERT [%s]: %s", key, msg)
            # Critical alerts: disable_notification=False → sound + priority
            await send_telegram(
                session,
                formatted,
                disable_notification=not is_critical(key),
            )
            SENT_ALERT_KEYS.add(key)
    else:
        if all_alerts:
            log.info(
                "Alerts present but already sent – skipping. Keys: %s",
                [k for k, _ in all_alerts],
            )


async def monitor_loop() -> None:
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN is not set!")
    if not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID is not set!")

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        await send_telegram(
            session,
            "👋 <b>OKX Monitor uruchomiony</b>\n"
            "Sprawdzam co 2 minuty:\n"
            "  • Status systemu OKX (przerwy techniczne)\n"
            "  • Rezerwy on-chain via DefiLlama\n\n"
            "Raporty statusowe: 08:00, 12:00, 16:00, 20:00 czasu PL (ciche).\n"
            "Alerty krytyczne wysyłane z dźwiękiem.\n"
            "Keep-alive HTTP server aktywny na porcie 8080.",
            disable_notification=False,
        )
await send_telegram(session, "🚨 TEST KOŃCOWY: Bot działa, serwer żyje, alerty dochodzą!", disable_notification=False)
        while True:
            log.info("Running checks…")
            try:
                await maybe_send_status_report(session)
                await run_checks(session)
            except Exception as exc:
                log.error("Unexpected error during checks: %s", exc)
            log.info("Sleeping %d s…", CHECK_INTERVAL)
            await asyncio.sleep(CHECK_INTERVAL)


async def main() -> None:
    await asyncio.gather(
        start_http_server(),
        monitor_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
