import asyncio
import logging
import re
from playwright.async_api import async_playwright
import httpx

# ============================================================
#  CONFIGURATION
# ============================================================
PRYSM_EMAIL      = "urieldegrugillier@gmail.com"
PRYSM_PASSWORD   = "Deg2005U!"
TELEGRAM_TOKEN   = "8225809582:AAFwsUQRVW-gx4y9QuAETowHqye2-3e76kI"
TELEGRAM_CHAT_ID = "-1003358493754"
INTERVAL_MINUTES = 4
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
    return (
        f"XAUUSD {signal['direction']}\n"
        f"PE : {signal['entry']}\n"
        f"TP : {signal['tp']}\n"
        f"SL : {signal['sl']}"
    )


async def scan_prysm(page) -> str | None:
    log.info("🔍 Scan Prysm en cours...")
    try:
        btn = page.locator("button", has_text=re.compile(r"View Signals|Scan Market", re.I))
        await btn.first.click()
        await page.wait_for_timeout(3000)

        for _ in range(30):
            content = await page.inner_text("body")
            if "Scanning" not in content:
                break
            await page.wait_for_timeout(2000)

        content = await page.inner_text("body")

        if "BUY" in content.upper() or "SELL" in content.upper():
            return content
        else:
            log.info(f"ℹ️ Pas de signal : {content[200:400].strip()}")
            return None

    except Exception as e:
        log.error(f"❌ Erreur pendant le scan : {e}")
        return None


async def login_prysm(page):
    """Se connecte à Prysm : page d'accueil → Connect → Sign In → email/mdp"""
    log.info("🔐 Connexion à Prysm...")

    # Étape 1 : page d'accueil
    await page.goto("https://prysmintelligence.app/")
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(2000)

    # Étape 2 : cliquer sur le bouton "Connect" en haut à droite
    try:
        connect_btn = page.locator("a, button", has_text=re.compile(r"Connect", re.I))
        await connect_btn.first.click()
        log.info("✅ Bouton Connect cliqué")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(2000)
    except Exception as e:
        log.error(f"❌ Bouton Connect introuvable : {e}")

    log.info(f"📄 URL après Connect : {page.url}")

    # Étape 3 : cliquer sur Sign In si présent
    try:
        signin_btn = page.locator("button, a", has_text=re.compile(r"Sign[- ]?[Ii]n|Login", re.I))
        count = await signin_btn.count()
        if count > 0:
            await signin_btn.first.click()
            log.info("✅ Bouton Sign In cliqué")
            await page.wait_for_timeout(2000)
    except Exception as e:
        log.warning(f"⚠️ Pas de bouton Sign In : {e}")

    # Étape 4 : remplir email et mot de passe
    try:
        await page.fill("input[type='email']", PRYSM_EMAIL, timeout=10000)
        log.info("✅ Email rempli")
    except Exception as e:
        log.error(f"❌ Champ email introuvable : {e}")

    try:
        await page.fill("input[type='password']", PRYSM_PASSWORD, timeout=10000)
        log.info("✅ Password rempli")
    except Exception as e:
        log.error(f"❌ Champ password introuvable : {e}")

    # Étape 5 : soumettre
    try:
        submit = page.locator("button[type='submit']")
        count = await submit.count()
        if count > 0:
            await submit.first.click()
            log.info("✅ Bouton submit cliqué")
        else:
            await page.keyboard.press("Enter")
            log.info("✅ Enter pressé")
    except Exception as e:
        log.error(f"❌ Erreur soumission : {e}")

    await page.wait_for_timeout(4000)
    log.info(f"📄 URL après login : {page.url}")

    if "auth" not in page.url and "login" not in page.url:
        log.info("✅ Connecté à Prysm avec succès !")
    else:
        log.warning("⚠️ Toujours sur la page auth — vérifie email/mot de passe dans le script")


async def main():
    log.info("🚀 Démarrage du bot Prysm → Telegram")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        await login_prysm(page)
        await page.goto("https://prysmintelligence.app/")
        await page.wait_for_timeout(2000)

        last_signal = None

        while True:
            try:
                raw = await scan_prysm(page)

                if raw:
                    signal = parse_signal(raw)
                    if signal:
                        msg = format_telegram_message(signal)
                        if msg != last_signal:
                            await send_telegram(msg)
                            last_signal = msg
                        else:
                            log.info("⏭️ Signal identique au précédent, ignoré")
                    else:
                        log.warning("⚠️ Signal détecté mais parsing échoué")

            except Exception as e:
                log.error(f"❌ Erreur dans la boucle principale : {e}")
                try:
                    await page.reload()
                    await page.wait_for_timeout(3000)
                except:
                    pass

            log.info(f"⏳ Prochain scan dans {INTERVAL_MINUTES} minutes...")
            await asyncio.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    asyncio.run(main())
