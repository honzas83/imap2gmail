import re
import os
import time
import sqlite3
import imaplib
import email
import sys
import logging
import json
from email.header import decode_header
from email.utils import parseaddr
from datetime import datetime
from dotenv import load_dotenv

# Try to import optional packages for Ollama classification
try:
    import yaml
except ImportError:
    yaml = None

try:
    import requests
    from requests.auth import HTTPDigestAuth
except ImportError:
    requests = None
    HTTPDigestAuth = None

# Load configuration
load_dotenv()

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()

# Configure logging to stderr
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

SOURCE_SERVER = os.getenv('SOURCE_IMAP_SERVER')
SOURCE_EMAIL = os.getenv('SOURCE_EMAIL')
SOURCE_PASSWORD = os.getenv('SOURCE_PASSWORD')

DEST_SERVER = os.getenv('DEST_IMAP_SERVER', 'imap.gmail.com')
DEST_EMAIL = os.getenv('DEST_EMAIL')
DEST_PASSWORD = os.getenv('DEST_PASSWORD')

CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL_SECONDS', 60))
IMAP_TIMEOUT = int(os.getenv('IMAP_TIMEOUT_SECONDS', 30))
EXCLUDE_IMPORTANT_SENDERS = [s.strip().lower() for s in os.getenv('EXCLUDE_IMPORTANT_SENDERS', '').split(',') if s.strip()]
DB_PATH = 'processed.db'

DEFAULT_SCHEMA = {
    "type": "object",
    "properties": {
        "important": {
            "type": "boolean",
            "description": "Whether the email is important"
        },
        "spam": {
            "type": "boolean",
            "description": "Whether the email is spam"
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Gmail tags/labels to assign to this email"
        }
    },
    "required": ["important", "spam", "tags"]
}

DEFAULT_PROMPT = """You are an assistant that classifies incoming emails.
Analyze the following email and respond with JSON matching this schema:
{schema}

Current Date: {current_date}
From: {from_display}
Subject: {subject}

Body:
{body}"""

def strip_html_tags(text):
    """Simple regex helper to strip HTML tags from email content."""
    clean = re.sub(r'<[^>]+>', ' ', text)
    clean = re.sub(r'\s+', ' ', clean)
    return clean.strip()

def get_email_body(msg):
    """Extracts plain text body from the email.Message object, falling back to HTML if needed."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get('Content-Disposition', ''))
            if content_type == 'text/plain' and 'attachment' not in content_disposition:
                try:
                    payload = part.get_payload(decode=True)
                    body += payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
                except Exception:
                    pass
    else:
        content_type = msg.get_content_type()
        if content_type == 'text/plain':
            try:
                payload = msg.get_payload(decode=True)
                body += payload.decode(msg.get_content_charset() or 'utf-8', errors='replace')
            except Exception:
                pass
    
    # If plain text body is empty, look for HTML content and strip tags
    if not body.strip():
        for part in (msg.walk() if msg.is_multipart() else [msg]):
            content_type = part.get_content_type()
            if content_type == 'text/html':
                try:
                    payload = part.get_payload(decode=True)
                    html_content = payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
                    body += strip_html_tags(html_content)
                except Exception:
                    pass
                    
    return body.strip()

class OllamaClassifier:
    """Handles classification of emails using an Ollama server and optional HTTP Digest Auth."""
    def __init__(self, config_path):
        self.config_path = config_path
        self.config = None
        self.endpoint = None
        self.model = None
        self.auth = None
        self.prompt_template = None
        self.schema = None
        self.enabled = False
        self.last_loaded_mtime = 0
        
        if yaml is None:
            logger.error("PyYAML is not installed. Please install it using pip to use Ollama classification.")
            return
        if requests is None:
            logger.error("requests is not installed. Please install it using pip to use Ollama classification.")
            return
            
        self.load_config()
        
    def load_config(self):
        if not self.config_path:
            return
        
        try:
            mtime = os.path.getmtime(self.config_path)
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)
            
            ollama_cfg = self.config.get('ollama', {})
            self.endpoint = ollama_cfg.get('endpoint', 'http://localhost:11434')
            self.model = ollama_cfg.get('model', 'gemma4:e4b')
            
            # Auth
            username = ollama_cfg.get('username')
            password = ollama_cfg.get('password')
            if username and password:
                self.auth = HTTPDigestAuth(username, password)
            else:
                self.auth = None
            
            classification_cfg = self.config.get('classification', {})
            self.prompt_template = classification_cfg.get('prompt', DEFAULT_PROMPT)
            self.schema = classification_cfg.get('schema', DEFAULT_SCHEMA)
            self.enabled = True
            self.last_loaded_mtime = mtime
            logger.info(f"Ollama classification loaded using config: {self.config_path}, model: {self.model}")
        except Exception as e:
            logger.error(f"Failed to load/parse Ollama configuration from {self.config_path}: {e}")
            self.enabled = False

    def classify(self, subject, from_display, body):
        # Hot-reload configuration if modified
        if self.config_path:
            try:
                mtime = os.path.getmtime(self.config_path)
                if mtime > self.last_loaded_mtime:
                    logger.info("Configuration file changed. Reloading Ollama configuration...")
                    self.load_config()
            except Exception as e:
                logger.warning(f"Failed to check for config updates/reload: {e}")

        if not self.enabled:
            return None
        
        try:
            # Formulate the prompt
            try:
                formatted_prompt = self.prompt_template.format(
                    from_display=from_display,
                    subject=subject,
                    body=body,
                    current_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    schema=json.dumps(self.schema, indent=2)
                )
            except KeyError as ke:
                logger.warning(f"KeyError formatting Ollama prompt: {ke}. Falling back to default format.")
                formatted_prompt = f"{self.prompt_template}\n\nFrom: {from_display}\nSubject: {subject}\nBody: {body}"
            
            # Ollama endpoint URL selection
            endpoint = self.endpoint.rstrip('/')
            if not (endpoint.endswith('/api/chat') or endpoint.endswith('/api/generate')):
                url = f"{endpoint}/api/chat"
            else:
                url = endpoint
            
            # Setup payload based on the URL type
            if url.endswith('/api/chat'):
                payload = {
                    "model": self.model,
                    "messages": [
                        {
                            "role": "user",
                            "content": formatted_prompt
                        }
                    ],
                    "stream": False,
                    "format": self.schema
                }
            else:
                payload = {
                    "model": self.model,
                    "prompt": formatted_prompt,
                    "stream": False,
                    "format": self.schema
                }
            
            logger.debug(f"Sending request to Ollama: {url} with model {self.model}")
            response = requests.post(url, json=payload, auth=self.auth, timeout=30)
            response.raise_for_status()
            
            resp_json = response.json()
            if 'message' in resp_json and 'content' in resp_json['message']:
                content = resp_json['message']['content']
            elif 'response' in resp_json:
                content = resp_json['response']
            else:
                raise ValueError(f"Ollama response does not contain 'message.content' or 'response': {resp_json}")
            
            # Parse structured JSON output
            result = json.loads(content)
            
            # Normalization and defaults
            fallback_res = {
                "important": True,
                "spam": False,
                "tags": []
            }
            if isinstance(result, dict):
                normalized = {
                    "important": bool(result.get('important', fallback_res['important'])),
                    "spam": bool(result.get('spam', fallback_res['spam'])),
                    "tags": [str(t) for t in result.get('tags', fallback_res['tags']) if t]
                }
                return normalized
            else:
                logger.warning(f"Ollama response JSON was not a dictionary: {result}")
                return None
            
        except Exception as e:
            logger.warning(f"Ollama classification failed: {e}. Falling back to default settings.")
            return None


def decode_mime_header(header_value):
    """Decodes MIME encoded headers like Subject or From."""
    if not header_value:
        return ""
    decoded_parts = decode_header(header_value)
    result_parts = []
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            try:
                result_parts.append(part.decode(encoding or 'utf-8', errors='replace'))
            except Exception:
                result_parts.append(part.decode('utf-8', errors='replace'))
        else:
            result_parts.append(str(part))
    return "".join(result_parts)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Create table if it doesn't exist
    cursor.execute('CREATE TABLE IF NOT EXISTS processed_emails (uid TEXT PRIMARY KEY)')
    
    # Migration: Add internal_date if missing
    cursor.execute("PRAGMA table_info(processed_emails)")
    columns = [info[1] for info in cursor.fetchall()]
    if 'internal_date' not in columns:
        logger.info("Migrating database: adding 'internal_date' column")
        cursor.execute('ALTER TABLE processed_emails ADD COLUMN internal_date TIMESTAMP')
    
    # Ensure we have at least one timestamp to act as a starting point.
    # We check for any non-null internal_date.
    cursor.execute('SELECT COUNT(*) FROM processed_emails WHERE internal_date IS NOT NULL')
    if cursor.fetchone()[0] == 0:
        now_iso = datetime.now().isoformat()
        logger.info(f"Setting initial sync point to NOW: {now_iso}. Older emails will be skipped.")
        cursor.execute('INSERT OR REPLACE INTO processed_emails (uid, internal_date) VALUES (?, ?)', ('STARTUP_MARKER', now_iso))
        
    conn.commit()
    conn.close()

def is_processed(uid):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM processed_emails WHERE uid = ?', (uid,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def mark_as_processed(uid, internal_date):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Store date as ISO string to avoid deprecation warnings in Python 3.12+
    date_iso = internal_date.isoformat() if isinstance(internal_date, datetime) else internal_date
    cursor.execute('INSERT OR REPLACE INTO processed_emails (uid, internal_date) VALUES (?, ?)', (uid, date_iso))
    conn.commit()
    conn.close()

def get_last_info():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Get max timestamp and max numeric UID
    cursor.execute('SELECT MAX(internal_date), MAX(CAST(uid AS INTEGER)) FROM processed_emails WHERE uid != "STARTUP_MARKER"')
    ts, max_uid = cursor.fetchone()
    if not ts:
        # Fallback to startup marker
        cursor.execute('SELECT internal_date FROM processed_emails WHERE uid = "STARTUP_MARKER"')
        row = cursor.fetchone()
        ts = row[0] if row else datetime.now().isoformat()
        max_uid = 0
    conn.close()
    # Ensure max_uid is at least 0
    max_uid = max_uid if max_uid is not None else 0
    return ts, max_uid

class IMAPConnection:
    def __init__(self, server, email, password, name):
        self.server = server
        self.email = email
        self.password = password
        self.name = name
        self.imap = None

    def connect(self):
        try:
            if self.imap:
                try:
                    self.imap.noop()
                    return self.imap
                except:
                    logger.info(f"Connection lost for {self.name}, reconnecting...")
                    self.disconnect()

            logger.info(f"Connecting to {self.name}: {self.server}")
            self.imap = imaplib.IMAP4_SSL(self.server, timeout=IMAP_TIMEOUT)
            self.imap.login(self.email, self.password)
            logger.info(f"Successfully logged into {self.name}")
            
            try:
                self.imap.id("name", "Thunderbird", "version", "115.10.1", "vendor", "Mozilla", "os", sys.platform)
            except Exception as e:
                logger.debug(f"ID command not supported by {self.name} server: {e}")

            return self.imap
        except Exception as e:
            logger.error(f"Failed to connect to {self.name}: {e}")
            self.imap = None
            return None

    def disconnect(self):
        if self.imap:
            try:
                # Set a very short timeout for logout to avoid hanging on shutdown
                if self.imap.sock:
                    self.imap.sock.settimeout(2.0)
                self.imap.logout()
            except:
                pass
            self.imap = None

def log_transfer_block(uid_str, new_uid, this_ts, from_display, subject, dest_folder, is_important, is_spam, tags):
    # ANSI Escape codes for pretty terminal output
    if sys.stderr.isatty():
        BLACK_BOLD = "\033[1;90m"
        BLUE_BOLD = "\033[1;34m"
        MAGENTA_BOLD = "\033[1;35m"
        GREEN_BOLD = "\033[1;32m"
        RED_BOLD = "\033[1;31m"
        YELLOW_ITALIC = "\033[3;33m"
        GRAY_ITALIC = "\033[3;90m"
        RESET = "\033[0m"
    else:
        BLACK_BOLD = BLUE_BOLD = MAGENTA_BOLD = GREEN_BOLD = RED_BOLD = YELLOW_ITALIC = GRAY_ITALIC = RESET = ""

    # Format values
    uid_val = f"{uid_str} (Source) ➔ {new_uid if new_uid else 'Unknown'} (Gmail)"
    folder_val = f"{RED_BOLD}{dest_folder}{RESET}" if "Spam" in dest_folder else f"{GREEN_BOLD}{dest_folder}{RESET}"
    
    important_val = f"{GREEN_BOLD}Yes{RESET}" if is_important else f"{GRAY_ITALIC}No{RESET}"
    spam_val = f"{RED_BOLD}Yes (Routed to Spam){RESET}" if is_spam else f"{GRAY_ITALIC}No (Routed to Inbox){RESET}"
    
    if tags:
        tags_val = ", ".join(f"{YELLOW_ITALIC}{t}{RESET}" for t in tags)
    else:
        tags_val = f"{GRAY_ITALIC}None{RESET}"

    # Build the multiline output
    block = (
        f"\n{BLACK_BOLD}┌── [EMAIL TRANSFER SUCCESS] ──────────────────────────────────────────{RESET}\n"
        f"{BLACK_BOLD}│{RESET} {BLUE_BOLD}UID:{RESET}      {uid_val}\n"
        f"{BLACK_BOLD}│{RESET} {BLUE_BOLD}Folder:{RESET}   {folder_val}\n"
        f"{BLACK_BOLD}│{RESET} {BLUE_BOLD}Date:{RESET}     {this_ts}\n"
        f"{BLACK_BOLD}│{RESET} {BLUE_BOLD}From:{RESET}     {from_display}\n"
        f"{BLACK_BOLD}│{RESET} {BLUE_BOLD}Subject:{RESET}  {subject}\n"
        f"{BLACK_BOLD}│{RESET}\n"
        f"{BLACK_BOLD}│{RESET} {MAGENTA_BOLD}LLM Classification Outputs:{RESET}\n"
        f"{BLACK_BOLD}│{RESET}   {BLUE_BOLD}Important:{RESET} {important_val}\n"
        f"{BLACK_BOLD}│{RESET}   {BLUE_BOLD}Spam:{RESET}      {spam_val}\n"
        f"{BLACK_BOLD}│{RESET}   {BLUE_BOLD}Tags:{RESET}      [{tags_val}]\n"
        f"{BLACK_BOLD}└───────────────────────────────────────────────────────────────────────{RESET}"
    )
    logger.info(block)

def transfer_emails(source_conn, dest_conn, classifier=None):
    logger.debug("Checking for new emails...")
    try:
        source_imap = source_conn.connect()
        if not source_imap:
            return

        source_imap.select('INBOX', readonly=True)

        last_ts_str, max_uid = get_last_info()
        last_ts = datetime.fromisoformat(last_ts_str) if last_ts_str else None
        
        # Optimize search: only look for UIDs higher than what we've seen
        search_criteria = 'ALL'
        if max_uid and max_uid > 0:
            search_criteria = f'UID {max_uid + 1}:*'
        
        result, data = source_imap.uid('search', None, search_criteria)
        if result != 'OK':
            logger.error(f"Failed to search source inbox with criteria: {search_criteria}")
            return

        uids = data[0].split()
        
        # Special case: First run after DB creation
        # If we have 19000+ emails and max_uid is still 0 (from STARTUP_MARKER),
        # we should mark the current highest UID as processed to avoid scanning them again.
        if max_uid == 0 and uids:
            highest_uid = max(int(u) for u in uids)
            logger.info(f"Initial check: Marking {len(uids)} existing emails as skipped (up to UID {highest_uid}).")
            # We don't need to fetch dates for all, just use the current time for the marker
            mark_as_processed(str(highest_uid), datetime.now())
            return

        # Filter out UIDs we've already seen (IMAP range search can be inclusive)
        uids = [u for u in uids if int(u) > max_uid]
        
        if not uids:
            logger.debug("No new emails found since last check.")
            return

        logger.info(f"Found {len(uids)} potential new messages. Filtering by timestamp...")
        
        # Connect to destination only if we might have work
        dest_imap = None
        new_count = 0

        for uid in uids:
            uid_str = uid.decode('utf-8')
            if is_processed(uid_str):
                continue

            # Fetch flags, internal date and content
            result, data = source_imap.uid('fetch', uid, '(FLAGS INTERNALDATE RFC822)')
            if result != 'OK' or not data or not data[0]:
                continue

            # data[0] is (metadata, raw_email)
            metadata = data[0][0] if isinstance(data[0], tuple) else data[0]
            raw_email = data[0][1] if isinstance(data[0], tuple) else None
            
            if not raw_email:
                continue

            # Parse metadata
            dt_tuple = imaplib.Internaldate2tuple(metadata)
            this_ts = datetime(*dt_tuple[0:6])
            dt_str = imaplib.Time2Internaldate(dt_tuple) if dt_tuple else None

            # Parse and update flags
            flags_match = re.search(rb'FLAGS \((.*?)\)', metadata)
            flags = flags_match.group(1).decode('utf-8').split() if flags_match else []
            
            # Clean flags: filter out \Recent (server-set)
            cleaned_flags = []
            for f in flags:
                f_lower = f.lower()
                if f_lower == '\\recent':
                    continue
                cleaned_flags.append(f)
            
            flags_str = "(" + " ".join(cleaned_flags) + ")"

            if last_ts and this_ts <= last_ts:
                # Skip older emails and mark them processed to avoid re-fetching metadata
                mark_as_processed(uid_str, this_ts)
                continue

            new_count += 1
            # Extract headers for logging and importance check
            msg = email.message_from_bytes(raw_email)
            raw_subject = msg.get('Subject', '(No Subject)')
            raw_from = msg.get('From', '(Unknown Sender)')
            
            subject = decode_mime_header(raw_subject)
            from_display = decode_mime_header(raw_from)
            _, from_email = parseaddr(from_display.lower())

            # Mark all transferred emails as Important in Gmail, 
            # unless the sender is in the exclusion list.
            is_important = True
            if from_email in EXCLUDE_IMPORTANT_SENDERS:
                is_important = False
                logger.info(f"Sender {from_email} is excluded from Important.")

            is_spam = False
            tags = []

            # Perform Ollama classification if enabled
            if classifier and classifier.enabled:
                body = get_email_body(msg)
                # Truncate body to prevent exceeding Ollama / context limits
                body_truncated = body[:4000]
                classification = classifier.classify(subject, from_display, body_truncated)
                if classification:
                    # Apply LLM classification decisions
                    is_spam = classification.get('spam', False)
                    tags = classification.get('tags', [])
                    # The hard exclusion list takes precedence over LLM classification for 'important'
                    if from_email in EXCLUDE_IMPORTANT_SENDERS:
                        is_important = False
                    else:
                        is_important = classification.get('important', is_important)

            if not dest_imap:
                dest_imap = dest_conn.connect()
                if not dest_imap:
                    return

            # Determine destination folder (Spam or Inbox)
            dest_folder = 'INBOX'
            if is_spam:
                dest_folder = '"[Gmail]/Spam"'

            # Push to destination
            logger.debug(f"Transferring UID {uid_str} | Date: {this_ts} | From: {from_display} | Subject: {subject} | Folder: {dest_folder}")
            result, response = dest_imap.append(dest_folder, flags_str, dt_str, raw_email)
            
            # Fallback if appending to spam folder fails
            if result != 'OK' and is_spam:
                logger.warning(f"Failed to append directly to {dest_folder}. Retrying append to INBOX.")
                dest_folder = 'INBOX'
                result, response = dest_imap.append(dest_folder, flags_str, dt_str, raw_email)

            if result == 'OK':
                mark_as_processed(uid_str, this_ts)
                
                new_uid = None
                # Apply labels / tags
                try:
                    # Ensure the correct folder is selected for STORE command
                    dest_imap.select(dest_folder)
                    
                    # Response looks like: [b'[APPENDUID 12345 67890] (Success)']
                    for resp in response:
                        if resp and b'APPENDUID' in resp:
                            match = re.search(r'APPENDUID\s+\d+\s+(\d+)', resp.decode())
                            if match:
                                new_uid = match.group(1)
                                
                                # Apply or remove standard Gmail 'Important' label
                                if is_important:
                                    dest_imap.uid('STORE', new_uid, '+X-GM-LABELS', '("\\\\Important")')
                                else:
                                    try:
                                        dest_imap.uid('STORE', new_uid, '-X-GM-LABELS', '("\\\\Important")')
                                    except Exception as remove_err:
                                        logger.debug(f"Could not remove Important label: {remove_err}")
                                
                                # Apply other Gmail tags / labels assigned by LLM
                                if tags:
                                    formatted_tags = []
                                    for tag in tags:
                                        tag_cleaned = tag.strip().replace('"', '\\"')
                                        if tag_cleaned:
                                            formatted_tags.append(f'"{tag_cleaned}"')
                                    
                                    if formatted_tags:
                                        tags_str = "(" + " ".join(formatted_tags) + ")"
                                        dest_imap.uid('STORE', new_uid, '+X-GM-LABELS', tags_str)
                                break
                except Exception as label_err:
                    logger.warning(f"Failed to apply labels/tags to UID {uid_str}: {label_err}")

                # Call the detailed colorized multi-line log block
                log_transfer_block(uid_str, new_uid, this_ts, from_display, subject, dest_folder, is_important, is_spam, tags)
            else:
                logger.error(f"Failed to append UID {uid_str}: {response}")
        
        if new_count == 0:
            logger.debug("No new emails found since last check.")

    except Exception as e:
        logger.error(f"Error during transfer: {e}")
        source_conn.disconnect()
        dest_conn.disconnect()

def main():
    import argparse
    parser = argparse.ArgumentParser(description="IMAP to Gmail Transfer Service")
    parser.add_argument('--ollama-config', type=str, default=os.getenv('OLLAMA_CONFIG_PATH'),
                        help="Path to Ollama configuration YAML file")
    args = parser.parse_args()

    if not all([SOURCE_SERVER, SOURCE_EMAIL, SOURCE_PASSWORD, DEST_EMAIL, DEST_PASSWORD]):
        logger.error("Missing configuration in .env file. Please check .env.example")
        sys.exit(1)

    init_db()
    
    source_conn = IMAPConnection(SOURCE_SERVER, SOURCE_EMAIL, SOURCE_PASSWORD, "Source")
    dest_conn = IMAPConnection(DEST_SERVER, DEST_EMAIL, DEST_PASSWORD, "Destination")

    # Pre-connect to both to verify credentials and servers
    if not source_conn.connect() or not dest_conn.connect():
        logger.error("Initial connection failed. Please check your credentials and server settings.")
        sys.exit(1)

    # Initialize classifier if config path is specified
    classifier = None
    if args.ollama_config:
        classifier = OllamaClassifier(args.ollama_config)
    else:
        logger.info("Ollama classification is disabled (no configuration path provided).")

    logger.info("Starting IMAP to Gmail transfer loop with persistent connections...")
    
    try:
        while True:
            transfer_emails(source_conn, dest_conn, classifier)
            logger.debug(f"Sleeping for {CHECK_INTERVAL} seconds...")
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        logger.info("Stopping script...")
    finally:
        source_conn.disconnect()
        dest_conn.disconnect()

if __name__ == "__main__":
    main()
