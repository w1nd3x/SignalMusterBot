import asyncio
import logging
import os
import sqlite3
from cron_converter import Cron
from datetime import datetime, date
from signal_bot_framework import create, AccountNumber, SignalBot
from signal_bot_framework.aliases import Context, DataMessage, CronCb, AccountUUID
from signal_bot_framework.args import ListContactArgs
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
TARGET_CHANNEL_ID = os.environ.get("TARGET_CHANNEL_ID")
CHAT_ID = os.environ.get("CHAT_ID")
MUSTERBOT_ID = os.environ.get("MUSTERBOT_ID")
DATABASE_FILE = os.environ.get("DATABASE_FILE")
REPORTING_USER_ID = os.environ.get("REPORTING_USER_ID")

# --- Emoji Status Mapping (Now with Emojis as keys) ---
STATUS_MAP = {
    "✅": {"text": "In at Normal Time", "prompt": None, "hint": "checkmark" },
    "⏱️": {"text": "In Late", "prompt": "What time do you expect to be in?", "hint":"stopwatch"},
    "🏠": {"text": "Working from Home", "prompt": None, "hint": "house"},
    "🗓️": {"text": "Appointment", "prompt": "What time do you expect to be in?", "hint": "calendar"},
    "🤒": {"text": "Out Sick", "prompt": None, "hint": "thermometer"},
    "🌴": {"text": "Liberty", "prompt": None, "hint": "palm tree"},
    "❓": {"text": "Other", "prompt": "Please provide your status for the day.", "hint": "question mark"}
}

daily_message_ts = {}
update_status_ts = {}
slack_app = None

# -- Database Setup ---
def db_connect():
    return sqlite3.connect(DATABASE_FILE) # type: ignore

def setup_database():
    """Initializes the SQLite database and creates tables if they don't exist."""
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, user_name TEXT NOT NULL,
            response_date TEXT NOT NULL, response_text TEXT NOT NULL, details TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY, sender_id TEXT NOT NULL, sender_name TEXT NOT NULL, 
            destination_id TEXT NOT NULL, sent_timestamp TEXT NOT NULL, message TEXT NOT NULL
        )               
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS leave (
            id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, user_name TEXT NOT NULL, 
            start_date TEXT NOT NULL, end_date TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tdy (
            id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, start_date TEXT NOT NULL,
            end_date TEXT NOT NULL, description TEXT NOT NULL 
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS holidays (
            holiday_date TEXT PRIMARY KEY, description TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            user_id TEXT PRIMARY KEY
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )
    ''')
    # Seed initial data if tables are empty
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('checkin_time', '06:00')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('reminder_time', '09:00')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('summary_time', '10:00')")
    # Add the reporting user as the first admin
    if REPORTING_USER_ID:
        cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (REPORTING_USER_ID,))
    conn.commit()
    conn.close()
    logging.info("Database initialized.")

# --- Helper Functions ---
def is_admin(user_id):
    """Checks if a user_id is in the admins table."""
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    is_admin_user = cursor.fetchone() is not None
    conn.close()
    return is_admin_user

def is_workday(check_date):
    """Checks if a given date is a workday (not weekend or holiday)."""
    if check_date.weekday() >= 5: # Saturday or Sunday
        return False
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM holidays WHERE holiday_date = ?", (check_date.strftime("%Y-%m-%d"),))
    is_holiday = cursor.fetchone() is not None
    conn.close()
    return not is_holiday

def is_user_on_leave(user_id, check_date):
    """Checks if a user is on leave on a specific date."""
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT start_date, end_date FROM leave WHERE user_id = ?", (user_id,))
    leave_periods = cursor.fetchall()
    conn.close()

    for start_str, end_str in leave_periods:
        start_date = date.fromisoformat(start_str)
        end_date = date.fromisoformat(end_str)
        if start_date <= check_date <= end_date:
            return True
    return False

def get_user_tdy_status(user_id, check_date):
    """Checks if a user is on TDY on a specific date and returns the description if they are."""
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT start_date, end_date, description FROM tdy WHERE user_id = ?", (user_id,))
    tdy_periods = cursor.fetchall()
    conn.close()

    for start_str, end_str, description in tdy_periods:
        start_date = date.fromisoformat(start_str)
        end_date = date.fromisoformat(end_str)
        if start_date <= check_date <= end_date:
            return description  # Return the description of the TDY
    return None

async def get_username_from_userid(signal: SignalBot, user_id: AccountUUID):
    user_info_task = await signal.list_contacts(args=ListContactArgs(recipient=user_id)) # type: ignore
    user_info_object = await user_info_task
    user_info = user_info_object.result[0]
    givenName = user_info['profile']['givenName'] if user_info['profile']['givenName'] else ''
    familyName = user_info['profile']['familyName'] if user_info['profile']['familyName'] else ''
    return f"{givenName} {familyName}"

def add_cron_while_running(signal: SignalBot, cron_hook: CronCb):
    loop = asyncio.get_running_loop()
    ref = datetime.now()
    cron_str, _ = cron_hook # type: ignore
    cron = Cron(cron_str)
    schedule = cron.schedule(ref)
    next_schedule = schedule.next()
    delay = (next_schedule - ref).total_seconds()
    signal._crons.append(loop.call_later(delay, signal._cron_repeat, signal, schedule, cron_hook)) # type: ignore

async def get_all_users(signal: SignalBot):
    group_info_task = await signal.get_group_info(CHAT_ID) # type: ignore
    group_info_response = await group_info_task
    group_info = group_info_response.result[0]
    all_users = [member for member in group_info['members']] # type: ignore
    return all_users

# --- Cron Callbacks ---
async def post_daily_checkin_callback(signal: SignalBot):
    """Posts the daily check-in message to the target Signal group."""
    today = date.today()
    if not is_workday(today):
        logging.info("Not a workday, no check-in") 
        return

    logging.info("In post_daily_checkin")
    today_str = today.strftime("%Y-%m-%d")
    instructions = "\n".join(f"{emoji} (search '{status['hint']}') - {status['text']}" for emoji, status in STATUS_MAP.items())
    message = f"☀️ Good morning! Please check in for {today_str} by reacting to this message. \n\n{instructions}"
    response_task = await signal.send_message(CHAT_ID, message) # type: ignore
    response_object = await response_task
    the_result = response_object.result
    daily_message_ts[the_result['timestamp']] = today_str
    logging.info(signal._crons) # type: ignore

async def post_daily_summary_callback(signal: SignalBot):
    today = date.today()
    if not is_workday(today): 
        logging.info("Not a workday, no summary")
        return

    logging.info("post_daily_summary_callback")
    today_str = today.strftime("%Y-%m-%d")
    summary_text = f"*Daily Status Summary for {today_str}*\n"
    try:
        group_info_task = await signal.get_group_info(CHAT_ID) # type: ignore
        group_info_response = await group_info_task
        group_info = group_info_response.result[0]
        all_users = [member['uuid'] for member in group_info['members'] if member['number'] != MUSTERBOT_ID] # type: ignore
        
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, response_text, details FROM responses WHERE response_date = ?", (today_str,))
        responses = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        conn.close()

        for user_id in all_users:
            tdy_status = get_user_tdy_status(user_id, today)
            if tdy_status:
                status_line = f"✈️ *{tdy_status}*"
            elif is_user_on_leave(user_id, today):
                status_line = f"🌴 On Leave"
            elif user_id in responses:
                response, details = responses[user_id]
                details_text = f" ({details})" if details else ""
                status_line = f"{response} {details_text}"
            else:
                status_line = "❌ Not Checked In"
            user_name = await get_username_from_userid(signal, user_id)
            summary_text += f"\n• {user_name}: {status_line}"

        await signal.send_message(CHAT_ID, summary_text) # type: ignore
        await slack_app.client.chat_postMessage(channel=TARGET_CHANNEL_ID, text=summary_text) # type: ignore
        logging.info("Posted daily summary.")
    except Exception as e:
        logging.error(f"Failed to post daily summary: {e}")

async def post_reminder_callback(signal: SignalBot) -> None:
    today = date.today()
    if not is_workday(today): 
        logging.info("Not a workday, no reminder")
        return 
    
    logging.info("In post_reminder_callback")
    today_str = today.strftime("%Y-%m-%d")

    # Get all users in the group
    group_info_task = await signal.get_group_info(CHAT_ID) # type: ignore
    group_info_response = await group_info_task
    group_info = group_info_response.result[0]
    all_users = [member['uuid'] for member in group_info['members']] # type: ignore
    
    # Get users who have responded
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM responses WHERE response_date = ?", (today_str,))
    responded_users = [row[0] for row in cursor.fetchall()]
    conn.close()

    # Find users who haven't responded and are not on leave
    for user_id in all_users:
        if user_id not in responded_users and not is_user_on_leave(user_id, today) and user_id != MUSTERBOT_ID:
            await signal.send_message(user_id, "Just a friendly reminder to please check in for today. ☀️")

    logging.info("Sent reminders.")

# --- Prefix Callbacks ---
async def help_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    # Ensure this is a DM
    if context[1] != message.sender:
        return False
        
    help_text = "*MusterBot Commands*\n\n"
    help_text += "*/help* - Show this help message\n"
    help_text += "*/status [date]* - Check your status for a given date (e.g., /status 2024-10-27). Defaults to today.\n"
    help_text += "*/leave [add/remove] [start_date YYYY-MM-DD] [end_date YYYY-MM-DD]* - Add or remove leave.\n"
    help_text += "*/tdy [add/remove] [start_date YYYY-MM-DD] [end_date YYYY-MM-DD]* - Add or remove tdy/training.\n"
    if is_admin(message.sender):
        help_text += "\n*Admin Commands*\n"
        help_text += "*/config [key] [value]* - View or set a configuration value (e.g., /config checkin_time 08:30)\n"
        help_text += "*/holiday [add/remove] [YYYY-MM-DD] [description]* - Add or remove a holiday.\n"
        help_text += "*/add_admin [@user]* - Add a new admin.\n"
        help_text += "*/status [user] [date]* - Check a user's status\n"
        help_text += "*/get_members* - Get the members of the group\n"
        help_text += "*/post_checkin* - Manually post the daily check-in message.\n"
        help_text += "*/post_summary* - Manually post the daily summary.\n"

    await signal.send_message(message.sender, help_text) # type: ignore
    return True

async def ping_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    return True
    pass

async def add_holiday_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    """Callback to add or remove a holiday from the database."""
    # Admin and DM only
    if not is_admin(message.sender_uuid) or context[1] != message.sender:
        return False

    parts = message.message.split(maxsplit=3) # type: ignore
    # Expected format: /holiday <add|remove> <YYYY-MM-DD> [description]
    if len(parts) < 3:
        await signal.send_message(message.sender_uuid, "Usage:\n/holiday add YYYY-MM-DD Description\n/holiday remove YYYY-MM-DD") # type: ignore
        return True

    _, action, holiday_date_str = parts[:3]
    description = parts[3] if len(parts) > 3 else ""

    try:
        # Validate date format
        date.fromisoformat(holiday_date_str)
    except ValueError:
        await signal.send_message(message.sender_uuid, "Invalid date format. Please use YYYY-MM-DD.") # type: ignore
        return True

    conn = db_connect()
    cursor = conn.cursor()

    if action.lower() == 'add':
        if not description:
            await signal.send_message(message.sender_uuid, "A description is required to add a holiday.") # type: ignore
            conn.close()
            return True
        cursor.execute("INSERT OR REPLACE INTO holidays (holiday_date, description) VALUES (?, ?)", (holiday_date_str, description))
        await signal.send_message(message.sender_uuid, f"Holiday '{description}' on {holiday_date_str} has been added. 🥳") # type: ignore
    elif action.lower() == 'remove':
        cursor.execute("DELETE FROM holidays WHERE holiday_date = ?", (holiday_date_str,))
        await signal.send_message(message.sender_uuid, f"Holiday on {holiday_date_str} has been removed.") # type: ignore
    else:
        await signal.send_message(message.sender_uuid, f"Unknown action '{action}'. Please use 'add' or 'remove'.") # type: ignore

    conn.commit()
    conn.close()
    return True

async def update_config_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    # Admin and DM only
    if not is_admin(message.sender_uuid) or context[1] != message.sender:
        return False

    parts = message.message.split() # type: ignore
    if len(parts) == 1: # /config
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM config")
        configs = cursor.fetchall()
        conn.close()
        config_text = "*Current Configuration*\n"
        for key, value in configs:
            config_text += f"\n• {key}: {value}"
        await signal.send_message(message.sender_uuid, config_text) # type: ignore

    elif len(parts) == 3: # /config key value
        _, key, value = parts
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()
        await signal.send_message(message.sender_uuid, f"Configuration updated: {key} = {value}") # type: ignore
        # Regenerate cron callbacks with the new times
        await generate_cron_callbacks(signal)
    else:
        await signal.send_message(message.sender_uuid, "Usage: /config [key] [value]") # type: ignore
        
    return True

async def add_admin_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    """Callback to add a new admin."""
    # Admin and DM only
    if not is_admin(message.sender_uuid) or context[1] != message.sender:
        return False

    parts = message.message.split() # type: ignore
    
    if len(parts) != 2:
        await signal.send_message(message.sender, "Usage: /add_admin [user]") # type: ignore
        return True

    new_admin_id = parts[1]

    # make sure this id actually exists on the signal servers
    
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_admin_id,))
    conn.commit()
    conn.close()

    await signal.send_message(message.sender_uuid, f"<@{new_admin_id}> has been added as an admin. 🛡️") # type: ignore
    return True

async def leave_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    """Callback to add or remove leave for a user."""
    # DM only
    if context[1] != message.sender:
        return False

    action = ""
    parts = message.message.split() # type: ignore
    sender_is_admin = is_admin(message.sender_uuid)
    target_user_id = None
    start_date_str = None
    end_date_str = None

    if sender_is_admin and len(parts) >= 4:
        target_user_id = parts[2]
        action = parts[1]
        start_date_str = parts[3]
        end_date_str = parts[4] if len(parts) > 4 else start_date_str
    if not target_user_id and len(parts) >= 3:
        target_user_id = message.sender_uuid
        action = parts[1]
        start_date_str = parts[2]
        end_date_str = parts[3] if len(parts) > 3 else start_date_str
    if not target_user_id:
        usage = "Usage: /leave <add|remove> <start_date YYYY-MM-DD> [end_date YYYY-MM-DD]\n"
        if sender_is_admin:
            usage += "Admin Usage:\n"
            usage += "  /leave [add|remove] [+123456789] [start] [end]"
        await signal.send_message(message.sender_uuid, usage)
        return True
    user_name = await get_username_from_userid(signal, target_user_id) # type: ignore

    try:
        date.fromisoformat(start_date_str) # type: ignore
        date.fromisoformat(end_date_str) # type: ignore
    except ValueError:
        await signal.send_message(message.sender_uuid, "Invalid date format. Please use YYYY-MM-DD.") # type: ignore
        return True

    conn = db_connect()
    cursor = conn.cursor()

    if action.lower() == 'add':
        cursor.execute("INSERT INTO leave (user_id, user_name, start_date, end_date) VALUES (?, ?, ?, ?)", (target_user_id, user_name, start_date_str, end_date_str))
        await signal.send_message(message.sender_uuid, f"Leave has been added for {user_name} from {start_date_str} to {end_date_str}. 🌴") # type: ignore
    elif action.lower() == 'remove':
        # This will remove all leave entries for the user that start on the specified date.
        cursor.execute("DELETE FROM leave WHERE user_id = ? AND start_date = ?", (target_user_id, start_date_str))
        await signal.send_message(message.sender_uuid, f"Leave starting on {start_date_str} for {user_name} has been removed.") # type: ignore
    else:
        await signal.send_message(message.sender_uuid, f"Unknown action '{action}'. Please use 'add' or 'remove'.") # type: ignore

    conn.commit()
    conn.close()
    return True

async def tdy_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    """Callback for users to log their own travel, training, or other temporary duty."""
    # Command must be sent in a DM
    if context[1] != message.sender:
        return False

    parts = message.message.split(maxsplit=3) # type: ignore
    # Expected format: /tdy <start_date> <end_date> <description>
    if len(parts) < 4:
        await signal.send_message(message.sender, "Usage: /tdy [add|remove] [start_date] [end_date] [description of travel/training]") # type: ignore
        return True

    _, start_date_str, end_date_str, description = parts

    try:
        start = date.fromisoformat(start_date_str)
        end = date.fromisoformat(end_date_str)
        if start > end:
            await signal.send_message(message.sender, "The start date cannot be after the end date.") # type: ignore
            return True
    except ValueError:
        await signal.send_message(message.sender, "Invalid date format. Please use YYYY-MM-DD.") # type: ignore
        return True

    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tdy (user_id, start_date, end_date, description) VALUES (?, ?, ?, ?)",
        (message.sender_uuid, start_date_str, end_date_str, description)
    )
    conn.commit()
    conn.close()

    await signal.send_message(message.sender, f"Got it. I've logged your status as '{description}' from {start_date_str} to {end_date_str}. ✈️") # type: ignore
    return True

async def get_members_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    # Admin and DM only
    if not is_admin(message.sender_uuid) or context[1] != message.sender:
        return False
    all_users = await get_all_users(signal)
    output = f""
    for user in all_users:
        username = await get_username_from_userid(signal, user['uuid'])
        output += f"{username}: {user['number']}\n"
    output += f"\n"
    await signal.send_message(message.sender_uuid, output) 
    return True

async def status_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    """Callback to retrieve status for a user."""
    # DM only
    if context[1] != message.sender:
        return False

    parts = message.message.split() # type: ignore
    sender_is_admin = is_admin(message.sender_uuid)
    target_user_id = None
    target_date = None

    if sender_is_admin and len(parts) == 3:
        target_date = parts[2]
        target_user_id = parts[1]
    if not target_user_id and len(parts) == 2:
        target_user_id = message.sender_uuid
        target_date = parts[1]
    if not target_user_id and len(parts) == 1:
        target_user_id = message.sender_uuid
        target_date = date.today().strftime("%Y-%m-%d")
    if not target_user_id:
        usage =  "Usage: /status [date]\n"
        usage += "  (Note: date should be in YYYY-MM-DD format)\n"
        if sender_is_admin:
            usage += "Admin Usage:\n"
            usage += "  /status [user] [date]\n"
            usage += "  (Note: user should be in '+15551234567' format)"
        await signal.send_message(message.sender_uuid, usage)
        return True
    
    user_name = await get_username_from_userid(signal, target_user_id) # type: ignore

    try:
        date.fromisoformat(target_date) # type: ignore
    except ValueError:
        await signal.send_message(message.sender_uuid, "Invalid date format. Please use YYYY-MM-DD.") # type: ignore
        return True

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute("SELECT user_id, user_name, response_date, response_text, details FROM responses WHERE user_id = ? AND response_date = ?", (target_user_id, target_date))
    responses = cursor.fetchall()
    conn.close()
    output = f""
    for _, user_name, _, text, details in responses:
        output += f"{user_name}: {text} ({details})\n"
    await signal.send_message(message.sender_uuid, output)
    return True

# --- Message Callbacks ---
async def message_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    """ This callback handles all messages (not prefix or cron callbacks) """
    print(message.sender_uuid)
    print(update_status_ts)
    # first check to see if this is in the main group or it's a dm
    if context[1] == CHAT_ID:
        # it's in the main thread.  The only things that should be here are reactions.
        if message.reaction and not message.reaction["isRemove"] and message.reaction["targetSentTimestamp"] in daily_message_ts:
            await react_callback(signal, context, message)
    # if it's not in the main chat, it should be a dm. Check to see if it's an update response.
    elif message.sender_uuid in update_status_ts:
        await update_status_callback(signal, context, message)
    # log all messages that are sent 
    try:
        if message.message:
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO messages (sender_id, sender_name, destination_id, sent_timestamp, message) VALUES (?, ?, ?, ?, ?)",
                (message.sender_uuid, message.sender_name, context[1], message.timestamp, message.message)
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logging.error(f"Error handling response: {e}")
        return False
    return True
    
async def react_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    today_str = daily_message_ts.get(message.reaction["targetSentTimestamp"]) # type: ignore
    if not today_str:
        return False # Not a reaction to a check-in message

    emoji = message.reaction["emoji"] # type: ignore
    status_info = STATUS_MAP.get(emoji)

    if not status_info:
        # Invalid emoji reaction
        await signal.send_message(message.sender_uuid, f"I don't understand the '{emoji}' emoji. Please react with one of the emojis from the daily check-in message.") # type: ignore
        return True

    status = status_info["text"]
    response_message = status_info.get("prompt")
    details = None

     # DM for more information if needed
    if response_message:
        print(message.sender_uuid)
        response_task = await signal.send_message(message.sender, response_message) # type: ignore
        response_object = await response_task
        the_result = response_object.result
        # Store the user_id and the status for which we are awaiting details
        update_status_ts[message.sender_uuid] = {"timestamp": the_result['timestamp'], "status": status, "response_date": today_str}
    else:
        # If no more info is needed, save directly to the database
        try:
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO responses (user_id, user_name, response_date, response_text, details) VALUES (?, ?, ?, ?, ?)",
                (message.sender_uuid, message.sender_name, today_str, status, details)
            )
            conn.commit()
            conn.close()
            # Acknowledge the check-in
            await signal.send_message(message.sender_uuid, f"Thanks for checking in! I've marked you as '{status}' for {today_str}.") # type: ignore
        except Exception as e:
            logging.error(f"Error handling response: {e}")

    return True

async def update_status_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool: 
    # Check if this is a reply to a request for more information
    status_update_info = update_status_ts.pop(message.sender_uuid)
    status = status_update_info["status"]
    response_date = status_update_info["response_date"]
    details = message.message

    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO responses (user_id, user_name, response_date, response_text, details) VALUES (?, ?, ?, ?, ?)",
            (message.sender_uuid, message.sender_name, response_date, status, details)
        )
        conn.commit()
        conn.close()
        # Confirm the update with the user
        await signal.send_message(message.sender_uuid, f"Got it! Your status has been updated to '{status}' with the details: '{details}'.") # type: ignore
    except Exception as e:
        logging.error(f"Error updating status: {e}")
    return True

# --- Test Callbacks ---
async def test_group_info_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    # Admin and DM only
    if not is_admin(message.sender_uuid) or context[1] != message.sender:
        return False
    group_info_task = await signal.get_group_info(CHAT_ID) # type: ignore
    group_info = await group_info_task
    print(group_info.result)
    #all_users = [member['number'] for member in group_info['members']] # type: ignore
    #print(all_users)
    return True

async def test_post_daily_checkin_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    # Admin and DM only
    if not is_admin(message.sender_uuid) or context[1] != message.sender:
        return False
    await post_daily_checkin_callback(signal)
    return True

async def test_post_reminder_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    # Admin and DM only
    if not is_admin(message.sender_uuid) or context[1] != message.sender:
        return False
    await post_reminder_callback(signal)
    return True

async def test_post_daily_summary_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    # Admin and DM only
    if not is_admin(message.sender_uuid) or context[1] != message.sender:
        return False
    await post_daily_summary_callback(signal)
    return True

# --- Deprecated Functions ---
async def generate_cron_callbacks(signal: SignalBot) -> None:
    # Stop all the currently sleeping jobs
    signal.stop_crons() # type: ignore

    # Get times from DB
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM config")
    configs = dict(cursor.fetchall())
    conn.close()

    cron_jobs = {
        'checkin': configs.get('checkin_time', '06:00'),
        'reminder': configs.get('reminder_time', '09:00'),
        'summary': configs.get('summary_time', '10:00')
    }
    
    for job_name, local_time_str in cron_jobs.items():
        local_hour, local_minute = map(int, local_time_str.split(':'))
        
        cron_string = f"{local_minute} {local_hour} * * 1-5" # Run Monday to Friday
        if job_name == 'checkin':
            add_cron_while_running(signal, (cron_string, post_daily_checkin_callback)) # type: ignore
            logging.info(f"Scheduled daily check-in: {cron_string}")
        elif job_name == 'reminder':
            add_cron_while_running(signal, (cron_string, post_reminder_callback)) # type: ignore
            logging.info(f"Scheduled reminder: {cron_string}")
        elif job_name == 'summary':
            add_cron_while_running(signal, (cron_string, post_daily_summary_callback)) # type: ignore
            logging.info(f"Scheduled summary: {cron_string}")

async def test_generate_cron_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    await generate_cron_callbacks(signal)
    return True

async def main():
    """Entrypoint"""
    global slack_app
    setup_database()

    # Create our Signal-Bot
    signal = await create(AccountNumber(MUSTERBOT_ID)) # type: ignore

    # Create our Slack-Bot
    slack_app = AsyncApp(token=SLACK_BOT_TOKEN)
    slack_handler = AsyncSocketModeHandler(slack_app, SLACK_APP_TOKEN)

    # Register prefix callbacks
    signal.on_prefix("/help", help_callback)
    signal.on_prefix("/leave", leave_callback)   
    signal.on_prefix('/tdy', tdy_callback)
    signal.on_prefix("/holiday", add_holiday_callback)
    signal.on_prefix("/add_admin", add_admin_callback)
    signal.on_prefix("/config", update_config_callback)
    signal.on_prefix("/get_members", get_members_callback)
    signal.on_prefix('/ping', ping_callback)
    
    # Register a callback to deal with messages
    signal.on_message(message_callback)

    # Register cron callbacks
    signal.on_cron("0 6 * * 1-5", post_daily_checkin_callback)
    signal.on_cron("0 8 * * 1-5", post_reminder_callback)
    signal.on_cron("0 10 * * 1-5", post_daily_summary_callback)

    # Register testing callbacks
    signal.on_prefix("/test_group", test_group_info_callback)
    signal.on_prefix("/post_checkin", test_post_daily_checkin_callback)
    signal.on_prefix("/post_reminder", test_post_reminder_callback)
    signal.on_prefix("/post_summary", test_post_daily_summary_callback)
    logging.info("Starting Signal and Slack bots...")
    await asyncio.gather(
        signal.run(),
        slack_handler.start_async()
    )
    await signal.run()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down...")