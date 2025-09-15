# Signal Muster Bot

A Python-based Signal bot to automate daily accountability and status tracking for a workgroup. The bot posts a daily check-in message, gathers responses via emoji reactions, sends reminders, and provides a daily summary.

## Overview

This project uses the `signal-bot-framework` to interact with the `signal-cli` daemon. It is designed to run continuously, providing a fully automated "muster" or check-in system for teams. All data, including user responses, leave, holidays, and configuration, is stored in a local SQLite database.

## Features

  * **Automated Daily Check-ins**: Posts a message every morning asking users to check in.
  * **Emoji-Based Responses**: Users react to the daily message with an emoji to indicate their status (e.g., ✅ for "In", 🏠 for "WFH").
  * **Follow-Up DMs**: If a status requires more information (e.g., a late arrival), the bot automatically sends a direct message to the user to ask for details.
  * **Daily Reminders**: Automatically sends a reminder via DM to anyone in the group who hasn't checked in by a configurable time.
  * **Daily Summary**: Posts a summary of everyone's status to the group at a configurable time.
  * **Leave & Holiday Tracking**: Users can log their own leave, and admins can manage leave for others and set public holidays. The bot won't ask users to check in on these days.
  * **Admin Controls**: Admins can configure bot settings, manage holidays, add other admins, and even muster on behalf of another user, all via direct message commands.
  * **Persistent Storage**: Uses a simple SQLite database to store all information.

## Prerequisites

1.  **Python 3.8+**
2.  **A dedicated phone number** registered on Signal for the bot. This number *cannot* be your primary Signal number.
3.  **signal-cli**: The bot requires `signal-cli` to be running in the background. You can find installation instructions on the [signal-cli Wiki](https://www.google.com/search?q=https://github.com/AsamK/signal-cli/wiki).
      * `signal-cli` has its own dependencies, including **Java 17 or higher**.

## Setup & Installation

1.  **Clone the Repository**

    ```bash
    git clone <your-repository-url>
    cd <repository-directory>
    ```

2.  **Install Python Dependencies**
    It's recommended to use a virtual environment.

    ```bash
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    ```

3.  **Configure `signal-cli`**

      * Follow the `signal-cli` instructions to register the bot's phone number.
      * Start `signal-cli` as a daemon on your server. This is a critical step. A common command to run it is:
        ```bash
        signal-cli --config /path/to/signal-cli/config -u +15551234567 daemon --socket /path/to/signal-cli/config/socket
        ```
        *(Replace the number and paths with your own.)*
      * Ensure the bot's number has joined the target Signal group.

4.  **Configure the Bot Script**
    Open `musterbot.py` and edit the constants at the top of the file:

      * `CHAT_ID`: The Base64 encoded Group ID of your Signal group. You can find this by running `signal-cli listGroups -u <BOT_NUMBER>`.
      * `MUSTERBOT_ID`: The bot's phone number in E.164 format.
      * `DATABASE_FILE`: The name of the SQLite database file.
      * `REPORTING_USER_ID`: The phone number of the primary admin. This user will be automatically granted admin privileges when the bot is first run.

5.  **First Run**
    Run the bot for the first time to initialize the database:

    ```bash
    python musterbot.py
    ```

    The bot will create the `accountability.db` file and set up the necessary tables. You can then stop it with `Ctrl+C`.

## Running the Bot

For the bot to work, both the `signal-cli` daemon and the Python script must be running continuously. It is highly recommended to run them as system services (e.g., using `systemd`) for reliability.

1.  **Run `signal-cli` daemon** (as a service or in a `screen`/`tmux` session).
2.  **Run the Python script**:
    ```bash
    python musterbot.py
    ```

## Usage & Commands

All commands should be sent as a **Direct Message** to the bot.

### User Commands

| Command                             | Description                                                                                             |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `/help`                             | Shows the list of available commands.                                                                   |
| `/status [YYYY-MM-DD]`              | Checks your status for a given date. Defaults to the current day.                                       |
| `/leave <add\|remove> <start_date> [end_date]` | Adds a leave period for yourself. The end date is optional and defaults to the start date. Example: `/leave add 2025-10-20 2025-10-24` |

### Admin-Only Commands

| Command                                                    | Description                                                                                                                              |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `/status [user] [date]`                                   | Checks the status of a  user for today.                                                                                  |
| `/config`                                                  | Displays the current bot configuration (check-in times, timezone, etc.).                                                                  |
| `/config <key> <value>`                                    | Sets a configuration value. Example: `/config checkin_time 08:30`. This will automatically reschedule the bot's internal jobs.         |
| `/holiday add <date> <desc>`                               | Adds a public holiday. Example: `/holiday add 2025-12-25 Christmas Day`                                                               |
| `/holiday remove <date>`                                   | Removes a public holiday.                                                                                                                |
| `/leave add [user] <start> <end>`                         | Adds a leave period for the user.                                                                                              |
| `/leave remove [user] <start>`                            | Removes a leave period for the  user that begins on the specified start date.                                                   |
| `/muster [user] <emoji> [details]`                        | Checks in on behalf of another user. Details are required for statuses like ⏱️. Example: `/muster +11234567890 ⏱️ "Arriving at 9:30 AM"` |
| `/add_admin [user]`                                       | Grants admin privileges to the user.                                                                                           |
| `/get_members`                                       | Retrieves user info so admins can add leave, etc.                                                                                           |
| `/post_checkin`                                            | Manually posts the daily check-in message to the group.                                                                                  |
| `/post_reminder`                                            | Manually posts the daily reminder to the group.                                                                                    |
| `/post_summary`                                            | Manually posts the daily status summary to the group.                                                                                    |

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.