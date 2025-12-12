import os
import json
import re
import discord
from discord.ext import commands

# Google Sheets imports
import gspread
from google.oauth2.service_account import Credentials

# ---------- Config ----------

INVENTORY_FILE = "inventory.json"

# Words that mean "inventory"
INVENTORY_WORDS = {
    "inventory",
    "inv",
    "stock",
    "list",
    "inventario",  # why not :)
}

# Aliases for item names
ITEM_ALIASES = {
    "sch": "schneider",
    "sh": "schneider",
    # add more if useful, e.g.:
    # "dm": "dmem",
    # "rp": "rpmi",
}


# ---------- Helpers ----------

def normalize_item_name(raw: str) -> str:
    """
    Normalize an item name:
      - lower-case
      - strip punctuation
      - apply known aliases (sch -> schneider, etc.)
    """
    key = raw.strip().lower()
    key = re.sub(r"[^\w]", "", key)
    return ITEM_ALIASES.get(key, key)


# ---------- Inventory helpers (JSON) ----------

def load_inventory():
    if not os.path.exists(INVENTORY_FILE):
        return {}
    try:
        with open(INVENTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        # If file is corrupted, start fresh
        return {}


def save_inventory(inv):
    with open(INVENTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(inv, f, indent=2)


# ---------- Google Sheets setup & sync ----------

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_gsheet = None  # will be initialized lazily


def get_sheet():
    """
    Lazily initialize and return the first worksheet of the spreadsheet.
    Uses GOOGLE_CREDS_JSON and SPREADSHEET_ID environment variables.
    """
    global _gsheet
    if _gsheet is not None:
        return _gsheet

    creds_json = os.getenv("GOOGLE_CREDS_JSON")
    if not creds_json:
        raise RuntimeError(
            "GOOGLE_CREDS_JSON environment variable is not set.\n"
            "In Replit, add it in the Secrets panel (full JSON from your service account)."
        )

    try:
        info = json.loads(creds_json)
    except json.JSONDecodeError as e:
        raise RuntimeError("GOOGLE_CREDS_JSON is not valid JSON.") from e

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    client = gspread.authorize(creds)

    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise RuntimeError(
            "SPREADSHEET_ID environment variable is not set.\n"
            "Set it to your Google Sheet ID."
        )

    _gsheet = client.open_by_key(spreadsheet_id).sheet1
    return _gsheet


def sync_inventory_to_sheets(inv: dict):
    """
    Overwrite the Google Sheet with the current inventory.
    Row 1: headers
    Then: one row per item
    """
    if not inv:
        data = [["Medium", "Volume (ml)"]]
    else:
        data = [["Medium", "Volume (ml)"]]
        for name, amount in sorted(inv.items()):
            data.append([name.upper(), amount])

    try:
        sheet = get_sheet()
        sheet.clear()
        sheet.update("A1", data)
    except Exception as e:
        # Don't crash the bot if Sheets fails
        print(f"[WARN] Error syncing to Google Sheets: {e}")


def load_inventory_from_sheet():
    """
    Read inventory from Google Sheets and return as dict.
    Expected sheet format:
        Row 1: Medium | Volume (ml)
        Row 2+: DMEM  | 200
    """
    try:
        sheet = get_sheet()
        data = sheet.get_all_values()

        if not data or len(data) < 2:
            return {}

        inv = {}
        # Skip header row
        for row in data[1:]:
            if len(row) < 2:
                continue
            name = row[0].strip().lower()
            val = row[1].strip()

            if not name:
                continue

            try:
                amount = int(val)
            except ValueError:
                # Ignore rows with non-integer values
                continue

            inv[name] = amount

        return inv

    except Exception as e:
        print(f"[WARN] Could not load inventory from Google Sheets: {e}")
        return None  # caller decides what to do


def update_item(inv, raw_name, delta_ml):
    """
    Update an item by delta_ml (can be negative), save JSON, sync Sheets.
    Return (canonical_name, new_total).
    """
    item_key = normalize_item_name(raw_name)
    current = inv.get(item_key, 0)
    new_value = current + delta_ml
    inv[item_key] = new_value
    save_inventory(inv)
    sync_inventory_to_sheets(inv)
    return item_key, new_value


def set_item(inv, raw_name, amount_ml):
    """
    Set an item to an exact amount, save JSON, sync Sheets.
    Return (canonical_name, new_total).
    """
    item_key = normalize_item_name(raw_name)
    inv[item_key] = amount_ml
    save_inventory(inv)
    sync_inventory_to_sheets(inv)
    return item_key, amount_ml


# ---------- Discord setup ----------

intents = discord.Intents.default()
intents.message_content = True  # needed to read normal messages

# We keep a command_prefix (here "!") so you can still use !help if you like.
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)


# ---------- Message handling (natural language) ----------

@bot.event
async def on_message(message: discord.Message):
    # Ignore messages from bots (including itself)
    if message.author.bot:
        return

    # ALWAYS sync JSON from Google Sheets first (Sheets is source of truth)
    sheet_inv = load_inventory_from_sheet()
    if sheet_inv is not None:
        save_inventory(sheet_inv)

    raw_content = message.content.strip()
    if not raw_content:
        return

    content = raw_content.lower()
    tokens = content.split()

    # 1) HELP shortcuts: "help", "h", "?"
    if tokens[0] in {"help", "h", "?"} and len(tokens) <= 2:
        await send_help(message.channel)
        return

    # 2) INVENTORY commands like:
    #    "inventory", "inv", "inventory dmem", "inv dmem"
    first_word_clean = re.sub(r"[^\w]", "", tokens[0])
    if first_word_clean in INVENTORY_WORDS:
        inv = load_inventory()

        # Just "inv" -> full inventory
        if len(tokens) == 1:
            await send_full_inventory(message.channel, inv)
            return

        # "inv dmem" -> single item
        item_name = normalize_item_name(tokens[1])
        await send_single_item_inventory(
            message.channel, inv, item_name, original_query=tokens[1]
        )
        return

    # 3) Single-word query for an item:
    #    "dmem" -> show that item (if it exists)
    if len(tokens) == 1:
        inv = load_inventory()
        item_name = normalize_item_name(tokens[0])
        if item_name in inv:
            await send_single_item_inventory(
                message.channel, inv, item_name, original_query=tokens[0]
            )
            return
        # If not in inventory, treat as normal chat and do nothing

    # 4) Update pattern:
    #    "dmem -200", "dmem -200ml", "dmem: -200", etc.
    # Require explicit + or - so we don't accidentally match random numbers.
    update_pattern = r"^(?P<name>\w+)\s*:?\s*(?P<amount>[+-]\d+)\s*(?:ml)?\b"
    m = re.match(update_pattern, content, flags=re.IGNORECASE)
    if m:
        item_raw = m.group("name")
        amount_str = m.group("amount")

        try:
            delta_ml = int(amount_str)
        except ValueError:
            await message.channel.send(
                "I couldn't understand the amount. "
                "Use something like `dmem -200` or `m199 +500`."
            )
            return

        inv = load_inventory()
        canonical_name, new_total = update_item(inv, item_raw, delta_ml)
        await message.channel.send(
            f"Updated **{canonical_name}** by {delta_ml} ml. "
            f"New total: {new_total} ml."
        )
        return

    # 5) Manual set with a keyword, e.g.:
    #    "set dmem 1000" or "set sch 500"
    if tokens[0] == "set" and len(tokens) >= 3:
        _, item_raw, amount_str = tokens[0], tokens[1], tokens[2]
        try:
            amount = int(amount_str)
        except ValueError:
            await message.channel.send(
                "Usage: `set <item> <amount_ml>`\nExample: `set dmem 1000`"
            )
            return

        inv = load_inventory()
        canonical_name, new_total = set_item(inv, item_raw, amount)
        await message.channel.send(
            f"Set **{canonical_name}** to {new_total} ml."
        )
        return

    # If none of our custom handlers triggered, we still let any "!" commands work
    await bot.process_commands(message)


# ---------- Helper functions for replies ----------

async def send_full_inventory(channel: discord.TextChannel, inv: dict):
    if not inv:
        await channel.send("Inventory is empty.")
        return

    lines = ["Current inventory:"]
    for name, amount in sorted(inv.items()):
        lines.append(f"- {name}: {amount} ml")

    await channel.send("\n".join(lines))


async def send_single_item_inventory(
    channel: discord.TextChannel, inv: dict, item_key: str, original_query: str
):
    if item_key in inv:
        await channel.send(f"{item_key}: {inv[item_key]} ml")
    else:
        await channel.send(f"No entry found for '{original_query}'.")


async def send_help(channel: discord.TextChannel):
    help_text = (
        "**Inventory bot usage (no # needed):**\n\n"
        "__Update amounts:__\n"
        "- `dmem -200` → subtract 200 ml from DMEM\n"
        "- `m199 +500ml` → add 500 ml to M199\n"
        "- `sch -50` → subtract 50 ml from Schneider (because `sch` is aliased)\n\n"
        "__Check inventory:__\n"
        "- `inv` or `inventory` → show all items\n"
        "- `inv dmem` or `inventory m199` → show a single item\n"
        "- `dmem` (just the name) → show that item's amount (if it exists)\n\n"
        "__Set exact value:__\n"
        "- `set dmem 1000` → set DMEM to exactly 1000 ml\n\n"
        "__Aliases:__\n"
        "- `sch` or `sh` → interpreted as **schneider**\n\n"
        "Inventory is stored in `inventory.json` and kept in sync with a Google Sheet."
    )
    await channel.send(help_text)


# ---------- Optional: keep command-based help if you ever type !help ----------

@bot.command(name="help")
async def help_command(ctx: commands.Context):
    await send_help(ctx.channel)


# ---------- Run the bot ----------

if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN environment variable is not set.\n"
            "In Replit, add it in Secrets. Locally, export it in your shell."
        )
    bot.run(token)
