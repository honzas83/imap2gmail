# IMAP to Gmail Transfer Script

A robust Python script that continuously monitors a source IMAP account for new emails and pushes them to a destination Gmail account.

## Features

- **Continuous Monitoring**: Runs in a loop with configurable intervals.
- **Duplicate Prevention**: Uses a local SQLite database to track processed emails by UID and internal timestamp.
- **Smart Startup**: Automatically skips all existing emails on the first run, syncing only new arrivals.
- **Persistent Connections**: Maintains active IMAP sessions to reduce login overhead.
- **Secure**: Uses environment variables for credentials (never hardcoded).
- **Ollama Classification & Filtering**: Optional real-time email classification using a local or remote Ollama model (default: `gemma4:e4b`).
  - Supports structured JSON output using native JSON Schema.
  - Automatically formats prompts with placeholders: `{subject}`, `{from_display}`, `{body}`, `{current_date}`, and `{schema}`.
  - Hot-reloads configuration YAML changes dynamically on the fly.
  - Beautiful colorized, multi-line card logging on the terminal separating email metadata from LLM outputs.

## Prerequisites

- Python 3.12+
- A source IMAP account (e.g., University or Work email)
- A destination Gmail account
  - **Note**: You will likely need a [Gmail App Password](https://myaccount.google.com/apppasswords) if 2FA is enabled.
- (Optional) An active Ollama instance for email classification.

## Installation

1. Clone the repository:
   ```bash
   git clone git@github.com:honzas83/imap2gmail.git
   cd imap2gmail
   ```

2. Create a virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Configure environment variables:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and fill in your server details and credentials.

## Ollama Classification & Filtering Configuration (Optional)

You can enable LLM-based filtering, categorization, and routing of incoming emails by creating a YAML configuration file.

1. Copy the example configuration:
   ```bash
   cp ollama_config.yaml.example ollama_config.yaml
   ```

2. Edit `ollama_config.yaml` to specify your Ollama endpoint, model, prompt, and JSON Schema.

3. Enable it by setting the configuration path in `.env`:
   ```env
   OLLAMA_CONFIG_PATH=ollama_config.yaml
   ```
   Alternatively, pass the config path as a command-line argument:
   ```bash
   .venv/bin/python imap2gmail.py --ollama-config ollama_config.yaml
   ```

### Classification Capabilities
- **Spam Control**: Emails classified as `spam: true` are directly routed to the Gmail `[Gmail]/Spam` folder instead of `INBOX`.
- **Importance Control**: Emails marked as `important: true` receive the `\Important` system label. If marked as `false`, the script will explicitly remove the `\Important` label (override exclusions list still applies).
- **Custom Tags**: Labels specified in the `tags` list output from the LLM are automatically mapped to custom Gmail labels using the `+X-GM-LABELS` extension.

## Usage

Run the script using the virtual environment:

```bash
.venv/bin/python imap2gmail.py
```

The script will log its activity to `stderr` with detailed, colorized formatting. You can stop it anytime with `Ctrl+C`.

## How it works

The script maintains a local SQLite database (`processed.db`) to track:
1. **UID**: Unique identifier of the message on the source server.
2. **Internal Date**: The timestamp when the message was received by the source server.

On the first execution, it sets a "startup marker" at the current time. Any emails received before this marker are ignored. Subsequent checks only query for new UIDs, making the process efficient even for very large inboxes.

## License

MIT
