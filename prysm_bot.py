import asyncio
import logging
import re
import os
from datetime import datetime
from playwright.async_api import async_playwright
import httpx

# ============================================================
#  CONFIGURATION — Remplis ces 3 valeurs
# ============================================================
PRYSM_EMAIL    = "urieldegrugillier@gmail.com"
PRYSM_PASSWORD = "Deg2005U!"
TELEGRAM_TOKEN = "8225809582:AAFwsUQRVW-gx4y9QuAETowHqye2-3e76kI"
TELEGRAM_CHAT_ID = "-1003358493754"
INTERVAL_MINUTES = 12   # Intervalle entre chaque scan (en minutes)
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("prysm_bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


async def send_telegram(message: str):
    """Envoie un message dans le groupe Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        })
    if resp.status_code == 200:
        log.info(f"✅ Message Telegram envoyé : {message}")
    else:
        log.error(f"❌ Erreur Telegram : {resp.text}")


def parse_signal(text: str) -> dict | None:
    """
    Extrait les données du bloc signal Prysm.
    Format attendu : XAU/USD HH:MM BUY/SELL Entry X.XX Take Profit X.XX Stop Loss X.XX
    """
    direction = None
    if "BUY" in text.upper():
        direction = "BUY"
    elif "SELL" in text.upper():
        direction = "SELL"
    
    if not direction:
        return None

    entry = re.search(r"Entry\s+([\d,\.]+)", text)
    tp    = re.search(r"Take Profit\s+([\d,\.]+)", text)
    sl    = re.search(r"Stop Loss\s+([\d,\.]+)", text)

    if not (entry and tp and sl):
        return None

    return {
        "direction": direction,
        "entry": entry.group(1).replace(",", ""),
        "tp":    tp.group(1).replace(",", ""),
        "sl":    sl.group(1).replace(",", ""),
    }


def format_telegram_message(signal: dict) -> str:
    """Formate le message dans le format attendu par Social Trade Hub."""
    return (
        f"XAUUSD {signal['direction']}\n"
        f"PE : {signal['entry']}\n"
        f"TP : {signal['tp']}\n"
        f"SL : {signal['sl']}"
    )


async def scan_prysm(page) -> str | None:
    """
    Clique sur View Signals, attend le scan, et retourne le texte du signal
    ou None si aucun signal trouvé.
    """
    log.info("🔍 Scan Prysm en cours...")

    try:
        # Clique sur le bouton View Signals / Scan Market
        btn = page.locator("button", has_text=re.compile(r"View Signals|Scan Market", re.I))
        await btn.first.click()

        # Attend que le scan soit terminé (bouton revient ou signal apparaît)
        # On attend max 60 secondes
        await page.wait_for_timeout(3000)

        # Attend la fin du scanning (disparition du %)
        for _ in range(30):
            content = await page.inner_text("body")
            if "Scanning" not in content:
                break
            await page.wait_for_timeout(2000)

        # Relit le contenu final
        content = await page.inner_text("body")

        # Vérifie si un signal est présent
        if "BUY" in content.upper() or "SELL" in content.upper():
            return content
        else:
            log.info(f"ℹ️ Pas de signal : {content[200:350].strip()}")
            return None

    except Exception as e:
        log.error(f"❌ Erreur pendant le scan : {e}")
        return None


async def login_prysm(page):
    """Se connecte à Prysm Intelligence."""
    log.info("🔐 Connexion à Prysm...")
    await page.goto("https://prysmintelligence.app/auth")
    await page.wait_for_timeout(2000)

    await page.fill("input[type='email']", PRYSM_EMAIL)
    await page.fill("input[type='password']", PRYSM_PASSWORD)
    await page.click("button[type='submit']")
    await page.wait_for_timeout(3000)

    if "auth" not in page.url:
        log.info("✅ Connecté à Prysm")
    else:
        log.warning("⚠️ Connexion peut-être échouée, vérifie tes identifiants")


async def main():
    log.info("🚀 Démarrage du bot Prysm → Telegram")
    await send_telegram("🤖 <b>Bot Prysm démarré</b>\nScan toutes les " + str(INTERVAL_MINUTES) + " minutes.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        await login_prysm(page)
        await page.goto("https://prysmintelligence.app/")
        await page.wait_for_timeout(2000)

        last_signal = None  # Evite d'envoyer le même signal deux fois

        while True:
            try:
                raw = await scan_prysm(page)

                if raw:
                    signal = parse_signal(raw)
                    if signal:
                        msg = format_telegram_message(signal)
                        # Evite les doublons consécutifs
                        if msg != last_signal:
                            await send_telegram(msg)
                            last_signal = msg
                        else:
                            log.info("⏭️ Signal identique au précédent, ignoré")
                    else:
                        log.warning("⚠️ Signal détecté mais parsing échoué")

            except Exception as e:
                log.error(f"❌ Erreur dans la boucle principale : {e}")
                # Tente de recharger la page en cas de problème
                try:
                    await page.reload()
                    await page.wait_for_timeout(3000)
                except:
                    pass

            log.info(f"⏳ Prochain scan dans {INTERVAL_MINUTES} minutes...")
            await asyncio.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    asyncio.run(main())
