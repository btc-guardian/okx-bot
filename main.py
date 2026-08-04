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

# --- State --------------------------------------------------------------------

RESERVE_HISTORY: dict[str, float] = {}

# Keys of alerts already sent - prevents re-sending the same alert every 2 min.
SENT_ALERT_KEYS: set[str] = set()

# Track which report hours have already been sent today.
SENT_REPORT_HOURS: set[tuple[datetime.date, int]] = set()

# Titles containing any of these substrings are silently ignored (case-insensitive).
IGNORE_TITLES = []


async def send_telegram(text: str, silent: bool = True) -> bool:
    """Send a message to Telegram. Defaults to silent notifications."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram token or chat ID not set. Message skipped.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": silent,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error(f"Telegram API error {resp.status}: {body}")
                    return False
                return True
    except Exception as e:
        log.error(f"Failed to send Telegram message: {e}")
        return False


async def check_okx_status(session: aiohttp.ClientSession) -> list[str]:
    """Fetch system status from OKX API and return alert messages for ongoing/scheduled maintenance."""
    alerts = []
    try:
        async with session.get(OKX_STATUS_URL, timeout=10) as resp:
            if resp.status != 200:
                log.error(f"OKX status API returned HTTP {resp.status}")
                key = f"okx_http_{resp.status}"
                if key not in SENT_ALERT_KEYS:
                    alerts.append(f"🚨 OKX API niedostępne: HTTP {resp.status}")
                    SENT_ALERT_KEYS.add(key)
                return alerts

            data = await resp.json()
            if data.get("code") != "0":
                log.error(f"OKX status API error code: {data.get('code')}")
                return alerts

            items = data.get("data", [])
            current_keys = set()

            for item in items:
                title = item.get("title", "")
                state = item.get("state", "")  # 'scheduled', 'ongoing', 'completed'
                service_type = item.get("serviceType", "")
                sched_beg = item.get("schedBeg", "")
                sched_end = item.get("schedEnd", "")

                # Ignore titles on the blacklist
                if any(ign.lower() in title.lower() for ign in IGNORE_TITLES):
                    continue

                # Only alert on 'ongoing' or 'scheduled'
                if state in ("ongoing", "scheduled"):
                    alert_key = f"okx_maint_{title}_{sched_beg}"
                    current_keys.add(alert_key)

                    if alert_key not in SENT_ALERT_KEYS:
                        state_label = "TRWA" if state == "ongoing" else "zaplanowano"
                        msg = (
                            f"🚨 <b>OKX maintenance: {title}</b> (stan: {state})\n"
                            f"Usługa: {service_type}\n"
                            f"Czas: {sched_beg} - {sched_end}"
                        )
                        alerts.append(msg)
                        SENT_ALERT_KEYS.add(alert_key)

            # Cleanup resolved maintenance keys
            keys_to_remove = {k for k in SENT_ALERT_KEYS if k.startswith("okx_maint_") and k not in current_keys}
            SENT_ALERT_KEYS.difference_update(keys_to_remove)

    except Exception as e:
        log.error(f"Error checking OKX status: {e}")

    return alerts


async def check_defillama_reserves(session: aiohttp.ClientSession) -> list[str]:
    """Fetch OKX reserve data from DefiLlama and check for significant drops (>5%)."""
    alerts = []
    try:
        async with session.get(DEFILLAMA_URL, timeout=15) as resp:
            if resp.status != 200:
                log.error(f"DefiLlama API returned HTTP {resp.status}")
                return alerts

            data = await resp.json()
            # Extract latest token balances from chainData / current reserves
            tokens = data.get("currentChainBalances", {})
            # Also check tokens in 'chainTvls' or 'tokens' if available
            # DefiLlama protocol endpoint structure:
            # data['tokens'] -> array of token objects or data['currentChainBalances']
            # We look for token USD values or quantities.

            # Alternative: parse 'tokens' array if present
            token_data = data.get("tokens", [])
            current_balances: dict[str, float] = {}

            if isinstance(token_data, list):
                for entry in token_data:
                    # Look for the latest date entry
                    date_tokens = entry.get("tokens", {})
                    for symbol, amount in date_tokens.items():
                        symbol_upper = symbol.upper()
                        if symbol_upper in TRACKED_CURRENCIES:
                            current_balances[symbol_upper] = float(amount)

            # Fallback: check currentChainBalances
            if not current_balances and isinstance(tokens, dict):
                for chain, chain_tokens in tokens.items():
                    if isinstance(chain_tokens, dict):
                        for symbol, amount in chain_tokens.items():
                            symbol_upper = symbol.upper()
                            if symbol_upper in TRACKED_CURRENCIES:
                                current_balances[symbol_upper] = (
                                    current_balances.get(symbol_upper, 0.0) + float(amount)
                                )

            # Compare with history
            for symbol, current_val in current_balances.items():
                if symbol in RESERVE_HISTORY:
                    prev_val = RESERVE_HISTORY[symbol]
                    if prev_val > 0:
                        pct_change = ((current_val - prev_val) / prev_val) * 100
                        if pct_change <= -5.0:
                            alert_key = f"reserve_drop_{symbol}_{datetime.datetime.now().strftime('%Y%m%d_%H')}"
                            if alert_key not in SENT_ALERT_KEYS:
                                msg = (
                                    f"⚠️ <b>ALERT REZERW: Spadek {symbol} o {abs(pct_change):.1f}%!</b>\n"
                                    f"Poprzednio: {prev_val:,.2f} -> Teraz: {current_val:,.2f}"
                                )
                                alerts.append(msg)
                                SENT_ALERT_KEYS.add(alert_key)

                RESERVE_HISTORY[symbol] = current_val

    except Exception as e:
        log.error(f"Error checking DefiLlama reserves: {e}")

    return alerts


async def monitor_loop():
    """Main monitoring loop that runs every CHECK_INTERVAL seconds."""
    log.info("Starting OKX & Reserve monitoring loop...")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now_warsaw = datetime.datetime.now(WARSAW_TZ)

                # 1. Check OKX system status
                okx_alerts = await check_okx_status(session)
                for alert in okx_alerts:
                    # Alerty czerwone 🚨 wyczyszczone z silent=False -> GŁOŚNO!
                    await send_telegram(alert, silent=False)

                # 2. Check DefiLlama reserves
                llama_alerts = await check_defillama_reserves(session)
                for alert in llama_alerts:
                    # Alerty rezerw ⚠️ -> GŁOŚNO!
                    await send_telegram(alert, silent=False)

                # 3. Scheduled status report (08:00, 12:00, 16:00, 20:00 Warsaw time)
                today = now_warsaw.date()
                current_hour = now_warsaw.hour

                if current_hour in REPORT_HOURS:
                    report_key = (today, current_hour)
                    if report_key not in SENT_REPORT_HOURS:
                        report_msg = (
                            f"🟢 <b>[STATUS] Bot działa poprawnie.</b>\n"
                            f"Monitoring OKX i rezerw jest aktywny."
                        )
                        # Raport zielony 🟢 z silent=True -> CICHO!
                        await send_telegram(report_msg, silent=True)
                        SENT_REPORT_HOURS.add(report_key)

                        # Clean up old report keys from previous days
                        old_keys = {k for k in SENT_REPORT_HOURS if k[0] < today}
                        SENT_REPORT_HOURS.difference_update(old_keys)

            except Exception as e:
                log.error(f"Unexpected error in monitor loop: {e}")

            await asyncio.sleep(CHECK_INTERVAL)


# --- Web Server for Keep-Alive ------------------------------------------------

async def handle_root(request):
    return web.Response(text="OKX Monitor Bot is running.")


async def handle_health(request):
    return web.json_response({"status": "ok", "timestamp": time.time()})


def create_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    return app


async def main():
    # Send startup message
    startup_msg = (
        "👋 <b>OKX Monitor uruchomiony</b>\n"
        "Sprawdzam co 2 minuty:\n"
        "• Status systemu OKX (przerwy techniczne)\n"
        "• Rezerwy on-chain via DefiLlama\n\n"
        "Raporty statusowe: 08:00, 12:00, 16:00, 20:00 czasu PL (ciche).\n"
        "Alerty krytyczne wysyłane z dźwiękiem."
    )
    # Startowa wiadomość wysyłana cicho
    await send_telegram(startup_msg, silent=True)

    # Start the web server (Render binds to PORT env var)
    port = int(os.environ.get("PORT", 8080))
    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Web server started on port {port}")

    # Start the monitoring task
    await monitor_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
