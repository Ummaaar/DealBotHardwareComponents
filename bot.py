#!/usr/bin/env python3
"""
DealBot — surveille les flux RSS Dealabs et notifie Discord (webhook)
quand un composant informatique passe sous ton prix plafond.

Dépendances : Python 3.10+ et PyYAML. Tout le reste est en stdlib.

Usage :
    python bot.py                    # une vérification (mode GitHub Actions / cron)
    python bot.py --loop 600         # boucle infinie, une vérif toutes les 600 s (Docker / VPS)
    python bot.py --dry-run          # affiche les alertes sans rien envoyer
    python bot.py --test-webhook     # envoie un message de test dans Discord
    python bot.py --feed-file x.xml  # lit un flux local (tests hors-ligne)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

import yaml

log = logging.getLogger("dealbot")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 DealBot/1.0"
)
STATE_RETENTION_DAYS = 21
HEALTH_FAIL_THRESHOLD = 3          # échecs consécutifs avant d'alerter (~45 min en GitHub Actions)
HEALTH_REALERT_SECONDS = 24 * 3600 # au plus une alerte "panne" par jour
DISCORD_MAX_EMBEDS = 10

CATEGORY_STYLE = {
    # catégorie: (emoji, couleur embed)
    "gpu": ("🎮", 0xE5484D),
    "cpu": ("🧠", 0xF76B15),
    "ram": ("🧩", 0x8E4EC6),
    "ssd": ("💾", 0x3E63DD),
    "hdd": ("🗄️", 0x6E56CF),
    "mobo": ("🔌", 0x12A594),
    "psu": ("⚡", 0xFFC53D),
    "cooling": ("❄️", 0x0090FF),
    "case": ("🧱", 0x8D8D8D),
    "screen": ("🖥️", 0x30A46C),
    "periph": ("⌨️", 0xD6409F),
    "network": ("🌐", 0x00A2C7),
    "homelab": ("📦", 0xA18072),
    "laptop": ("💻", 0x5B5BD6),
    "pepite": ("🔥", 0xFF6B00),
}
DEFAULT_STYLE = ("🛒", 0x5865F2)


# --------------------------------------------------------------------------- #
# Modèle
# --------------------------------------------------------------------------- #
@dataclass
class Deal:
    uid: str
    title: str
    link: str
    price: float | None
    merchant: str | None
    published: datetime | None
    description: str = ""
    from_hot_feed: bool = False
    temperature: int | None = None
    norm: str = field(init=False)

    def __post_init__(self) -> None:
        self.norm = normalize(self.title)


@dataclass
class Target:
    name: str
    category: str
    max_price: float
    include: list[re.Pattern]
    exclude: list[re.Pattern]
    require: list[re.Pattern] = field(default_factory=list)
    min_price: float = 0.0
    kind: str = "component"  # "component" (ignore les portables) ou "laptop" (portables uniquement)


@dataclass
class Alert:
    deal: Deal
    kind: str  # "target" | "pepite"
    label: str
    category: str
    max_price: float | None = None


# --------------------------------------------------------------------------- #
# Utilitaires texte / prix
# --------------------------------------------------------------------------- #
def normalize(text: str) -> str:
    """minuscules, sans accents, tirets/underscores -> espaces, espaces compactés."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


PRICE_RE = re.compile(r"(\d[\d   .]*(?:,\d{1,2})?)\s*(?:€|eur\b|euros?\b)", re.I)


def parse_price(raw: str | None) -> float | None:
    """'640,93€' / '1 029,99 €' / '1.029,99€' / '899€' / '19.99 €' -> float."""
    if not raw:
        return None
    m = PRICE_RE.search(raw)
    if not m:
        # attribut price sans symbole (ex: "640,93")
        m = re.fullmatch(r"\s*(\d[\d   .]*(?:,\d{1,2})?)\s*", raw)
        if not m:
            return None
    num = re.sub(r"[   ]", "", m.group(1))
    if "," in num:
        num = num.replace(".", "").replace(",", ".")
    elif re.search(r"\.\d{1,2}$", num):
        head, dec = num.rsplit(".", 1)
        num = head.replace(".", "") + "." + dec
    else:
        num = num.replace(".", "")
    try:
        value = float(num)
    except ValueError:
        return None
    return value if 0 < value < 100_000 else None


TEMP_PREFIX_RE = re.compile(r"^\s*(-?\d{1,5})\s*°\s*[-–—:]?\s*")


def strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html or ""))).strip()


def fmt_eur(value: float) -> str:
    s = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return s.removesuffix(",00") + " €"


# --------------------------------------------------------------------------- #
# Flux RSS
# --------------------------------------------------------------------------- #
def http_get(url: str, timeout: int = 20, retries: int = 2) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"},
    )
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as err:
            last_err = err
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"échec téléchargement {url}: {last_err}")


def parse_feed(xml_bytes: bytes, hot: bool) -> list[Deal]:
    root = ET.fromstring(xml_bytes)
    deals: list[Deal] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link or title).strip()
        description = item.findtext("description") or ""
        published = None
        if (pub := item.findtext("pubDate")):
            try:
                published = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                published = None

        # Dealabs (réseau Pepper) : <pepper:merchant name="Amazon" price="640,93€"/>
        merchant, price = None, None
        for child in item:
            if child.tag.split("}")[-1].lower() == "merchant":
                merchant = child.attrib.get("name") or (child.text or "").strip() or None
                price = parse_price(child.attrib.get("price"))
        # Repli : prix dans le titre, puis dans la description
        if price is None:
            price = parse_price(title) or parse_price(strip_html(description)[:300])

        # Les flux "hot" préfixent le titre par la température : "173° - Processeur ..."
        temperature = None
        if (tm := TEMP_PREFIX_RE.match(title)):
            temperature = int(tm.group(1))
            title = title[tm.end():].strip()

        if title and link:
            deals.append(Deal(guid, title, link, price, merchant, published, description, hot, temperature))
    return deals


def load_deals(feeds: list[dict], feed_file: str | None,
               feed_hot: bool = False) -> tuple[list[Deal], list[str]]:
    """Retourne (deals, erreurs). erreurs contient un message par flux en échec."""
    if feed_file:
        return parse_feed(Path(feed_file).read_bytes(), hot=feed_hot), []

    merged: dict[str, Deal] = {}
    errors: list[str] = []
    for feed in feeds:
        url, hot = feed["url"], bool(feed.get("hot", False))
        try:
            for deal in parse_feed(http_get(url), hot):
                if deal.uid in merged:
                    merged[deal.uid].from_hot_feed |= hot
                else:
                    merged[deal.uid] = deal
            log.info("flux OK : %s", url)
        except (RuntimeError, ET.ParseError) as err:
            log.warning("flux ignoré (%s) : %s", url, err)
            errors.append(f"{url} : {err}")
        time.sleep(1)  # politesse envers Dealabs
    return list(merged.values()), errors


# --------------------------------------------------------------------------- #
# Règles
# --------------------------------------------------------------------------- #
def compile_patterns(patterns: list[str] | None) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in (patterns or [])]


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg.setdefault("settings", {})
    cfg.setdefault("feeds", [{"url": "https://www.dealabs.com/rss/new"},
                             {"url": "https://www.dealabs.com/rss/hot", "hot": True}])
    cfg["_global_exclude"] = compile_patterns(cfg.get("global_exclude"))
    cfg["_laptop"] = compile_patterns(cfg.get("laptop_patterns"))
    cfg["_targets"] = [
        Target(
            name=t["name"],
            category=t.get("category", "other"),
            max_price=float(t["max_price"]),
            min_price=float(t.get("min_price", 0)),
            include=compile_patterns(t["include"]),
            exclude=compile_patterns(t.get("exclude")),
            require=compile_patterns(t.get("require")),
            kind=t.get("kind", "component"),
        )
        for t in cfg.get("targets", [])
    ]
    pep = cfg.get("pepites") or {}
    cfg["_pepites"] = {
        "enabled": bool(pep.get("enabled", False)),
        "include": compile_patterns(pep.get("include")),
        "exclude": compile_patterns(pep.get("exclude")),
        "min_temperature": int(pep.get("min_temperature", 0)),
        "include_laptops": bool(pep.get("include_laptops", False)),
    }
    return cfg


def any_match(patterns: list[re.Pattern], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def evaluate(deal: Deal, cfg: dict) -> Alert | None:
    if any_match(cfg["_global_exclude"], deal.norm):
        return None
    is_laptop = any_match(cfg["_laptop"], deal.norm)

    for t in cfg["_targets"]:
        # un portable ne doit pas déclencher une cible composant (ex : "RTX 5070" d'un portable)
        if (t.kind == "laptop") != is_laptop:
            continue
        if not any_match(t.include, deal.norm) or any_match(t.exclude, deal.norm):
            continue
        # chaque motif "require" doit être présent (ex: modèle + capacité 16 Go)
        if not all(p.search(deal.norm) for p in t.require):
            continue
        if deal.price is None:
            log.debug("prix inconnu, ignoré : %s", deal.title)
            continue
        if t.min_price <= deal.price <= t.max_price:
            return Alert(deal, "target", t.name, t.category, t.max_price)

    pep = cfg["_pepites"]
    if (pep["enabled"] and deal.from_hot_feed
            and (pep["include_laptops"] or not is_laptop)
            and any_match(pep["include"], deal.norm)
            and not any_match(pep["exclude"], deal.norm)
            and (deal.temperature is None or deal.temperature >= pep["min_temperature"])):
        return Alert(deal, "pepite", "Pépite informatique (deal hot)", "pepite")
    return None


# --------------------------------------------------------------------------- #
# État (anti-doublon)
# --------------------------------------------------------------------------- #
def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("état corrompu, réinitialisation")
        return None


def save_state(path: Path, state: dict) -> None:
    cutoff = time.time() - STATE_RETENTION_DAYS * 86400
    state["alerted"] = {k: v for k, v in state.get("alerted", {}).items() if v >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def alert_key(alert: Alert) -> str:
    # une même annonce peut alerter une fois comme "cible" puis une fois comme "pépite"
    return f"{alert.kind}:{alert.deal.uid}"


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #
def build_embed(alert: Alert) -> dict:
    d = alert.deal
    emoji, color = CATEGORY_STYLE.get(alert.category, DEFAULT_STYLE)
    lines = []
    if d.price is not None:
        price_line = f"💶 **{fmt_eur(d.price)}**"
        if alert.max_price:
            economy = alert.max_price - d.price
            price_line += f"  ·  plafond {fmt_eur(alert.max_price)}"
            if economy >= 1:
                price_line += f" (−{fmt_eur(economy)})"
        lines.append(price_line)
    if d.merchant:
        lines.append(f"🏪 {d.merchant}")
    if d.temperature is not None:
        lines.append(f"🔥 {d.temperature}° sur Dealabs")
    elif d.from_hot_feed:
        lines.append("🔥 Deal hot sur Dealabs")
    embed = {
        "title": f"{emoji} {d.title}"[:256],
        "url": d.link,
        "description": "\n".join(lines)[:4000],
        "color": color,
        "footer": {"text": f"{alert.label} · Dealabs"[:2048]},
    }
    if d.published:
        embed["timestamp"] = d.published.astimezone(timezone.utc).isoformat()
    return embed


def discord_post(webhook: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    for _ in range(4):
        req = urllib.request.Request(
            webhook, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=20):
                return
        except urllib.error.HTTPError as err:
            if err.code == 429:  # rate limit Discord
                try:
                    retry = float(json.loads(err.read() or b"{}").get("retry_after", 2))
                except (ValueError, json.JSONDecodeError):
                    retry = 2.0
                time.sleep(min(retry, 30) + 0.5)
                continue
            raise RuntimeError(f"Discord HTTP {err.code}: {err.read()[:300]!r}") from err
    raise RuntimeError("Discord : trop de rate limits, abandon")


def send_alerts(webhook: str, alerts: list[Alert], settings: dict) -> None:
    for i in range(0, len(alerts), DISCORD_MAX_EMBEDS):
        chunk = alerts[i:i + DISCORD_MAX_EMBEDS]
        payload = {
            "username": settings.get("username", "DealBot"),
            "embeds": [build_embed(a) for a in chunk],
            "allowed_mentions": {"parse": ["everyone"]},
        }
        if settings.get("avatar_url"):
            payload["avatar_url"] = settings["avatar_url"]
        if settings.get("mention"):
            payload["content"] = settings["mention"]
        discord_post(webhook, payload)
        time.sleep(1)


# --------------------------------------------------------------------------- #
# Santé du bot : te prévient sur Discord quand il faut intervenir
# --------------------------------------------------------------------------- #
def send_notice(webhook: str | None, settings: dict, title: str, text: str, color: int) -> None:
    if not webhook:
        return
    try:
        discord_post(webhook, {
            "username": settings.get("username", "DealBot"),
            "embeds": [{"title": title[:256], "description": text[:4000], "color": color,
                        "timestamp": datetime.now(timezone.utc).isoformat()}],
        })
    except Exception:  # une notif de santé ne doit jamais faire planter le bot
        log.exception("impossible d'envoyer la notification de santé")


def record_failure(state: dict, webhook: str | None, settings: dict, reason: str) -> None:
    h = state.setdefault("health", {})
    h["fail_streak"] = h.get("fail_streak", 0) + 1
    h["last_error"] = reason[:1500]
    log.warning("échec n°%d : %s", h["fail_streak"], reason[:300])
    if h["fail_streak"] < HEALTH_FAIL_THRESHOLD:
        return
    if time.time() - h.get("last_down_alert", 0) < HEALTH_REALERT_SECONDS:
        return
    hint = ("**Dealabs bloque les serveurs qui font tourner le bot** (erreur 403/429) : "
            "il faut passer sur l'hébergement Docker (homelab ou VPS), voir le README."
            if re.search(r"\b(403|429)\b", reason) else
            "Regarde les logs du dernier run dans l'onglet **Actions** du dépôt GitHub.")
    send_notice(webhook, settings, "⚠️ DealBot a besoin de toi",
                f"Le bot échoue depuis {h['fail_streak']} vérifications d'affilée, "
                f"il ne surveille plus les deals.\n\n{hint}\n\n```\n{reason[:900]}\n```", 0xE5484D)
    h["last_down_alert"] = time.time()
    h["down_notified"] = True


def record_success(state: dict, webhook: str | None, settings: dict) -> None:
    h = state.setdefault("health", {})
    if h.get("down_notified"):
        send_notice(webhook, settings, "✅ DealBot fonctionne de nouveau",
                    "La surveillance des deals a repris normalement.", 0x30A46C)
    state["health"] = {"fail_streak": 0, "last_ok": time.time()}


# --------------------------------------------------------------------------- #
# Exécution
# --------------------------------------------------------------------------- #
def run_once(cfg: dict, state_path: Path, webhook: str | None, dry_run: bool,
             feed_file: str | None, feed_hot: bool = False) -> int:
    state = load_state(state_path) or {"alerted": {}}
    bootstrap = not state.get("bootstrapped") and not state.get("alerted")
    alerted: dict[str, float] = state.setdefault("alerted", {})

    deals, errors = load_deals(cfg["feeds"], feed_file, feed_hot)
    log.info("%d deals analysés", len(deals))

    if errors and not deals:
        # aucun flux lisible : on ne fait rien d'autre, on compte la panne
        if not dry_run:
            record_failure(state, webhook, cfg["settings"], "\n".join(errors))
            save_state(state_path, state)
        return 0
    if not dry_run and not feed_file:
        record_success(state, webhook, cfg["settings"])

    fresh: list[Alert] = []
    for deal in deals:
        alert = evaluate(deal, cfg)
        if alert and alert_key(alert) not in alerted:
            fresh.append(alert)

    if bootstrap and not dry_run and not cfg["settings"].get("notify_on_first_run", False):
        # Premier lancement : on mémorise l'existant sans spammer le salon.
        for a in fresh:
            alerted[alert_key(a)] = time.time()
        state["bootstrapped"] = True
        save_state(state_path, state)
        log.info("premier lancement : %d deals existants mémorisés sans notification", len(fresh))
        return 0

    if not fresh:
        log.info("aucune nouvelle alerte")
        if not dry_run:
            state["bootstrapped"] = True
            save_state(state_path, state)
        return 0

    for a in fresh:
        price = fmt_eur(a.deal.price) if a.deal.price is not None else "?"
        log.info("ALERTE [%s] %s -> %s | %s", a.label, a.deal.title, price, a.deal.link)

    if dry_run:
        return len(fresh)

    if not webhook:
        log.error("webhook Discord manquant (variable %s)", cfg["settings"].get("webhook_env", "DISCORD_WEBHOOK_URL"))
        return -1

    send_alerts(webhook, fresh, cfg["settings"])
    state["bootstrapped"] = True
    now = time.time()
    for a in fresh:
        alerted[alert_key(a)] = now
    save_state(state_path, state)
    log.info("%d alerte(s) envoyée(s)", len(fresh))
    return len(fresh)


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="DealBot Dealabs -> Discord")
    ap.add_argument("--config", default=str(here / "config.yaml"))
    ap.add_argument("--state", default=os.environ.get("DEALBOT_STATE", str(here / "state" / "state.json")))
    ap.add_argument("--dry-run", action="store_true", help="n'envoie rien, affiche les alertes")
    ap.add_argument("--loop", type=int, default=0, metavar="SECONDES", help="boucle infinie (Docker/VPS)")
    ap.add_argument("--feed-file", help="flux RSS local (tests)")
    ap.add_argument("--feed-hot", action="store_true", help="traite --feed-file comme un flux hot")
    ap.add_argument("--test-webhook", action="store_true", help="envoie un message de test")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg = load_config(Path(args.config))
    webhook = os.environ.get(cfg["settings"].get("webhook_env", "DISCORD_WEBHOOK_URL"))

    if args.test_webhook:
        if not webhook:
            log.error("définis la variable d'environnement du webhook avant le test")
            return 1
        discord_post(webhook, {
            "username": cfg["settings"].get("username", "DealBot"),
            "content": f"✅ DealBot connecté — {len(cfg['_targets'])} cibles surveillées.",
        })
        log.info("message de test envoyé")
        return 0

    while True:
        try:
            result = run_once(cfg, Path(args.state), webhook, args.dry_run, args.feed_file, args.feed_hot)
        except Exception as err:  # en boucle on ne meurt pas sur une erreur ponctuelle
            log.exception("erreur pendant la vérification")
            if not args.dry_run:
                st_path = Path(args.state)
                st = load_state(st_path) or {"alerted": {}}
                record_failure(st, webhook, cfg["settings"], f"{type(err).__name__}: {err}")
                save_state(st_path, st)
            if not args.loop:
                return 1
            result = 0
        if not args.loop:
            return 1 if result < 0 else 0
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
