# IMAP to Gmail Transfer Project

## Project Overview
This project provides a robust Python-based service to continuously synchronize emails from a source IMAP account (e.g., University or Work) to a destination Gmail account. It is designed to be efficient, secure, and resilient to connection drops.

## Technical Architecture

### Core Components
- **`imap2gmail.py`**: Main service handling persistent IMAP connections, email fetching, and appending to Gmail.
- **SQLite Database (`processed.db`)**: Tracks processed emails using both unique UIDs and `INTERNALDATE` timestamps.
- **Environment Variables (`.env`)**: Stores sensitive credentials and server configurations.

### Key Workflows
1. **Initial Startup**: On the very first run (empty database), the script sets a "startup marker" using the current timestamp. It skips all pre-existing emails to start syncing only from that moment onwards.
2. **Synchronization Cycle**:
    - Periodically (default 60s) checks the source INBOX.
    - Uses UID range searching (`UID last_max_uid+1:*`) to minimize server load.
    - Compares internal timestamps to ensure messages are truly newer than the last processed one.
3. **Persistence**:
    - Maintains long-lived IMAP connections.
    - Automatically reconnects using a `NOOP` check if the server closes the session.
    - Identifies as a standard mail client using the IMAP `ID` command.

## Repository Information
- **URL**: `git@github.com:honzas83/imap2gmail.git`
- **Branch**: `main`

## Environment Setup
Required variables in `.env`:
- `SOURCE_IMAP_SERVER`, `SOURCE_EMAIL`, `SOURCE_PASSWORD`
- `DEST_IMAP_SERVER`, `DEST_EMAIL`, `DEST_PASSWORD` (App Password recommended for Gmail)
- `CHECK_INTERVAL_SECONDS`
- `EXCLUDE_IMPORTANT_SENDERS` (Comma-separated list of email addresses to skip "Important" marking)
- `LOG_LEVEL` (Optional: INFO, DEBUG, WARNING, ERROR. Default: INFO)

## Maintenance Notes
- **Database Schema**: The `processed_emails` table contains `uid` (Primary Key) and `internal_date` (ISO format string).
- **Migration Logic**: The script includes automatic `ALTER TABLE` logic to handle schema updates (e.g., adding the `internal_date` column).
- **Logging**: All activity is logged to `stderr` with timestamps, including success/failure of logins and specific details (From/Subject) of transferred emails.

## Ollama Classification & Filtering
An optional classification step can process incoming emails using a local or remote Ollama model (defaulting to `gemma4:e4b`).

### Configuration
1. Set the path to your config YAML file in `.env` or as a CLI argument:
   ```env
   OLLAMA_CONFIG_PATH=ollama_config.yaml
   ```
   Or run the script with:
   ```bash
   .venv/bin/python imap2gmail.py --ollama-config ollama_config.yaml
   ```
2. Create `ollama_config.yaml` specifying:
   - `ollama`: `endpoint`, `model` (default: `gemma4:e4b`), and optional `username` & `password` for `htdigest` authentication.
   - `classification`: `prompt` template (can use `{subject}`, `{from_display}`, `{body}` variables) and `schema` representing the JSON schema for classification.

### Output Actions
- **important**: If classified as important, the Gmail system label `\Important` is assigned. If not important, it is explicitly removed. Note that hard exclusions in `EXCLUDE_IMPORTANT_SENDERS` still override the LLM.
- **spam**: If classified as spam, the email is routed directly to the `[Gmail]/Spam` folder instead of `INBOX`.
- **tags**: A list of custom strings that are assigned to the transferred email as Gmail labels (using the `+X-GM-LABELS` IMAP extension).
