# Supercell Tracker

A fully-featured Telegram bot for Clash of Clans that allows users to track their player stats, clan details, battle logs, donations, and other relevant Clash of Clans activities directly inside any Telegram Group or Private Message in real time!

## 🤖 Try The Bot Live!

You can interact with the bot and invite it to your groups directly on Telegram:
👉 **[Supercell Tracker Bot](https://t.me/SupercellTracker_bot)** *(or search `@SupercellTracker_bot`)*

## 📌 Features
- **Live Automatic Notifications**: Get updates automatically routed directly to your Telegram groups!
- **Battle Log Polling**: Automatically receives logs of your attacks and defenses, complete with loot summaries and directly copyable army links.
- **Trophy & League Updates**: Reports live trophy increments, league promotions, and demotions.
- **Clan Events**: Tracks when tracked players join or leave clans, or receive promotions/demotions within their existing clan.
- **Upgrades & Unlocks**: Live update notifications for Hero, Troop, Spell, Equipment, Town Hall, and Builder Hall upgrades. 
- **Milestone Tracker**: Detects when players reach designated daily and seasonal donation requirements.

## 🛠 Commands

**Global User Commands:**
- `/start` - View the welcome message and instructions.
- `/trackplayer #TAG` - Begins tracking a player. Send this inside a group to map the notifications to that specific group chat!
- `/untrackplayer` - Delete your tracking session entirely.
- `/mystats` - Generate a live analytical snapshot chart for your currently tracked player.

**Owner Commands:**
- `/tracklist` - View every user and the specific chat they are pushing their tracking history to.
- `/botlog` - See global bot analytics and user counts.

## 🚀 Hosting & Setup
1. Fork or clone this repository to your local machine or server.
2. Install pip dependencies: `pip install -r requirements.txt`
3. Set up a `.env` file containing:
   - `BOT_TOKEN`: Provided by @BotFather via Telegram.
   - `COC_EMAIL` & `COC_PASSWORD`: Your Clash of Clans official developer API credentials.
   - `OWNER_ID`: Your personal Telegram User ID.
   - `ERROR_LOG_GROUP`: A secret channel or group ID where administrative bot activity logs (like user additions) and runtime error warnings will be sent.
4. Launch the application: `python bot.py`
