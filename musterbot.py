import asyncio
import logging
import os
import pytz
import sqlite3
from datetime import datetime, timedelta, date
from signal_bot_framework import create, AccountNumber, SignalBot
from signal_bot_framework.aliases import Context, DataMessage
from signal_bot_framework.args import QuoteMessageArgs, SendMessageArgs


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

daily_callback = None
reminder_callback = None
summary_callback = None

daily_message_ts = {}
update_status_ts = {}

# -- Database Setup ---
def db_connect():
    return sqlite3.connect(DATABASE_FILE)

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
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('checkin_time', '08:00')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('reminder_time', '10:00')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('summary_time', '11:00')")
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
    conn = sqlite3.connect(DATABASE_FILE)
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

async def post_daily_checkin_callback(signal: SignalBot):
    """Posts the daily check-in message to the target Signal group."""
    # Send the message and store its timestamp
    today_str = date.today().strftime("%Y-%m-%d")
    instructions = "\n".join(f"{emoji} (search '{status['hint']}') - {status['text']}" for emoji, status in STATUS_MAP.items())
    message = f"*Good morning! Please check in for {today_str} by reacting to this message.* ☀️\n\n{instructions}"
    response_task = await signal.send_message(CHAT_ID, message)
    response_object = await response_task
    the_result = response_object.result
    daily_message_ts[the_result['timestamp']] = today_str

async def post_daily_summary_callback(signal: SignalBot):
    today = date.today()
    if not is_workday(today): return

    today_str = today.strftime("%Y-%m-%d")
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, response_text, details FROM responses WHERE response_date = ?", (today_str,))
    responses = cursor.fetchall()
    conn.close()

    if not responses:
        summary_text = f"*Daily Status Summary for {today_str}*\n\nNo one has checked in yet."
    else:
        summary_text = f"*Daily Status Summary for {today_str}*\n"
        for user_id, response, details in responses:
            details_text = f" ({details})" if details else ""
            summary_text += f"\n• <@{user_id}>: *{response}*{details_text}"
    try:
        signal.send_message(CHAT_ID, summary_text)
        logging.info("Posted daily summary.")
    except Exception as e:
        logging.error(f"Failed to post daily summary: {e}")

async def post_reminder_callback(signal: SignalBot) -> bool:
    today = date.today()
    if not is_workday(today): return True
    today_str = today.strftime("%Y-%m-%d")

    # Get all users in the group
    group_info = await signal.get_group(CHAT_ID)
    all_users = [member['number'] for member in group_info['members']]
    
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
    return True

async def message_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    """ This callback handles all messages (not prefix or cron callbacks) """
    # first check to see if this is in the main group or it's a dm
    if context[1] == CHAT_ID:
        # it's in the main thread.  The only things that should be here are reactions.
        if message.reaction and not message.reaction["isRemove"] and message.reaction["targetSentTimestamp"] in daily_message_ts:
            await react_callback(signal, context, message)
    # if it's not in the main chat, it should be a dm. Check to see if it's an update response.
    elif context[1] in update_status_ts:
        await update_status_callback(signal, context, message)
    # log all messages that are sent 
    try:
        if message.message:
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO messages (sender_id, sender_name, destination_id, sent_timestamp, message) VALUES (?, ?, ?, ?, ?)",
                (message.sender, message.sender_name, context[1], message.timestamp, message.message)
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logging.error(f"Error handling response: {e}")
    
async def react_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    today_str = daily_message_ts.get(message.reaction["targetSentTimestamp"])
    if not today_str:
        return False # Not a reaction to a check-in message

    emoji = message.reaction["emoji"]
    status_info = STATUS_MAP.get(emoji)

    if not status_info:
        # Invalid emoji reaction
        await signal.send_message(message.sender, f"I don't understand the '{emoji}' emoji. Please react with one of the emojis from the daily check-in message.")
        return True

    status = status_info["text"]
    response_message = status_info.get("prompt")
    details = None

     # DM for more information if needed
    if response_message:
        response_task = await signal.send_message(message.sender, response_message)
        response_object = await response_task
        the_result = response_object.result
        # Store the user_id and the status for which we are awaiting details
        update_status_ts[message.sender] = {"timestamp": the_result['timestamp'], "status": status, "response_date": today_str}
    else:
        # If no more info is needed, save directly to the database
        try:
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO responses (user_id, user_name, response_date, response_text, details) VALUES (?, ?, ?, ?, ?)",
                (message.sender, message.sender_name, today_str, status, details)
            )
            conn.commit()
            conn.close()
            # Acknowledge the check-in
            await signal.send_message(message.sender, f"Thanks for checking in! I've marked you as '{status}' for {today_str}.")
        except Exception as e:
            logging.error(f"Error handling response: {e}")

    return True

async def help_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    # Ensure this is a DM
    if context[1] != message.sender:
        return False
        
    help_text = "*MusterBot Commands*\n\n"
    help_text += "*/help* - Show this help message\n"
    help_text += "*/status [date]* - Check your status for a given date (e.g., /status 2024-10-27). Defaults to today.\n"
    if is_admin(message.sender):
        help_text += "\n*Admin Commands*\n"
        help_text += "*/config [key] [value]* - View or set a configuration value (e.g., /config checkin_time 08:30)\n"
        help_text += "*/holiday [add/remove] [YYYY-MM-DD] [description]* - Add or remove a holiday.\n"
        help_text += "*/leave [add/remove] [@user] [start_date] [end_date]* - Add or remove leave for a user.\n"
        help_text += "*/add_admin [@user]* - Add a new admin.\n"
        help_text += "*/post_checkin* - Manually post the daily check-in message.\n"
        help_text += "*/post_summary* - Manually post the daily summary.\n"

    await signal.send_message(message.sender, help_text)
    return True

async def ping_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    pass

async def update_status_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    # Check if this is a reply to a request for more information
    if message.sender in update_status_ts and message.quote is not None and update_status_ts[message.sender]["timestamp"] == message.quote["id"]:
        status_update_info = update_status_ts.pop(message.sender)
        status = status_update_info["status"]
        response_date = status_update_info["response_date"]
        details = message.message

        try:
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO responses (user_id, user_name, response_date, response_text, details) VALUES (?, ?, ?, ?, ?)",
                (message.sender, message.sender_name, response_date, status, details)
            )
            conn.commit()
            conn.close()
            # Confirm the update with the user
            await signal.send_message(message.sender, f"Got it! Your status has been updated to '{status}' with the details: '{details}'.")
        except Exception as e:
            logging.error(f"Error updating status: {e}")
        return True
    return False

async def add_holiday_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    """Callback to add or remove a holiday from the database."""
    # Admin and DM only
    if not is_admin(message.sender) or context[1] != message.sender:
        return False

    parts = message.message.split(maxsplit=3)
    # Expected format: /holiday <add|remove> <YYYY-MM-DD> [description]
    if len(parts) < 3:
        await signal.send_message(message.sender, "Usage:\n/holiday add YYYY-MM-DD Description\n/holiday remove YYYY-MM-DD")
        return True

    _, action, holiday_date_str = parts[:3]
    description = parts[3] if len(parts) > 3 else ""

    try:
        # Validate date format
        date.fromisoformat(holiday_date_str)
    except ValueError:
        await signal.send_message(message.sender, "Invalid date format. Please use YYYY-MM-DD.")
        return True

    conn = db_connect()
    cursor = conn.cursor()

    if action.lower() == 'add':
        if not description:
            await signal.send_message(message.sender, "A description is required to add a holiday.")
            conn.close()
            return True
        cursor.execute("INSERT OR REPLACE INTO holidays (holiday_date, description) VALUES (?, ?)", (holiday_date_str, description))
        await signal.send_message(message.sender, f"Holiday '{description}' on {holiday_date_str} has been added. 🥳")
    elif action.lower() == 'remove':
        cursor.execute("DELETE FROM holidays WHERE holiday_date = ?", (holiday_date_str,))
        await signal.send_message(message.sender, f"Holiday on {holiday_date_str} has been removed.")
    else:
        await signal.send_message(message.sender, f"Unknown action '{action}'. Please use 'add' or 'remove'.")

    conn.commit()
    conn.close()
    return True

async def update_config_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    # Admin and DM only
    if not is_admin(message.sender) or context[1] != message.sender:
        return False

    parts = message.message.split()
    if len(parts) == 1: # /config
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM config")
        configs = cursor.fetchall()
        conn.close()
        config_text = "*Current Configuration*\n"
        for key, value in configs:
            config_text += f"\n• {key}: {value}"
        await signal.send_message(message.sender, config_text)

    elif len(parts) == 3: # /config key value
        _, key, value = parts
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()
        await signal.send_message(message.sender, f"Configuration updated: {key} = {value}")
        # Regenerate cron callbacks with the new times
        await generate_cron_callbacks(signal, context, message)
    else:
        await signal.send_message(message.sender, "Usage: /config [key] [value]")
        
    return True

async def add_admin_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    """Callback to add a new admin."""
    # Admin and DM only
    if not is_admin(message.sender) or context[1] != message.sender:
        return False

    if not message.mentions:
        await signal.send_message(message.sender, "Usage: /add_admin [@user]")
        return True

    new_admin_id = message.mentions[0]['number']
    
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_admin_id,))
    conn.commit()
    conn.close()

    await signal.send_message(message.sender, f"<@{new_admin_id}> has been added as an admin. 🛡️")
    return True

async def leave_callback(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    """Callback to add or remove leave for a user."""
    # Admin and DM only
    if not is_admin(message.sender) or context[1] != message.sender:
        return False

    parts = message.message.split()
    # Expected format: /leave <add|remove> @user <start_date> <end_date>
    if len(parts) < 4 or not message.mentions:
        await signal.send_message(message.sender, "Usage: /leave [add/remove] [@user] [start_date] [end_date]")
        return True

    action = parts[1]
    user_id = message.mentions[0]['number'] # Get the phone number from the first mention
    user_name = message.mentions[0]['name']
    start_date_str = parts[2] if action == 'remove' else parts[3]
    end_date_str = parts[3] if action == 'remove' else parts[4] if len(parts) > 4 else start_date_str # End date is optional, defaults to start date

    try:
        date.fromisoformat(start_date_str)
        date.fromisoformat(end_date_str)
    except ValueError:
        await signal.send_message(message.sender, "Invalid date format. Please use YYYY-MM-DD.")
        return True

    conn = db_connect()
    cursor = conn.cursor()

    if action.lower() == 'add':
        cursor.execute("INSERT INTO leave (user_id, user_name, start_date, end_date) VALUES (?, ?, ?, ?)", (user_id, user_name, start_date_str, end_date_str))
        await signal.send_message(message.sender, f"Leave has been added for <@{user_id}> from {start_date_str} to {end_date_str}. 🌴")
    elif action.lower() == 'remove':
        # This will remove all leave entries for the user that start on the specified date.
        cursor.execute("DELETE FROM leave WHERE user_id = ? AND start_date = ?", (user_id, start_date_str))
        await signal.send_message(message.sender, f"Leave starting on {start_date_str} for <@{user_id}> has been removed.")
    else:
        await signal.send_message(message.sender, f"Unknown action '{action}'. Please use 'add' or 'remove'.")

    conn.commit()
    conn.close()
    return True

async def generate_cron_callbacks(signal: SignalBot, context: Context, message: DataMessage) -> bool:
    global daily_callback, reminder_callback, summary_callback
    
    # Remove previous cron jobs
    if daily_callback: signal.remove_cron(daily_callback)
    if reminder_callback: signal.remove_cron(reminder_callback)
    if summary_callback: signal.remove_cron(summary_callback)

    # Get times from DB
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM config")
    configs = dict(cursor.fetchall())
    conn.close()

    local_tz_name = configs.get('timezone', 'UTC') # Default to UTC if not set
    try:
        local_tz = pytz.timezone(local_tz_name)
    except pytz.UnknownTimeZoneError:
        logging.error(f"Unknown timezone in config: {local_tz_name}. Defaulting to UTC.")
        local_tz = pytz.utc

    cron_jobs = {
        'checkin': configs.get('checkin_time', '08:00'),
        'reminder': configs.get('reminder_time', '10:00'),
        'summary': configs.get('summary_time', '11:00')
    }

    utc_now = datetime.now(pytz.utc)
    
    for job_name, local_time_str in cron_jobs.items():
        local_hour, local_minute = map(int, local_time_str.split(':'))
        
        # Create a datetime object for today in the local timezone with the specified time
        local_dt = local_tz.localize(datetime.now()).replace(hour=local_hour, minute=local_minute, second=0, microsecond=0)
        
        # Convert the localized datetime to UTC
        utc_dt = local_dt.astimezone(pytz.utc)
        
        utc_hour = utc_dt.hour
        utc_minute = utc_dt.minute
        
        cron_string = f"{utc_minute} {utc_hour} * * 1-5" # Run Monday to Friday
        
        if job_name == 'checkin':
            daily_callback = signal.on_cron(cron_string, post_daily_checkin_callback)
            logging.info(f"Scheduled daily check-in: {cron_string} (UTC)")
        elif job_name == 'reminder':
            reminder_callback = signal.on_cron(cron_string, post_reminder_callback)
            logging.info(f"Scheduled reminder: {cron_string} (UTC)")
        elif job_name == 'summary':
            summary_callback = signal.on_cron(cron_string, post_daily_summary_callback)
            logging.info(f"Scheduled summary: {cron_string} (UTC)")
            
    return True

async def main():
    """Entrypoint"""

    setup_database()

    # Create our Signal-Bot
    signal = await create(AccountNumber(MUSTERBOT_ID))

    # Register a callback for messages beginning with "/help"
    signal.on_prefix("/help", help_callback)
   
    # Register a callback for messages beginning with "/holiday"
    signal.on_prefix("/holiday", add_holiday_callback)

    # Register a callback for messages beginning with "/add_admin"
    signal.on_prefix("/add_admin", add_admin_callback)

    # Regsister a callback for messgaes beginning with "/leave"
    signal.on_prefix("/leave", leave_callback)
    
    # Register a callback for messages beginning with "/ping".
    signal.on_prefix('/ping', ping_callback)
    
    # Register a callback to deal with all messages
    signal.on_message(message_callback)

    # Register a cron callback to generate the other cron callbacks each day
    signal.on_cron("0 15 * * *", generate_cron_callbacks)

    # Callback to change the configuration settings
    signal.on_prefix("/config", update_config_callback)

    signal.on_prefix("/post_checkin", post_daily_checkin_callback)

    signal.on_prefix("/post_reminder", post_reminder_callback)

    signal.on_prefix("/post_summary", post_daily_summary_callback)
    await signal.run()

if __name__ == "__main__":
    asyncio.run(main())
