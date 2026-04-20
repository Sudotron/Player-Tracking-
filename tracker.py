"""
tracker.py — coc.py EventsClient setup and battle log background poller.

Two responsibilities:
  1. setup_events()        — registers @coc.PlayerEvents callbacks with the EventsClient
  2. battle_log_poller()   — async task that polls the undocumented battlelog endpoint
"""

import asyncio
import json
import logging
import os
import urllib.parse

import coc
import httpx
from dotenv import load_dotenv

import database as db
import notifier

load_dotenv()

logger = logging.getLogger(__name__)

ERROR_LOG_GROUP     = int(os.getenv("ERROR_LOG_GROUP", "0"))
BATTLE_POLL_SECS    = 60        # How often to poll battle log (seconds)
DONATED_THRESHOLD   = 30        # Min donated increase to trigger notification
RECEIVED_THRESHOLD  = 30        # Min received increase to trigger notification


# ─── coc.py API Token Helper ─────────────────────────────────────────────────

def _get_token(coc_client: coc.EventsClient) -> str | None:
    """
    Extract a live Bearer token from coc.py's internal HTTP key pool.
    Tries several attribute paths for compatibility across coc.py versions.
    """
    http = getattr(coc_client, "http", None)
    if not http:
        return None

    # coc.py stores key objects in various attributes depending on version
    for attr in ("_keys", "key_list", "keys"):
        keys_obj = getattr(http, attr, None)
        if keys_obj:
            try:
                if hasattr(keys_obj, '__next__'):
                    k = next(keys_obj)
                else:
                    k = keys_obj[0]
                return getattr(k, "key", None) or str(k)
            except Exception:
                pass

    return None


# ─── Battle Log Polling ───────────────────────────────────────────────────────

async def _fetch_battle_log(coc_client: coc.EventsClient, player_tag: str) -> list:
    """
    Call the undocumented official CoC battle log endpoint and return entries.
    Uses a Bearer token borrowed from coc.py's managed pool.
    """
    token = _get_token(coc_client)
    if not token:
        logger.warning("No CoC Bearer token available for battle log fetch.")
        return []

    encoded_tag = urllib.parse.quote(player_tag, safe="")
    url = f"https://api.clashofclans.com/v1/players/{encoded_tag}/battlelog"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )

        if resp.status_code == 200:
            data = resp.json()
            # API might wrap in {"items": [...]} or return a list directly
            return data.get("items", data) if isinstance(data, dict) else data

        if resp.status_code == 404:
            return []           # No battles yet

        logger.warning(f"Battle log HTTP {resp.status_code} for {player_tag}")
        return []

    except httpx.TimeoutException:
        logger.warning(f"Battle log timeout for {player_tag}")
        return []
    except Exception as e:
        logger.error(f"Battle log error for {player_tag}: {e}")
        return []


# Track which player tags have been seeded (first-poll baseline)
_seeded_tags: set[str] = set()


async def battle_log_poller(coc_client: coc.EventsClient, bot):
    """
    Background asyncio task.
    Polls /battlelog for every tracked player every BATTLE_POLL_SECS seconds.

    First poll per player: silently seed ALL battles as seen (no messages sent).
    Subsequent polls: only NEW battles (not yet in seen_battles) get notified.
    """
    logger.info("🔄 Battle log poller started.")

    while True:
        try:
            tags = db.get_all_tracked_tags()

            for tag in tags:
                try:
                    battles = await _fetch_battle_log(coc_client, tag)

                    if not battles:
                        # Even with no battles, mark tag as seeded so future
                        # battles (once they arrive) are treated as new.
                        _seeded_tags.add(tag)
                        continue

                    # ── First poll: seed existing history silently ───────────
                    if tag not in _seeded_tags:
                        seeded_count = 0
                        for battle in battles:
                            key = str(hash(json.dumps(battle, sort_keys=True)))
                            if not db.is_battle_seen(tag, key):
                                db.mark_battle_seen(tag, key)
                                seeded_count += 1
                        _seeded_tags.add(tag)
                        logger.info(
                            f"[battle_log] Seeded {seeded_count} existing battle(s) "
                            f"for {tag} — no messages sent."
                        )
                        continue   # skip sending on this first pass

                    # ── Subsequent polls: only send genuinely new battles ────
                    chat_id = db.get_log_chat_for_tag(tag)
                    if not chat_id:
                        continue   # group not configured yet

                    player_row = db.get_tracked_player_by_tag(tag) or {}
                    player_name = player_row.get("player_name", "Unknown")

                    for battle in battles:
                        key = str(hash(json.dumps(battle, sort_keys=True)))

                        if db.is_battle_seen(tag, key):
                            continue

                        db.mark_battle_seen(tag, key)

                        msg = notifier.fmt_battle(tag, player_name, battle)
                        if msg:
                            await notifier.send(bot, chat_id, msg)
                            await asyncio.sleep(1)   # respect flood control

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    err = f"Battle log processing failed for {tag}: {e}"
                    logger.error(err)
                    await notifier.send_error(bot, ERROR_LOG_GROUP, err)

                await asyncio.sleep(1)   # small gap between players

        except asyncio.CancelledError:
            logger.info("Battle log poller cancelled.")
            return
        except Exception as e:
            logger.error(f"Battle log poller outer error: {e}")

        await asyncio.sleep(BATTLE_POLL_SECS)


# ─── Event Helpers ────────────────────────────────────────────────────────────

def _diff_list(old_items, new_items, key="name") -> list[tuple]:
    """
    Compare two lists of coc.py objects (heroes/troops/spells).
    Returns list of (old_level, new_level, name) for items whose level increased.
    """
    old_map = {getattr(i, key): getattr(i, "level", 0) for i in (old_items or [])}
    changes = []
    for item in (new_items or []):
        name  = getattr(item, key)
        new_l = getattr(item, "level", 0)
        old_l = old_map.get(name, 0)
        if new_l > old_l:
            changes.append((old_l, new_l, name))
    return changes


# ─── Event Registration ───────────────────────────────────────────────────────

def setup_events(coc_client: coc.EventsClient, bot):
    """
    Register all @coc.PlayerEvents callbacks.
    Each callback looks up the notification group from the DB and sends a message.
    """

    async def _send(player_tag: str, text: str):
        """Look up the group for this tag and send, but only if a group is set."""
        chat_id = db.get_log_chat_for_tag(player_tag)
        if chat_id and text:
            await notifier.send(bot, chat_id, text)

    async def _log_error(context: str, tag: str, exc: Exception):
        err = (
            f"Function: <code>{context}</code>\n"
            f"Tag: <code>{tag}</code>\n"
            f"Error: {type(exc).__name__}: {exc}"
        )
        logger.error(f"[{context}] {tag}: {exc}")
        await notifier.send_error(bot, ERROR_LOG_GROUP, err)

    # ── Trophy changes ────────────────────────────────────────────────────
    @coc_client.event
    @coc.PlayerEvents.trophies()
    async def on_trophies(old: coc.Player, new: coc.Player):
        try:
            diff = new.trophies - old.trophies
            logger.info(f"[on_trophies] {new.tag}: {old.trophies} → {new.trophies} ({diff:+d})")
            await _send(new.tag, notifier.fmt_trophy_change(old, new))
        except Exception as e:
            await _log_error("on_trophies", new.tag, e)

    # ── League changes ────────────────────────────────────────────────────
    @coc_client.event
    @coc.PlayerEvents.league()
    async def on_league(old: coc.Player, new: coc.Player):
        try:
            await _send(new.tag, notifier.fmt_league_change(old, new))
        except Exception as e:
            await _log_error("on_league", new.tag, e)

    # ── Clan join / leave ─────────────────────────────────────────────────
    @coc_client.event
    @coc.PlayerEvents.joined_clan()
    async def on_joined_clan(old: coc.Player, new: coc.Player):
        try:
            msg = notifier.fmt_clan_change(old, new)
            if msg:
                await _send(new.tag, msg)
        except Exception as e:
            await _log_error("on_joined_clan", new.tag, e)

    @coc_client.event
    @coc.PlayerEvents.left_clan()
    async def on_left_clan(old: coc.Player, new: coc.Player):
        try:
            msg = notifier.fmt_clan_change(old, new)
            if msg:
                await _send(new.tag, msg)
        except Exception as e:
            await _log_error("on_left_clan", new.tag, e)

    # ── Clan role change (Elder/Co-Leader etc.) ───────────────────────────
    @coc_client.event
    @coc.PlayerEvents.role()
    async def on_role(old: coc.Player, new: coc.Player):
        try:
            # If the clan itself changed, on_joined_clan/on_left_clan already
            # handles it — firing here too causes a duplicate message.
            old_clan_tag = old.clan.tag if old.clan else None
            new_clan_tag = new.clan.tag if new.clan else None
            if old_clan_tag != new_clan_tag:
                return  # clan switch — skip, already covered
            msg = notifier.fmt_role_change(old, new)
            if msg:
                await _send(new.tag, msg)
        except Exception as e:
            await _log_error("on_role", new.tag, e)

    # ── War stars ─────────────────────────────────────────────────────────────
    @coc_client.event
    @coc.PlayerEvents.war_stars()
    async def on_war_stars(old: coc.Player, new: coc.Player):
        try:
            await _send(new.tag, notifier.fmt_war_stars(old, new))
        except Exception as e:
            await _log_error("on_war_stars", new.tag, e)

    # ── Town Hall upgrade ───────────────────────────────────────────────────
    @coc_client.event
    @coc.PlayerEvents.town_hall()
    async def on_town_hall(old: coc.Player, new: coc.Player):
        try:
            await _send(new.tag, notifier.fmt_th_upgrade(old, new))
        except Exception as e:
            await _log_error("on_town_hall", new.tag, e)

    # ── Builder Hall upgrade ─────────────────────────────────────────────────
    @coc_client.event
    @coc.PlayerEvents.builder_hall()
    async def on_builder_hall(old: coc.Player, new: coc.Player):
        try:
            await _send(new.tag, notifier.fmt_bh_upgrade(old, new))
        except Exception as e:
            await _log_error("on_builder_hall", new.tag, e)

    # ── XP level up ─────────────────────────────────────────────────────────────
    @coc_client.event
    @coc.PlayerEvents.exp_level()
    async def on_exp_level(old: coc.Player, new: coc.Player):
        try:
            await _send(new.tag, notifier.fmt_exp_level(old, new))
        except Exception as e:
            await _log_error("on_exp_level", new.tag, e)

    # ── Donations given ───────────────────────────────────────────────────────
    @coc_client.event
    @coc.PlayerEvents.donations()
    async def on_donations(old: coc.Player, new: coc.Player):
        try:
            diff = new.donations - old.donations
            if diff >= DONATED_THRESHOLD:
                await _send(new.tag, notifier.fmt_donations(old, new))
        except Exception as e:
            await _log_error("on_donations", new.tag, e)

    # ── Donations received ────────────────────────────────────────────────────
    @coc_client.event
    @coc.PlayerEvents.received()
    async def on_received(old: coc.Player, new: coc.Player):
        try:
            diff = new.received - old.received
            if diff >= RECEIVED_THRESHOLD:
                await _send(new.tag, notifier.fmt_donations_received(old, new))
        except Exception as e:
            await _log_error("on_received", new.tag, e)

    # ── Capital contributions ─────────────────────────────────────────────────
    @coc_client.event
    @coc.PlayerEvents.clan_capital_contributions()
    async def on_capital(old: coc.Player, new: coc.Player):
        try:
            diff = new.clan_capital_contributions - old.clan_capital_contributions
            if diff > 0:
                await _send(new.tag, notifier.fmt_capital_contributions(old, new))
        except Exception as e:
            await _log_error("on_capital", new.tag, e)

    # ── Hero upgrades (v4: hero_change — passes changed hero object) ──────────
    @coc_client.event
    @coc.PlayerEvents.hero_change()
    async def on_hero_change(old: coc.Player, new: coc.Player, changed_hero):
        try:
            old_hero = next((h for h in old.heroes if h.name == changed_hero.name), None)
            old_l = old_hero.level if old_hero else 0
            # Ignore if level hasn't actually increased
            if changed_hero.level <= old_l:
                return
            line = f"👑 {changed_hero.name}: Lv.{old_l} → <b>Lv.{changed_hero.level}</b>"
            await _send(new.tag, notifier.fmt_hero_upgrades(new.name, [line]))
        except Exception as e:
            await _log_error("on_hero_change", new.tag, e)

    # ── Troop upgrades (v4: troop_change — passes changed troop object) ───────
    @coc_client.event
    @coc.PlayerEvents.troop_change()
    async def on_troop_change(old: coc.Player, new: coc.Player, changed_troop):
        try:
            if getattr(changed_troop, "village", "") != "home":
                return  # skip builder base troops
            old_troop = next((t for t in old.troops if t.name == changed_troop.name), None)
            old_l = old_troop.level if old_troop else 0
            # Ignore if level hasn't actually increased (like super troop activation)
            if changed_troop.level <= old_l:
                return
            line = f"🪄 {changed_troop.name}: Lv.{old_l} → <b>Lv.{changed_troop.level}</b>"
            await _send(new.tag, notifier.fmt_troop_upgrades(new.name, [line]))
        except Exception as e:
            await _log_error("on_troop_change", new.tag, e)

    # ── Spell upgrades (v4: spell_change — passes changed spell object) ───────
    @coc_client.event
    @coc.PlayerEvents.spell_change()
    async def on_spell_change(old: coc.Player, new: coc.Player, changed_spell):
        try:
            old_spell = next((s for s in old.spells if s.name == changed_spell.name), None)
            old_l = old_spell.level if old_spell else 0
            if changed_spell.level <= old_l:
                return
            line = f"✨ {changed_spell.name}: Lv.{old_l} → <b>Lv.{changed_spell.level}</b>"
            await _send(new.tag, notifier.fmt_spell_upgrades(new.name, [line]))
        except Exception as e:
            await _log_error("on_spell_change", new.tag, e)

    # ── Equipment upgrades (v4: equipment_change) ─────────────────────────────
    @coc_client.event
    @coc.PlayerEvents.equipment_change()
    async def on_equipment_change(old: coc.Player, new: coc.Player, changed_eq):
        try:
            old_eq_list = getattr(old, "equipment", []) or []
            old_eq = next((e for e in old_eq_list if e.name == changed_eq.name), None)
            old_l = old_eq.level if old_eq else 0
            if changed_eq.level <= old_l:
                return
            line = f"⚙️ {changed_eq.name}: Lv.{old_l} → <b>Lv.{changed_eq.level}</b>"
            await _send(new.tag, notifier.fmt_equipment_upgrades(new.name, [line]))
        except Exception as e:
            await _log_error("on_equipment_change", new.tag, e)

    # ── Achievement progress ──────────────────────────────────────────────────
    @coc_client.event
    @coc.PlayerEvents.achievement_change()
    async def on_achievement_change(old_achievement, new_achievement, player):
        try:
            # Only notify when an achievement is newly completed
            old_stars = getattr(old_achievement, "stars", 0)
            new_stars = getattr(new_achievement, "stars", 0)
            if new_stars > old_stars:
                msg = notifier.fmt_achievement(player.name, old_achievement, new_achievement)
                if msg:
                    await _send(player.tag, msg)
        except Exception as e:
            await _log_error("on_achievement_change", player.tag, e)

    logger.info("✅ All coc.py PlayerEvents registered.")

