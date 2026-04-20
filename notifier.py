"""
notifier.py — Formats and sends all Telegram notification messages.

All messages use HTML parse mode.
Time is always shown in IST (UTC+5:30).
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from telegram.error import RetryAfter, TimedOut

logger = logging.getLogger(__name__)

# India Standard Time
IST = timezone(timedelta(hours=5, minutes=30))

SEP = "━" * 25


def _now() -> str:
    return datetime.now(IST).strftime("%I:%M %p IST")


def _stars(n: int) -> str:
    n = max(0, min(3, int(n)))
    return "⭐" * n + "☆" * (3 - n)


def _sign(n: int | float) -> str:
    return f"+{n}" if n >= 0 else str(n)


# ─── Send Helpers ────────────────────────────────────────────────────────────

async def send(bot, chat_id: int, text: str):
    """Send a message to a group chat with flood-control handling."""
    for attempt in range(2):  # try at most twice (once after RetryAfter)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return  # success
        except RetryAfter as e:
            wait = e.retry_after + 1
            logger.warning(f"[notifier] Flood control hit for chat={chat_id}. Waiting {wait}s…")
            await asyncio.sleep(wait)
            # loop will retry once
        except TimedOut:
            logger.warning(f"[notifier] Timed out sending to chat={chat_id}. Skipping.")
            return
        except Exception as e:
            logger.error(f"[notifier] send failed → chat={chat_id}: {e}")
            return


async def send_error(bot, error_group: int, text: str):
    """Send a formatted error to the owner error-log group."""
    if not error_group:
        return
    msg = (
        f"🔴 <b>Tracker Error</b>\n"
        f"{SEP}\n"
        f"{text}\n"
        f"🕐 {_now()}"
    )
    try:
        await bot.send_message(chat_id=error_group, text=msg, parse_mode="HTML")
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        try:
            await bot.send_message(chat_id=error_group, text=msg, parse_mode="HTML")
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[notifier] could not reach error group: {e}")

async def send_bot_log(bot, log_channel: int, text: str):
    """Send formatted log to the bot log channel."""
    if not log_channel:
        return
    msg = (
        f"📝 <b>Bot Activity Log</b>\n"
        f"{SEP}\n"
        f"{text}\n"
        f"🕐 {_now()}"
    )
    try:
        await bot.send_message(chat_id=log_channel, text=msg, parse_mode="HTML")
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        try:
            await bot.send_message(chat_id=log_channel, text=msg, parse_mode="HTML")
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[notifier] could not reach log channel: {e}")


# ─── Player Event Formatters ─────────────────────────────────────────────────

def fmt_trophy_change(old, new) -> str:
    diff = new.trophies - old.trophies
    league = new.league.name if new.league else "Unranked"
    arrow = "📈" if diff > 0 else "📉"
    return (
        f"🏆 <b>Trophy Update — {new.name}</b>\n"
        f"{SEP}\n"
        f"{arrow} Trophies:  {old.trophies:,} → <b>{new.trophies:,}</b>  ({_sign(diff)})\n"
        f"🏅 League: {league}\n"
        f"🕐 {_now()}"
    )


def fmt_league_change(old, new) -> str:
    old_l = old.league.name if old.league else "Unranked"
    new_l = new.league.name if new.league else "Unranked"
    return (
        f"🏅 <b>League Change — {new.name}</b>\n"
        f"{SEP}\n"
        f"🏅 {old_l} → <b>{new_l}</b>\n"
        f"🕐 {_now()}"
    )


def fmt_clan_change(old, new) -> str | None:
    """Called for clan join / leave only. Role changes are handled separately."""
    old_clan = old.clan
    new_clan = new.clan
    lines = []

    if old_clan and not new_clan:
        lines.append(f"🚪 Left clan: <b>{old_clan.name}</b>")
    elif not old_clan and new_clan:
        lines.append(f"🏡 Joined clan: <b>{new_clan.name}</b>")
    elif old_clan and new_clan and old_clan.tag != new_clan.tag:
        lines.append(f"🚪 Left: <b>{old_clan.name}</b>")
        lines.append(f"🏡 Joined: <b>{new_clan.name}</b>")

    if not lines:
        return None
    return (
        f"🏰 <b>Clan Activity — {new.name}</b>\n"
        f"{SEP}\n"
        + "\n".join(lines)
        + f"\n🕐 {_now()}"
    )


def fmt_role_change(old, new) -> str | None:
    """Called only when the player stays in the SAME clan but their role changes."""
    old_role = getattr(old, "role", None)
    new_role = getattr(new, "role", None)
    if not new_role or old_role == new_role:
        return None

    old_val = old_role.value if hasattr(old_role, "value") else str(old_role)
    new_val = new_role.value if hasattr(new_role, "value") else str(new_role)
    old_lbl = str(old_role) if old_role else "—"
    new_lbl = str(new_role)

    rank  = {"member": 1, "admin": 2, "coLeader": 3, "leader": 4}
    arrow = "📈" if rank.get(new_val, 0) > rank.get(old_val, 0) else "📉"
    clan_name = new.clan.name if new.clan else "Unknown Clan"

    return (
        f"🏅 <b>Role Change — {new.name}</b>\n"
        f"{SEP}\n"
        f"{arrow} {old_lbl} → <b>{new_lbl}</b>\n"
        f"🏰 Clan: {clan_name}\n"
        f"🕐 {_now()}"
    )


def fmt_war_stars(old, new) -> str:
    diff = new.war_stars - old.war_stars
    return (
        f"⭐ <b>War Stars — {new.name}</b>\n"
        f"{SEP}\n"
        f"⭐ +{diff} war stars  (Total: <b>{new.war_stars:,}</b>)\n"
        f"🕐 {_now()}"
    )


def fmt_th_upgrade(old, new) -> str:
    return (
        f"🏰 <b>Town Hall Upgrade — {new.name}</b>\n"
        f"{SEP}\n"
        f"🏰 Town Hall: Lv.{old.town_hall} → <b>Lv.{new.town_hall}</b>\n"
        f"🎉 Congratulations!\n"
        f"🕐 {_now()}"
    )


def fmt_bh_upgrade(old, new) -> str:
    return (
        f"⚒️ <b>Builder Hall Upgrade — {new.name}</b>\n"
        f"{SEP}\n"
        f"⚒️ Builder Hall: Lv.{old.builder_hall} → <b>Lv.{new.builder_hall}</b>\n"
        f"🕐 {_now()}"
    )


def fmt_exp_level(old, new) -> str:
    return (
        f"🎮 <b>Level Up — {new.name}</b>\n"
        f"{SEP}\n"
        f"🎮 XP Level: {old.exp_level} → <b>{new.exp_level}</b>\n"
        f"🕐 {_now()}"
    )


def fmt_donations(old, new) -> str:
    diff = new.donations - old.donations
    return (
        f"📦 <b>Donated — {new.name}</b>\n"
        f"{SEP}\n"
        f"📤 Donated: <b>+{diff:,}</b>  (Season total: {new.donations:,})\n"
        f"🕐 {_now()}"
    )


def fmt_donations_received(old, new) -> str:
    diff = new.received - old.received
    return (
        f"📥 <b>Received — {new.name}</b>\n"
        f"{SEP}\n"
        f"📥 Received: <b>+{diff:,}</b>  (Season total: {new.received:,})\n"
        f"🕐 {_now()}"
    )


def fmt_hero_upgrades(player_name: str, changes: list[str]) -> str:
    return (
        f"👑 <b>Hero Upgrade — {player_name}</b>\n"
        f"{SEP}\n"
        + "\n".join(changes)
        + f"\n🕐 {_now()}"
    )


def fmt_troop_upgrades(player_name: str, changes: list[str]) -> str:
    return (
        f"🪄 <b>Troop Upgrade — {player_name}</b>\n"
        f"{SEP}\n"
        + "\n".join(changes)
        + f"\n🕐 {_now()}"
    )


def fmt_spell_upgrades(player_name: str, changes: list[str]) -> str:
    return (
        f"✨ <b>Spell Upgrade — {player_name}</b>\n"
        f"{SEP}\n"
        + "\n".join(changes)
        + f"\n🕐 {_now()}"
    )


def fmt_equipment_upgrades(player_name: str, changes: list[str]) -> str:
    return (
        f"⚙️ <b>Equipment Upgrade — {player_name}</b>\n"
        f"{SEP}\n"
        + "\n".join(changes)
        + f"\n🕐 {_now()}"
    )


def fmt_capital_contributions(old, new) -> str:
    diff = new.clan_capital_contributions - old.clan_capital_contributions
    return (
        f"🏛️ <b>Capital Contribution — {new.name}</b>\n"
        f"{SEP}\n"
        f"🏛️ Capital Gold Donated: <b>+{diff:,}</b>\n"
        f"🕐 {_now()}"
    )


def fmt_achievement(player_name: str, old_ach, new_ach) -> str | None:
    """Format an achievement star-level increase."""
    old_stars = getattr(old_ach, "stars", 0)
    new_stars = getattr(new_ach, "stars", 0)
    name = getattr(new_ach, "name", "Unknown")
    value = getattr(new_ach, "value", 0)
    target = getattr(new_ach, "target", 0)

    stars_display = "⭐" * new_stars + "☆" * (3 - new_stars)

    return (
        f"🏅 <b>Achievement — {player_name}</b>\n"
        f"{SEP}\n"
        f"🎯 {name}\n"
        f"⭐ Stars: {old_stars} → <b>{new_stars}</b>  {stars_display}\n"
        f"📊 Progress: <b>{value:,}</b> / {target:,}\n"
        f"🕐 {_now()}"
    )


# ─── Battle Log Formatter ─────────────────────────────────────────────────────

def fmt_battle(player_tag: str, player_name: str, battle: dict) -> str | None:
    """
    Format a raw battle log entry from the CoC API.
    Handles the undocumented shape perfectly.
    """
    is_attack = battle.get("attack")
    if is_attack is None:
        return None  # Unrecognized format
        
    opp_tag = battle.get("opponentPlayerTag", "Unknown")
    stars = int(battle.get("stars", 0))
    destruction = float(battle.get("destructionPercentage", 0))
    
    army_share = battle.get("armyShareCode")
    army_line = ""
    if army_share:
        url = f"https://link.clashofclans.com/en?action=CopyArmy&army={army_share}"
        army_line = f"\n🪖 <a href='{url}'>View Army</a>"

    # Extract Loot
    loot_map = {item["name"]: item["amount"] for item in battle.get("lootedResources", [])}
    gold = loot_map.get("Gold", 0)
    elixir = loot_map.get("Elixir", 0)
    de = loot_map.get("DarkElixir", 0)
    
    loot_str = ""
    if gold or elixir or de:
        loot_str = f"💰 Looted: {gold:,} G | {elixir:,} E | {de:,} DE\n"

    if is_attack:
        outcome = "✅ Won" if stars >= 1 else "❌ Lost"
        return (
            f"⚔️ <b>Attack — {player_name}</b>\n"
            f"{SEP}\n"
            f"🎯 Defender:   <b>{opp_tag}</b>\n"
            f"💥 Destroyed:  <b>{destruction:.1f}%</b>  {_stars(stars)}\n"
            f"🔖 Result:     {outcome}\n"
            f"{loot_str}"
            f"{army_line.lstrip()}"
            f"\n🕐 {_now()}"
        )
    else:
        outcome = "✅ Defended" if stars == 0 else "❌ Failed"
        return (
            f"🛡️ <b>Defense — {player_name} was attacked!</b>\n"
            f"{SEP}\n"
            f"⚔️ Attacker:  <b>{opp_tag}</b>\n"
            f"💥 Destroyed: <b>{destruction:.1f}%</b>  {_stars(stars)}\n"
            f"🔖 Result:    {outcome}\n"
            f"{loot_str.replace('Looted', 'Lost')}"
            f"🕐 {_now()}"
        )


# ─── /mystats Formatter ───────────────────────────────────────────────────────

def fmt_mystats(player) -> str:
    clan_info = "No Clan"
    if player.clan:
        r = getattr(player, "role", None)
        role_str = str(r) if r else "Member"
        clan_info = f"{player.clan.name} · {role_str}"

    league  = player.league.name if player.league else "Unranked"
    profile = f"https://link.clashofclans.com/en?action=OpenPlayerProfile&tag={player.tag.lstrip('#')}"

    return (
        f"📊 <b>{player.name} ({player.tag})</b>\n"
        f"{SEP}\n"
        f"🏰 TH: <b>{player.town_hall}</b>  |  🎮 XP: <b>{player.exp_level}</b>\n"
        f"🏆 Trophies: <b>{player.trophies:,}</b>  |  Best: {player.best_trophies:,}\n"
        f"🏅 League: {league}\n"
        f"⭐ War Stars: <b>{player.war_stars:,}</b>\n"
        f"📦 Donations: {player.donations:,} · Received: {player.received:,}\n"
        f"🏰 Clan: {clan_info}\n"
        f"🔗 <a href='{profile}'>Open in Game</a>\n"
        f"🕐 {_now()}"
    )
