import os
import time
import sqlite3
import imaplib
import email
import sys
import logging
from datetime import datetime
from dotenv import load_dotenv

# Configure logging to stderr
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

# Load configuration
load_dotenv()

SOURCE_SERVER = os.getenv('SOURCE_IMAP_SERVER')
SOURCE_EMAIL = os.getenv('SOURCE_EMAIL')
SOURCE_PASSWORD = os.getenv('SOURCE_PASSWORD')

DEST_SERVER = os.getenv('DEST_IMAP_SERVER', 'imap.gmail.com')
DEST_EMAIL = os.getenv('DEST_EMAIL')
DEST_PASSWORD = os.getenv('DEST_PASSWORD')

CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL_SECONDS', 60))
DB_PATH = 'processed.db'

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
            self.imap = imaplib.IMAP4_SSL(self.server)
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
                self.imap.logout()
            except:
                pass
            self.imap = None

def transfer_emails(source_conn, dest_conn):
    logger.info("Checking for new emails...")
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
            logger.info("No new emails found since last check.")
            return

        logger.info(f"Found {len(uids)} potential new messages. Filtering by timestamp...")
        
        # Connect to destination only if we might have work
        dest_imap = None
        new_count = 0

        for uid in uids:
            uid_str = uid.decode('utf-8')
            if is_processed(uid_str):
                continue

            # Fetch internal date first to compare
            result, data = source_imap.uid('fetch', uid, '(INTERNALDATE RFC822)')
            if result != 'OK' or not data or not data[0]:
                continue

            # data[0] is (metadata, raw_email)
            metadata = data[0][0] if isinstance(data[0], tuple) else data[0]
            raw_email = data[0][1] if isinstance(data[0], tuple) else None
            
            if not raw_email:
                continue

            internal_date_tuple = imaplib.Internaldate2tuple(metadata)[0:6]
            this_ts = datetime(*internal_date_tuple)

            if last_ts and this_ts <= last_ts:
                # Skip older emails and mark them processed to avoid re-fetching metadata
                mark_as_processed(uid_str, this_ts)
                continue

            new_count += 1
            # Extract headers for logging
            msg = email.message_from_bytes(raw_email)
            subject = msg.get('Subject', '(No Subject)')
            from_addr = msg.get('From', '(Unknown Sender)')

            if not dest_imap:
                dest_imap = dest_conn.connect()
                if not dest_imap:
                    return

            # Push to destination
            logger.info(f"Transferring UID {uid_str} | Date: {this_ts} | From: {from_addr} | Subject: {subject}")
            result, response = dest_imap.append('INBOX', None, None, raw_email)
            
            if result == 'OK':
                mark_as_processed(uid_str, this_ts)
                logger.info(f"Successfully transferred UID {uid_str}")
            else:
                logger.error(f"Failed to append UID {uid_str}: {response}")
        
        if new_count == 0:
            logger.info("No new emails found since last check.")

    except Exception as e:
        logger.error(f"Error during transfer: {e}")
        source_conn.disconnect()
        dest_conn.disconnect()

def main():
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

    logger.info("Starting IMAP to Gmail transfer loop with persistent connections...")
    
    try:
        while True:
            transfer_emails(source_conn, dest_conn)
            logger.info(f"Sleeping for {CHECK_INTERVAL} seconds...")
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        logger.info("Stopping script...")
    finally:
        source_conn.disconnect()
        dest_conn.disconnect()

if __name__ == "__main__":
    main()
