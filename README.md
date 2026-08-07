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
  - Automatically formats prompts with placeholders: `{subject}`, `{from_display}`, `{to_display}`, `{cc_display}`, `{recipients}`, `{body}`, `{current_date}`, and `{schema}`.
  - Supports an optional two-stage pipeline: cleans up previous message history (quotes/forwards) before executing classification.
  - Hot-reloads configuration YAML changes dynamically on the fly.
  - Beautiful colorized, multi-line card logging on the terminal separating email metadata from LLM outputs.

## Prerequisites

- Python 3.12+
- A source IMAP account
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

## Plugin Architecture (Optional)

`imap2gmail.py` supports a flexible plugin system. Plugins can hook into the email transfer lifecycle:
1. `before_transfer(msg, context)`: Can inspect or modify properties (like `is_spam`, `tags`, `is_important`, `dest_folder`) before the email is copied.
2. `after_transfer(msg, context)`: Runs after the email is successfully appended to Gmail (useful for archiving, local saves, notifications, etc.).

Active plugins are configured in a comma-separated list in `.env`:
```env
PLUGINS=ollama_config.yaml,local_saver_config.yaml
```
Or passed via the `--plugins` command-line argument:
```bash
.venv/bin/python imap2gmail.py --plugins ollama_config.yaml,local_saver_config.yaml
```

Each plugin configuration YAML must define a `plugin_class` key specifying the class name:
- **Ollama Classification & Filtering**: `plugin_class: "OllamaClassifier"`
- **Local EML Saver**: `plugin_class: "LocalSaver"`

*(Backward compatibility: The legacy `--config` / `--ollama-config` parameters and single-file YAML layouts are still fully supported.)*

### 1. Ollama Classification & Filtering
Enables LLM-based filtering, categorization, and routing of incoming emails. See `ollama_config.yaml.example` for details.
- **Two-Stage Processing (Cleanup & Classification)**: To prevent context bloat and improve classification accuracy, you can enable a pre-classification cleanup phase. The LLM is first queried with a shorter `cleanup_prompt` to extract only the clean, newly written message text, stripping out previous thread history (quoted replies/forwards) while preserving the current signatures, greetings, and disclaimers. The classification prompt is then executed on the resulting cleaned message body.
- **Configurable Cleanup**: Set `cleanup_prompt: true` to use the default cleanup prompt, specify a custom prompt string, or omit/set it to `false` to disable the cleanup stage.
- **Configurable Context Size**: Control the maximum character limit of the email body text sent to Ollama using `max_body_chars` (defaults to `8192` characters).
- **Spam Control**: Emails classified as `spam: true` are directly routed to the Gmail `[Gmail]/Spam` folder instead of `INBOX`.
- **Importance Control**: Emails marked as `important: true` receive the `\Important` system label. If marked as `false`, the script will explicitly remove the `\Important` label (override exclusions list still applies).
- **Custom Tags**: Labels specified in the `tags` list output from the LLM are automatically mapped to custom Gmail labels using the `+X-GM-LABELS` extension.

### 2. Local EML Saver
Archives transferred emails as `.eml` files locally using a template-driven layout.
Specify the target `directory` and a custom path `template` in your YAML config:
```yaml
plugin_class: "LocalSaver"
directory: "./archive"
# Supports {year}, {month}, {day}, {date}, {from_clean}, and {subject_clean}
template: "{year}/{month}/{date}-{from_clean}-{subject_clean}.eml"
```
Filename conflicts are resolved automatically by appending `_1`, `_2`, etc. to the filename base before the extension.


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

This project is licensed under the terms of the **MIT License**.

### Summary of Terms

*   **Permissions**: You are free to use, modify, distribute, sublicense, and sell the software for both personal and commercial purposes.
*   **Conditions**: The original copyright notice and this permission notice must be included in all copies or substantial portions of the Software.
*   **Warranty**: The software is provided "as is", without warranty of any kind, express or implied.

See the [LICENSE](file:///Users/honzas/Prga/imap2gmail/LICENSE) file for the full license text.
