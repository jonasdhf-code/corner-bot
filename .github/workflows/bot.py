"""
Corner Signal Bot — GitHub Actions Edition
Roda a cada execução do workflow, busca jogos ao vivo e envia sinais.
Sem servidor, sem custo. 100% gratuito via GitHub Actions.
"""

import asyncio
import os
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
import httpx
from telegram import Bot
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MIN_MINUTE = 8
MAX_MINUTE = 78
ASIAN_LINE = 4.5
HT_LINE    = 2.5

# Arquivo de cache para não repetir sinais (persiste via GitHub Actions artifact)
CACHE_FILE = Path("sent_signals.json")

TOURNAMENTS = {
    17:   "Premier League",
    8:    "La Liga",
    23:   "Serie A",
    35:   "Bundesliga",
    34:   "Ligue 1",
    325:  "Brasileirão Série A",
    390:  "Brasileirão Série B",
    155:  "Argentina - Liga Profesional",
    242:  "MLS",
    7:    "Champions League",
    679:  "Europa League",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.sofascore.com/",
    "Accept":  "application/json",
}
BASE = "https://api.sofascore.com/api/v1"

# ─── HTTP ─────────────────────────────────────────────────────────────────────
async def sget(path: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as c:
            r = await c.get(f"{BASE}{path}")
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logger.error(f"HTTP error {path}: {e}")
    return None

# ─── DADOS ────────────────────────────────────────────────────────────────────
async def get_live_events() -> list[dict]:
    data = await sget("/sport/football/events/live")
    return data.get("events", []) if data else []

async def get_corners(event_id: int) -> tuple[int, int]:
    data = await sget(f"/event/{event_id}/statistics")
    h = a = 0
    if not data:
        return h, a
    for group in data.get("statistics", []):
        for stat in group.get("statisticsItems", []):
            if "corner" in stat.get("name", "").lower():
                try:
                    h += int(stat.get("home") or 0)
                    a += int(stat.get("away") or 0)
                except ValueError:
                    pass
    return h, a

async def get_team_avg(team_id: int) -> float:
    data = await sget(f"/team/{team_id}/events/last/0")
    if not data:
        return 0.0
    totals = []
    for ev in (data.get("events") or [])[:8]:
        eid = ev.get("id")
        if not eid:
            continue
        stats = await sget(f"/event/{eid}/statistics")
        if not stats:
            continue
        for group in stats.get("statistics", []):
            for stat in group.get("statisticsItems", []):
                if "corner" in stat.get("name", "").lower():
                    try:
                        h = int(stat.get("home") or 0)
                        a = int(stat.get("away") or 0)
                        if ev.get("homeTeam", {}).get("id") == team_id:
                            totals.append(h)
                        else:
                            totals.append(a)
                    except ValueError:
                        pass
    return round(sum(totals) / len(totals), 1) if totals else 0.0

# ─── ANÁLISE ──────────────────────────────────────────────────────────────────
def analyse(ev: dict, ch: int, ca: int, ha: float, aa: float) -> list[dict]:
    signals = []
    period  = ev.get("status", {}).get("type", "")

    now_ts  = int(datetime.now(timezone.utc).timestamp())
    start   = ev.get("time", {}).get("currentPeriodStartTimestamp") or now_ts
    base    = ev.get("time", {}).get("initial", 0) or 0
    elapsed = base + (now_ts - start) // 60 if period == "inprogress" else (45 if period == "halftime" else 0)

    if elapsed < MIN_MINUTE or elapsed > MAX_MINUTE:
        return []

    corners   = ch + ca
    home_name = ev.get("homeTeam", {}).get("name", "Casa")
    away_name = ev.get("awayTeam", {}).get("name", "Fora")
    score_h   = (ev.get("homeScore") or {}).get("current", 0)
    score_a   = (ev.get("awayScore") or {}).get("current", 0)
    home_fav  = ha >= aa
    fav_name  = home_name if home_fav else away_name
    fav_score = score_h   if home_fav else score_a
    und_score = score_a   if home_fav else score_h
    exp_total = ha + aa

    base_ctx  = dict(home=home_name, away=away_name, score_h=score_h,
                     score_a=score_a, fav=fav_name, corners_now=corners, minute=elapsed)

    # 1 — Asiáticos (jogo todo)
    if elapsed > 5:
        proj = corners + (corners / elapsed) * (90 - elapsed)
        line = round(ASIAN_LINE + corners * 0.5, 1)
        if proj >= line + 1.5 and exp_total >= ASIAN_LINE:
            obs = "Não entrar se sair gol do favorito" if fav_score == und_score else None
            signals.append({**base_ctx,
                "strategy": f"Mais {line} cantos asiáticos",
                "half": "Jogo completo",
                "confidence": "Alta" if proj >= line + 3 else "Média",
                "obs": obs})

    # 2 — 1º Tempo
    if period == "inprogress" and 15 <= elapsed <= 40:
        proj_ht = corners + (corners / elapsed) * (45 - elapsed)
        if proj_ht >= HT_LINE + 1:
            signals.append({**base_ctx,
                "strategy": f"Mais {HT_LINE} cantos no 1º Tempo",
                "half": "Primeiro tempo",
                "confidence": "Alta" if proj_ht >= HT_LINE + 2 else "Média",
                "obs": None})

    # 3 — Favorito dominante
    if elapsed >= 20:
        fav_c   = ch if home_fav else ca
        fav_avg = ha if home_fav else aa
        if fav_avg >= 5.0 and fav_c >= 3:
            signals.append({**base_ctx,
                "strategy": "Favorito dominando escanteios",
                "half": "Jogo completo",
                "confidence": "Média",
                "obs": f"{fav_name} média {fav_avg} cantos/jogo"})

    # 4 — Recuperação esperada
    if elapsed >= 25 and corners <= 1 and exp_total >= 6:
        signals.append({**base_ctx,
            "strategy": f"Mais {ASIAN_LINE} cantos (recuperação)",
            "half": "Jogo completo",
            "confidence": "Baixa",
            "obs": "Poucos cantos até agora — recuperação provável"})

    return signals

# ─── MENSAGEM ─────────────────────────────────────────────────────────────────
def fmt(sig: dict, league: str, eid: int) -> str:
    emoji = {"Alta": "🟢", "Média": "🟡", "Baixa": "🔴"}.get(sig["confidence"], "⚪")
    now   = datetime.now().strftime("%H:%M")
    link  = f"https://www.bet365.com/#/IP/EV{eid}C1"
    lines = [
        f"🎯 *Oportunidade no {sig['half']}*", "",
        f"🏆 Futebol Ao-Vivo — {league}",
        f"⚽ {sig['home']} {sig['score_h']} x {sig['score_a']} {sig['away']}  _(min. {sig['minute']})_", "",
        f"Favorito: *{sig['fav']}*", "",
        f"{emoji} *{sig['strategy']}*",
        f"📊 Escanteios atual: {sig['corners_now']}",
    ]
    if sig.get("obs"):
        lines.append(f"\n⚠️ Obs: _{sig['obs']}_")
    lines += ["", f"🔗 {link}", f"_Sinal às {now}_"]
    return "\n".join(lines)

# ─── CACHE ────────────────────────────────────────────────────────────────────
def load_cache() -> set[str]:
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            # Remove sinais com mais de 24h
            now = datetime.now().timestamp()
            return {k for k, ts in data.items() if now - ts < 86400}
        except Exception:
            pass
    return set()

def save_cache(cache: set[str]):
    now  = datetime.now().timestamp()
    data = {k: now for k in cache}
    CACHE_FILE.write_text(json.dumps(data))

# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    bot    = Bot(token=TELEGRAM_TOKEN)
    sent   = load_cache()
    events = await get_live_events()

    if not events:
        logger.info("Nenhum jogo ao vivo.")
        save_cache(sent)
        return

    logger.info(f"{len(events)} jogos ao vivo encontrados.")
    count = 0

    for ev in events:
        t_id = ev.get("tournament", {}).get("uniqueTournament", {}).get("id")
        if t_id not in TOURNAMENTS:
            continue

        league = TOURNAMENTS[t_id]
        eid    = ev["id"]
        hid    = ev.get("homeTeam", {}).get("id")
        aid    = ev.get("awayTeam", {}).get("id")

        ch, ca = await get_corners(eid)
        ha     = await get_team_avg(hid) if hid else 0.0
        aa     = await get_team_avg(aid) if aid else 0.0

        for sig in analyse(ev, ch, ca, ha, aa):
            key = f"{eid}::{sig['strategy']}"
            if key in sent:
                continue
            msg = fmt(sig, league, eid)
            try:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=msg,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
                sent.add(key)
                count += 1
                logger.info(f"✅ {sig['strategy']} — {sig['home']} x {sig['away']}")
            except Exception as e:
                logger.error(f"Telegram error: {e}")
            await asyncio.sleep(2)

        await asyncio.sleep(1)

    logger.info(f"Concluído. {count} sinais enviados.")
    save_cache(sent)

if __name__ == "__main__":
    asyncio.run(main())
