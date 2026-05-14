#!/usr/bin/env python3
"""
Prysm Bot — Automatisation des signaux de trading XAU/USD
Se connecte à prysmintelligence.app, demande un signal toutes les 5 min,
et l'envoie dans un groupe Telegram si le signal est nouveau et récent.
"""

import asyncio
import logging
import math
import os
import re
import sys
import time

import httpx
from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright

# ============================================================
# CONFIGURATION — surchargeables via variables d'environnement Railway
# ============================================================
EMAIL          = os.environ.get("EMAIL",          "urieldegrugillier@gmail.com")
PASSWORD       = os.environ.get("PASSWORD",       "Deg2005U!")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8225809582:AAFwsUQRVW-gx4y9QuAETowHqye2-3e76kI")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT",  "-1003358493754")
SITE_URL       = "https://prysmintelligence.app/"

WAIT_AFTER_SIGNAL = 15 * 60  # Secondes d'attente après un signal envoyé
SCAN_INTERVAL     = 15       # Secondes entre chaque scan de page
MAX_SCAN_TIME     = 2 * 60   # Durée max de scan après Run analysis


def charger_presets() -> list[dict]:
    """
    Lit la variable d'environnement PRESETS et retourne une liste de dicts.

    Format attendu : "XAU/USD,Scalping,5|XAU/USD,Swing,30"
    Chaque champ : asset,strategy,intervalle_minutes (séparés par virgule).
    Presets séparés par '|'.

    Si PRESETS n'est pas défini, retourne le preset par défaut Scalping 5 min.
    """
    raw = os.environ.get("PRESETS", "")
    if not raw.strip():
        return [{"asset": "XAU/USD", "strategy": "Scalping", "intervalle_min": 5}]

    presets = []
    for bloc in raw.split("|"):
        parties = bloc.strip().split(",")
        if len(parties) != 3:
            log.warning(f"⚠️ Preset ignoré (format invalide) : '{bloc}'")
            continue
        asset, strategy, intervalle = parties
        try:
            presets.append({
                "asset":         asset.strip(),
                "strategy":      strategy.strip(),
                "intervalle_min": int(intervalle.strip()),
            })
        except ValueError:
            log.warning(f"⚠️ Intervalle non entier ignoré dans le preset : '{bloc}'")

    if not presets:
        log.warning("⚠️ Aucun preset valide trouvé dans PRESETS — utilisation du défaut")
        return [{"asset": "XAU/USD", "strategy": "Scalping", "intervalle_min": 5}]

    return presets

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("prysm_bot")


# ============================================================
# ARRONDI SELON LA DIRECTION DU SIGNAL
# ============================================================

def arrondir_prix(direction: str, valeur: float) -> float:
    """
    Arrondit un prix au quart d'entier (0.25) le plus proche selon la direction.
    S'applique identiquement au TP et au SL.
    - BUY  → floor au 0.25 : math.floor(valeur * 4) / 4
              ex: 4667.43 → 4667.00 / 4656.76 → 4656.50 / 4679.57 → 4679.25
    - SELL → ceil  au 0.25 : math.ceil(valeur * 4) / 4
              ex: 4667.43 → 4667.75 / 4656.76 → 4657.00 / 4679.57 → 4680.00
    """
    if direction == "BUY":
        return math.floor(valeur * 4) / 4
    else:
        return math.ceil(valeur * 4) / 4


def fmt_num(n) -> str:
    """Affiche un entier sans .0 (ex: 4802.0 → '4802'), sinon le float tel quel."""
    if isinstance(n, float) and n == int(n):
        return str(int(n))
    return str(n)


# ============================================================
# FORMATAGE ET ENVOI TELEGRAM
# ============================================================

def formater_signal(asset: str, direction: str, entry: float, tp: float, sl: float) -> str:
    """
    Construit le message Telegram à partir des données brutes du signal.
    Le préfixe dépend de l'asset : XAU/USD → XAUUSD, BTC/USD → BTC/USD, US100 → US100.
    """
    prefixes = {"XAU/USD": "XAUUSD", "BTC/USD": "BTC/USD", "US100": "US100"}
    prefixe = prefixes.get(asset, asset)

    tp_fmt = arrondir_prix(direction, tp)
    sl_fmt = arrondir_prix(direction, sl)
    return (
        f"{prefixe} {direction}\n"
        f"PE : {entry}\n"
        f"TP : {fmt_num(tp_fmt)}\n"
        f"SL : {fmt_num(sl_fmt)}"
    )


async def envoyer_telegram(message: str) -> bool:
    """Envoie un message texte dans le groupe Telegram via l'API sendMessage."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json={"chat_id": TELEGRAM_CHAT, "text": message})
            resp.raise_for_status()
            log.info("✅ Message envoyé sur Telegram")
            return True
    except Exception as e:
        log.error(f"❌ Erreur Telegram : {e}")
        return False


# ============================================================
# DÉTECTION DE FRAÎCHEUR ET PARSING DES CARTES SIGNAL
# ============================================================

def est_recent(temps_str: str) -> bool:
    """
    Retourne True si l'indication temporelle correspond à un signal émis
    il y a moins de 2 minutes (ex: '45 seconds ago', '1 min ago', '2 min ago').
    """
    if not temps_str:
        return False
    s = temps_str.strip().lower()

    # Accepter toute mention de secondes
    if re.search(r"\d+\s*sec", s):
        return True

    # Accepter jusqu'à 2 minutes
    m = re.search(r"(\d+)\s*min", s)
    if m and int(m.group(1)) <= 2:
        return True

    return False


def parser_signal(texte: str) -> dict | None:
    """
    Extrait direction, entry, tp, sl et indication temporelle du texte brut
    d'une carte signal. Retourne un dict ou None si le parsing échoue.
    """
    txt = texte.upper()

    # Direction : SELL avant BUY pour éviter "BUY" dans "XAUUSD BUY/SELL" mix
    if "SELL" in txt:
        direction = "SELL"
    elif "BUY" in txt:
        direction = "BUY"
    else:
        return None

    def extraire(label: str) -> float | None:
        """Cherche un nombre flottant après un label donné."""
        pattern = rf"{label}[\s\n:]*([0-9][0-9,]*\.?[0-9]*)"
        match = re.search(pattern, texte, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                return None
        return None

    entry = extraire("Entry")
    tp    = extraire("TP") or extraire("Take Profit")
    sl    = extraire("SL") or extraire("Stop Loss")

    if entry is None or tp is None or sl is None:
        return None

    # Indication temporelle (ex: "30 seconds ago", "1 min ago", "2h ago")
    m_temps = re.search(
        r"(\d+\s*(?:second|sec|min|hour|hr|day|week|month|mo)s?\s*ago)",
        texte,
        re.IGNORECASE,
    )
    temps_str = m_temps.group(1) if m_temps else ""

    return {
        "direction": direction,
        "entry":     entry,
        "tp":        tp,
        "sl":        sl,
        "temps":     temps_str,
    }


# ============================================================
# EXTRACTION DES CARTES VIA JAVASCRIPT
# ============================================================

# Script JS injecté dans la page pour récupérer le texte de chaque carte signal.
# On cherche les éléments DOM qui contiennent XAUUSD + BUY/SELL + Entry + TP + SL
# sans qu'aucun de leurs enfants directs ne soit lui-même une telle carte (évite les doublons).
_JS_EXTRAIRE_CARTES = """
() => {
    const resultats = [];
    const elements  = Array.from(document.querySelectorAll('*'));

    for (const el of elements) {
        const txt = (el.innerText || el.textContent || '').trim();

        if (txt.length < 20 || txt.length > 3000) continue;
        if (!txt.includes('XAUUSD'))              continue;
        if (!txt.includes('BUY') && !txt.includes('SELL')) continue;
        if (!/entry/i.test(txt))                  continue;
        if (!/\bTP\b|Take Profit/i.test(txt))     continue;
        if (!/\bSL\b|Stop Loss/i.test(txt))       continue;

        // Vérifier qu'aucun enfant direct n'est lui-même une carte (évite les conteneurs)
        const enfantEstCarte = Array.from(el.children).some(c => {
            const ct = (c.innerText || c.textContent || '').trim();
            return ct.includes('XAUUSD')
                && (ct.includes('BUY') || ct.includes('SELL'))
                && /entry/i.test(ct);
        });

        if (!enfantEstCarte) {
            resultats.push(txt);
        }
    }

    // Dédupliquer avant de retourner
    return [...new Set(resultats)];
}
"""


async def extraire_cartes_page(page: Page) -> list[str]:
    """Lance le script JS et retourne la liste des textes de cartes signal."""
    try:
        return await page.evaluate(_JS_EXTRAIRE_CARTES)
    except Exception as e:
        log.error(f"❌ Erreur extraction cartes DOM : {e}")
        return []


# ============================================================
# DÉTECTION D'ERREUR SUR LA PAGE
# ============================================================

async def page_en_erreur(page: Page) -> bool:
    """
    Retourne True si la page affiche un message d'erreur connu
    (ex: 'Agent Crash', modale d'erreur, etc.).
    """
    try:
        # Chercher des textes d'erreur courants dans des éléments visibles
        patterns = [
            re.compile(r"agent\s*crash", re.I),
            re.compile(r"analysis\s*failed", re.I),
            re.compile(r"something\s*went\s*wrong", re.I),
        ]
        for pattern in patterns:
            candidats = page.get_by_text(pattern)
            if await candidats.count() > 0:
                try:
                    if await candidats.first.is_visible(timeout=1500):
                        log.warning(f"⚠️ Erreur détectée : '{pattern.pattern}'")
                        return True
                except PWTimeout:
                    pass
        return False
    except Exception:
        return False


# ============================================================
# GESTION DE LA SESSION (CONNEXION / RECONNEXION)
# ============================================================

async def est_connecte(page: Page) -> bool:
    """
    Vérifie si la session est active en cherchant le bouton 'Request signal'.
    Si le bouton 'Sign in' est visible à la place, la session est expirée.
    """
    try:
        sign_in = page.get_by_role("button", name=re.compile(r"^sign\s*in$", re.I))
        if await sign_in.count() > 0 and await sign_in.first.is_visible(timeout=2500):
            return False
        # Vérifier la présence du bouton principal de l'app
        btn = page.locator("button", has_text=re.compile(r"request signal", re.I))
        return await btn.count() > 0
    except Exception:
        return False


async def se_connecter(page: Page) -> bool:
    """
    Navigue vers la page d'accueil, clique sur 'Sign in', remplit le formulaire
    email / mot de passe et valide. Retourne True si la connexion réussit.
    """
    log.info("🔐 Connexion au site Prysm Intelligence...")
    try:
        await page.goto(SITE_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        # Cliquer sur le bouton "Sign in" de la barre de navigation
        nav_btn = page.locator("button, a", has_text=re.compile(r"^sign\s*in$", re.I)).first
        await nav_btn.click(timeout=10_000)
        await page.wait_for_timeout(1500)

        # Remplir le champ email
        email_input = page.locator(
            'input[type="email"], input[name="email"], input[placeholder*="email" i]'
        ).first
        await email_input.fill(EMAIL)

        # Remplir le champ mot de passe
        pwd_input = page.locator('input[type="password"], input[name="password"]').first
        await pwd_input.fill(PASSWORD)

        # Soumettre le formulaire (bouton submit ou bouton "Sign In" du formulaire)
        submit = page.locator(
            "button[type='submit'], button",
            has_text=re.compile(r"sign\s*in", re.I),
        ).last
        await submit.click(timeout=10_000)

        # Attendre le chargement du tableau de bord
        await page.wait_for_timeout(4000)

        if await est_connecte(page):
            log.info("✅ Connexion réussie")
            return True

        log.error("❌ Connexion échouée : bouton 'Request signal' introuvable après login")
        return False

    except Exception as e:
        log.error(f"❌ Exception lors de la connexion : {e}")
        return False


# ============================================================
# DEMANDE DE SIGNAL
# ============================================================

async def demander_signal(page: Page, asset: str, strategy: str) -> bool:
    """
    Ouvre la modal 'Request signal', sélectionne l'asset et la strategy demandés,
    puis clique sur 'Run analysis'. Retourne True si l'action a réussi.

    - asset    : texte exact de la carte à cliquer (ex: "XAU/USD", "BTC/USD", "US100")
    - strategy : texte exact de la carte à cliquer (ex: "Scalping", "Intraday", "Swing")
    """
    log.info(f"📤 Demande de signal — {asset} / {strategy}")
    try:
        # Ouvrir la modal en cliquant sur "Request signal"
        btn_req = page.locator("button", has_text=re.compile(r"request signal", re.I)).first
        await btn_req.click(timeout=10_000)
        await page.wait_for_timeout(1500)

        # Sélectionner la carte asset (texte exact)
        try:
            carte_asset = page.locator(
                "button, [role='option'], [role='radio'], label, div",
                has_text=re.compile(rf"^{re.escape(asset)}$", re.I),
            ).first
            if await carte_asset.is_visible(timeout=3000):
                await carte_asset.click()
                log.info(f"   ✔ Asset sélectionné : {asset}")
        except PWTimeout:
            log.warning(f"   ⚠️ Carte asset '{asset}' non trouvée (peut-être déjà sélectionnée)")

        await page.wait_for_timeout(300)

        # Sélectionner la carte strategy (texte exact)
        try:
            carte_strat = page.locator(
                "button, [role='option'], [role='radio'], label, div",
                has_text=re.compile(rf"^{re.escape(strategy)}$", re.I),
            ).first
            if await carte_strat.is_visible(timeout=3000):
                await carte_strat.click()
                log.info(f"   ✔ Strategy sélectionnée : {strategy}")
        except PWTimeout:
            log.warning(f"   ⚠️ Carte strategy '{strategy}' non trouvée (peut-être déjà sélectionnée)")

        await page.wait_for_timeout(500)

        # Lancer l'analyse
        run_btn = page.locator("button", has_text=re.compile(r"run analysis", re.I)).first
        await run_btn.click(timeout=10_000)
        log.info("▶️  Analyse lancée")
        return True

    except Exception as e:
        log.error(f"❌ Erreur lors de la demande de signal : {e}")
        return False


# ============================================================
# SCAN DE LA PAGE POUR TROUVER UN SIGNAL RÉCENT
# ============================================================

async def scanner_signal_recent(page: Page) -> dict | None:
    """
    Scanne la page toutes les SCAN_INTERVAL secondes pendant MAX_SCAN_TIME secondes
    pour détecter un signal récent (≤ 2 min). Retourne le signal ou None.
    """
    log.info(f"🔍 Scan en cours ({MAX_SCAN_TIME // 60} min max, toutes les {SCAN_INTERVAL}s)...")
    elapsed = 0

    while elapsed < MAX_SCAN_TIME:

        # Vérifier d'abord si la page signale une erreur
        if await page_en_erreur(page):
            log.warning("⚠️ Erreur agent détectée — scan annulé")
            return None

        # Extraire toutes les cartes de signal présentes sur la page
        cartes = await extraire_cartes_page(page)
        log.debug(f"   {len(cartes)} carte(s) trouvée(s)")

        for texte in cartes:
            signal = parser_signal(texte)
            if signal and est_recent(signal["temps"]):
                log.info(
                    f"🎯 Signal récent : {signal['direction']} | "
                    f"Entry={signal['entry']} | Temps='{signal['temps']}'"
                )
                return signal

        log.info(f"   Aucun signal récent. Prochain scan dans {SCAN_INTERVAL}s "
                 f"({elapsed + SCAN_INTERVAL}/{MAX_SCAN_TIME}s écoulés)")
        await asyncio.sleep(SCAN_INTERVAL)
        elapsed += SCAN_INTERVAL

    log.info("⏱  Délai de 2 min dépassé — aucun signal valide trouvé")
    return None


# ============================================================
# BOUCLE PRINCIPALE
# ============================================================

async def main():
    """
    Boucle multi-preset :
    1. Charge les presets depuis PRESETS (env var) ou utilise le défaut Scalping 5 min
    2. Pour chaque preset, déclenche une demande de signal dès que son intervalle est écoulé
    3. Si signal trouvé → envoie sur Telegram + pause globale de WAIT_AFTER_SIGNAL
    4. Si pas de signal → met à jour last_run et passe au preset suivant
    5. Dort 30 secondes entre chaque tour complet pour éviter de tourner à vide
    6. Reconnexion automatique si la session expire
    """
    presets = charger_presets()

    log.info("🚀 Démarrage du Prysm Bot")
    log.info(f"   {len(presets)} preset(s) chargé(s) :")
    for i, p in enumerate(presets):
        log.info(f"   [{i}] {p['asset']} / {p['strategy']} — toutes les {p['intervalle_min']} min")

    # Afficher les variables d'environnement actives (secrets masqués)
    log.info(f"   EMAIL          = {EMAIL}")
    log.info(f"   PASSWORD       = {'*' * len(PASSWORD)}")
    log.info(f"   TELEGRAM_TOKEN = {'*' * 10}...{TELEGRAM_TOKEN[-4:]}")
    log.info(f"   TELEGRAM_CHAT  = {TELEGRAM_CHAT}")

    # Dernier signal envoyé par preset (index → (direction, entry)) pour l'anti-doublon
    derniers_signaux: dict[int, tuple | None] = {i: None for i in range(len(presets))}

    # Horodatage du dernier appel par preset (0.0 → jamais appelé, déclenche immédiatement)
    last_run: dict[int, float] = {i: 0.0 for i in range(len(presets))}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # Connexion initiale
        if not await se_connecter(page):
            log.critical("🛑 Impossible de se connecter au site. Arrêt du bot.")
            await browser.close()
            return

        # Boucle principale
        while True:
            try:
                # Vérifier la session et se reconnecter si nécessaire
                if not await est_connecte(page):
                    log.warning("🔄 Session expirée — reconnexion en cours...")
                    if not await se_connecter(page):
                        log.error("❌ Reconnexion échouée. Nouvelle tentative dans 30s.")
                        await asyncio.sleep(30)
                        continue

                now = time.time()
                signal_envoye = False  # Flag pour sortir proprement après un envoi

                # Parcourir tous les presets dans l'ordre
                for i, preset in enumerate(presets):
                    delai = preset["intervalle_min"] * 60

                    # Vérifier si l'intervalle est écoulé pour ce preset
                    if now - last_run[i] < delai:
                        reste = int(delai - (now - last_run[i]))
                        log.debug(
                            f"   [{preset['asset']}/{preset['strategy']}] "
                            f"{reste}s avant prochain déclenchement"
                        )
                        continue

                    log.info(
                        f"⏰ Preset [{i}] {preset['asset']} / {preset['strategy']} — déclenchement"
                    )

                    # Demander le signal pour cet asset + strategy
                    ok = await demander_signal(page, preset["asset"], preset["strategy"])
                    if not ok:
                        log.warning(f"⚠️ Demande échouée pour preset [{i}] — last_run mis à jour")
                        last_run[i] = time.time()
                        continue

                    # Scanner pour un signal récent
                    signal = await scanner_signal_recent(page)
                    last_run[i] = time.time()  # Mettre à jour après le scan dans tous les cas

                    if signal is None:
                        log.info(
                            f"💤 [{preset['asset']}/{preset['strategy']}] Aucun signal valide."
                        )
                        continue

                    # Anti-doublon (même direction + même prix d'entrée)
                    cle = (signal["direction"], signal["entry"])
                    if cle == derniers_signaux[i]:
                        log.info(
                            f"⏭  [{preset['asset']}/{preset['strategy']}] Signal identique "
                            f"({signal['direction']} @ {signal['entry']}) — ignoré."
                        )
                        continue

                    # Formater et envoyer sur Telegram
                    message = formater_signal(
                        preset["asset"],
                        signal["direction"],
                        signal["entry"],
                        signal["tp"],
                        signal["sl"],
                    )
                    log.info(f"📨 Envoi du signal :\n{message}")
                    envoye = await envoyer_telegram(message)

                    if envoye:
                        derniers_signaux[i] = cle
                        log.info(f"⏳ Signal envoyé. Pause de {WAIT_AFTER_SIGNAL // 60} min.")
                        await asyncio.sleep(WAIT_AFTER_SIGNAL)
                        signal_envoye = True
                        break  # Sortir du for et repartir sur un tour complet propre
                    else:
                        log.warning("⚠️ Échec de l'envoi Telegram.")

                # Dormir 30 secondes entre chaque tour (sauf si on vient de sortir d'une pause longue)
                if not signal_envoye:
                    await asyncio.sleep(30)

            except Exception as e:
                log.error(f"❌ Erreur inattendue dans la boucle : {e}", exc_info=True)
                await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
