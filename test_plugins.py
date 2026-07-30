import unittest
from unittest.mock import MagicMock, patch
import email
from email.message import EmailMessage
import os

from imap2gmail import (
    strip_html_tags,
    get_email_body,
    OllamaClassifier,
    LocalSaver,
    resolve_plugin,
    transfer_emails,
    DEFAULT_SCHEMA,
    DEFAULT_PROMPT
)

class TestImap2GmailOllama(unittest.TestCase):
    
    def test_strip_html_tags(self):
        html_input = "<html><body><h1>Hello World</h1><p>This is a <b>test</b>.</p></body></html>"
        expected = "Hello World This is a test ."
        self.assertEqual(strip_html_tags(html_input), expected)

    def test_get_email_body_plain(self):
        msg = EmailMessage()
        msg.set_content("This is plain text content.")
        self.assertEqual(get_email_body(msg), "This is plain text content.")

    def test_get_email_body_html(self):
        msg = EmailMessage()
        msg.set_content("<html><body>HTML only content.</body></html>", subtype='html')
        self.assertEqual(get_email_body(msg), "HTML only content.")

    def test_get_email_body_multipart(self):
        msg = EmailMessage()
        msg.set_content("This is plain text.")
        msg.add_alternative("<html><body>This is HTML alternative.</body></html>", subtype='html')
        # Should extract the text/plain part first
        self.assertEqual(get_email_body(msg), "This is plain text.")

    @patch('requests.post')
    def test_ollama_classifier_success(self, mock_post):
        # Create a mock YAML config
        config_content = """
ollama:
  endpoint: "http://localhost:11434"
  model: "gemma4:e4b"
  username: "testuser"
  password: "testpassword"
classification:
  prompt: "Classify: {subject} on {current_date} using schema {schema}"
  schema:
    type: object
    properties:
      important:
        type: boolean
      spam:
        type: boolean
      tags:
        type: array
        items:
          type: string
"""
        with open('test_config.yaml', 'w') as f:
            f.write(config_content)
        
        try:
            classifier = OllamaClassifier('test_config.yaml')
            self.assertTrue(classifier.enabled)
            self.assertEqual(classifier.endpoint, "http://localhost:11434")
            self.assertEqual(classifier.model, "gemma4:e4b")
            self.assertIsNotNone(classifier.auth)
            
            # Mock Ollama HTTP response
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "message": {
                    "content": '{"important": false, "spam": true, "tags": ["Work", "Receipts"]}'
                }
            }
            mock_post.return_value = mock_resp
            
            res = classifier.classify("Test Subject", "Sender Name <sender@example.com>", "Email Body text.")
            self.assertIsNotNone(res)
            self.assertFalse(res['important'])
            self.assertTrue(res['spam'])
            self.assertEqual(res['tags'], ["Work", "Receipts"])
            
            # Test schema format param and prompt formatting with current_date
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            self.assertEqual(kwargs['json']['format']['type'], 'object')
            self.assertEqual(kwargs['json']['model'], 'gemma4:e4b')
            
            # Verify date and schema formatting are present in the formatted prompt content
            prompt_content = kwargs['json']['messages'][0]['content']
            self.assertTrue(prompt_content.startswith("Classify: Test Subject on 2026-"))
            self.assertIn('"important"', prompt_content)
            
        finally:
            if os.path.exists('test_config.yaml'):
                os.remove('test_config.yaml')

    @patch('requests.post')
    def test_ollama_classifier_fallback(self, mock_post):
        config_content = """
ollama:
  endpoint: "http://localhost:11434"
classification:
  prompt: "Classify: {subject}"
"""
        with open('test_config_fallback.yaml', 'w') as f:
            f.write(config_content)
            
        try:
            classifier = OllamaClassifier('test_config_fallback.yaml')
            self.assertTrue(classifier.enabled)
            self.assertEqual(classifier.model, "gemma4:e4b")  # default model
            self.assertIsNone(classifier.auth)  # no credentials
            
            # Mock failed request
            mock_post.side_effect = Exception("Ollama server down")
            
            res = classifier.classify("Test", "sender@example.com", "body")
            self.assertIsNone(res)  # should return None and log warning instead of crashing
        finally:
            if os.path.exists('test_config_fallback.yaml'):
                os.remove('test_config_fallback.yaml')

    @patch('imap2gmail.get_last_info')
    @patch('imap2gmail.is_processed')
    @patch('imap2gmail.mark_as_processed')
    def test_transfer_emails_spam_and_tags(self, mock_mark, mock_is_processed, mock_get_last):
        # Setup mocks for IMAP connections
        mock_get_last.return_value = ("2026-07-30T00:00:00", 100)
        mock_is_processed.return_value = False
        
        # Source IMAP connection
        source_conn = MagicMock()
        source_imap = MagicMock()
        source_conn.connect.return_value = source_imap
        source_imap.uid.side_effect = [
            ('OK', [b'101']), # search
            ('OK', [(b'FLAGS (\\Seen) INTERNALDATE "30-Jul-2026 08:00:00 +0200"', b'From: test@example.com\r\nSubject: Test\r\n\r\nTest Body')]) # fetch
        ]
        
        # Dest IMAP connection
        dest_conn = MagicMock()
        dest_imap = MagicMock()
        dest_conn.connect.return_value = dest_imap
        
        # Mock append response with APPENDUID
        dest_imap.append.return_value = ('OK', [b'[APPENDUID 12345 999] (Success)'])
        
        # Mock Classifier
        classifier = MagicMock()
        def mock_before_transfer(msg, context):
            context["is_important"] = False
            context["is_spam"] = True
            context["tags"] = ["Urgent", "Finance"]
        classifier.before_transfer = mock_before_transfer
        
        # Run transfer
        transfer_emails(source_conn, dest_conn, plugins=[classifier])
        
        # Verify it appended to [Gmail]/Spam folder
        dest_imap.append.assert_called_once()
        args, kwargs = dest_imap.append.call_args
        self.assertEqual(args[0], '"[Gmail]/Spam"')
        
        # Verify it stored labels/tags on the destination email
        # dest_imap.select should be called with "[Gmail]/Spam"
        dest_imap.select.assert_called_with('"[Gmail]/Spam"')
        
        # dest_imap.uid should be called to remove 'Important' and add new labels
        # 1. Remove Important: uid('STORE', '999', '-X-GM-LABELS', '("\\\\Important")')
        # 2. Add tags: uid('STORE', '999', '+X-GM-LABELS', '("Urgent" "Finance")')
        calls = dest_imap.uid.call_args_list
        # verify store commands were sent
        store_calls = [c[0] for c in calls if c[0][0] == 'STORE']
        self.assertTrue(any('-X-GM-LABELS' in str(c) for c in store_calls))
        self.assertTrue(any('+X-GM-LABELS' in str(c) and '"Urgent"' in str(c) and '"Finance"' in str(c) for c in store_calls))

    @patch('requests.post')
    def test_ollama_classifier_hot_reload(self, mock_post):
        config_path = 'test_config_reload.yaml'
        
        # 1. Write initial configuration
        config_content_1 = """
ollama:
  endpoint: "http://localhost:11434"
  model: "model_one"
classification:
  prompt: "Classify: {subject}"
"""
        with open(config_path, 'w') as f:
            f.write(config_content_1)
        
        try:
            classifier = OllamaClassifier(config_path)
            self.assertEqual(classifier.model, "model_one")
            
            # 2. Update config file content
            config_content_2 = """
ollama:
  endpoint: "http://localhost:11434"
  model: "model_two"
classification:
  prompt: "Classify: {subject}"
"""
            import time
            with open(config_path, 'w') as f:
                f.write(config_content_2)
            
            # Advance modification time manually to bypass filesystem cache/same second speed
            future_time = time.time() + 10
            os.utime(config_path, (future_time, future_time))
            
            # Mock successful response
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "message": {
                    "content": '{"important": true, "spam": false, "tags": []}'
                }
            }
            mock_post.return_value = mock_resp
            
            # Trigger classify which calls reload
            classifier.classify("Test", "sender@example.com", "body")
            
            # Check model is updated
            self.assertEqual(classifier.model, "model_two")
            
        finally:
            if os.path.exists(config_path):
                os.remove(config_path)

class TestImap2GmailLocalSaver(unittest.TestCase):

    def test_sanitize_component(self):
        saver = LocalSaver(None)
        # Test normal characters and space collapse
        self.assertEqual(saver.sanitize_component("A simple test"), "A-simple-test")
        # Test special characters and symbols
        self.assertEqual(saver.sanitize_component("Hello/World!: how? are* you;"), "HelloWorld-how-are-you")
        # Test email addresses and domains
        self.assertEqual(saver.sanitize_component("test.user@domain-name.com"), "test.user@domain-name.com")
        # Test truncation
        self.assertEqual(saver.sanitize_component("a" * 150, max_len=50), "a" * 50)
        # Test empty input fallback
        self.assertEqual(saver.sanitize_component(""), "unnamed")

    def test_local_saver_flat(self):
        config_path = "test_saver_flat.yaml"
        archive_dir = "test_archive_flat"
        config_content = f"""
local_saver:
  enabled: true
  directory: "{archive_dir}"
  structure: "flat"
"""
        with open(config_path, "w") as f:
            f.write(config_content)

        import shutil
        from datetime import datetime

        try:
            saver = LocalSaver(config_path)
            self.assertTrue(saver.enabled)
            self.assertEqual(saver.template, "{subject_clean}.eml")

            # Save test message
            this_ts = datetime(2026, 7, 30, 8, 0, 0)
            saver.save(b"raw email content", this_ts, "Sender Name <sender@example.com>", "Test Flat EML Subject")

            expected_file = os.path.join(archive_dir, "Test-Flat-EML-Subject.eml")
            self.assertTrue(os.path.exists(expected_file))
            with open(expected_file, 'rb') as f:
                self.assertEqual(f.read(), b"raw email content")

            # Save again to check conflict resolution
            saver.save(b"second email content", this_ts, "Sender Name <sender@example.com>", "Test Flat EML Subject")
            conflict_file = os.path.join(archive_dir, "Test-Flat-EML-Subject_1.eml")
            self.assertTrue(os.path.exists(conflict_file))
            with open(conflict_file, 'rb') as f:
                self.assertEqual(f.read(), b"second email content")

        finally:
            if os.path.exists(config_path):
                os.remove(config_path)
            if os.path.exists(archive_dir):
                shutil.rmtree(archive_dir)

    def test_local_saver_none_timestamp(self):
        config_path = "test_saver_none_ts.yaml"
        archive_dir = "test_archive_none_ts"
        config_content = f"""
plugin_class: "LocalSaver"
directory: "{archive_dir}"
template: "{{year}}/{{month}}/{{date}}-{{subject_clean}}.eml"
"""
        with open(config_path, "w") as f:
            f.write(config_content)

        import shutil

        try:
            saver = LocalSaver(config_path)
            self.assertTrue(saver.enabled)

            # Raw email bytes containing a Date header
            raw_email = b"Date: Sat, 30 May 2026 12:00:00 +0200\nSubject: Test None Timestamp\n\nBody"
            saver.save(raw_email, None, "Sender <sender@example.com>", "Test None Timestamp")

            # Check that it extracted the date correctly from the Date header (2026-05-30)
            expected_dir = os.path.join(archive_dir, "2026", "05")
            expected_file = os.path.join(expected_dir, "2026-05-30-Test-None-Timestamp.eml")
            self.assertTrue(os.path.exists(expected_file))
            with open(expected_file, 'rb') as f:
                self.assertEqual(f.read(), raw_email)

        finally:
            if os.path.exists(config_path):
                os.remove(config_path)
            if os.path.exists(archive_dir):
                shutil.rmtree(archive_dir)

    def test_local_saver_structured(self):
        config_path = "test_saver_struct.yaml"
        archive_dir = "test_archive_struct"
        config_content = f"""
local_saver:
  enabled: true
  directory: "{archive_dir}"
  structure: "structured"
"""
        with open(config_path, "w") as f:
            f.write(config_content)

        import shutil
        from datetime import datetime

        try:
            saver = LocalSaver(config_path)
            self.assertTrue(saver.enabled)
            self.assertEqual(saver.template, "{year}/{month}/{date}-{from_clean}-{subject_clean}.eml")

            # Save test message 1: Text in from header
            this_ts = datetime(2026, 7, 30, 8, 0, 0)
            saver.save(b"raw structured 1", this_ts, "John Doe <john@example.com>", "Structured Subject One")

            expected_dir = os.path.join(archive_dir, "2026", "07")
            expected_file = os.path.join(expected_dir, "2026-07-30-John-Doe-Structured-Subject-One.eml")
            self.assertTrue(os.path.exists(expected_file))
            with open(expected_file, 'rb') as f:
                self.assertEqual(f.read(), b"raw structured 1")

            # Save test message 2: Email only in from header
            saver.save(b"raw structured 2", this_ts, "only-email@example.com", "Structured Subject Two")
            expected_file_2 = os.path.join(expected_dir, "2026-07-30-only-email@example.com-Structured-Subject-Two.eml")
            self.assertTrue(os.path.exists(expected_file_2))
            with open(expected_file_2, 'rb') as f:
                self.assertEqual(f.read(), b"raw structured 2")

        finally:
            if os.path.exists(config_path):
                os.remove(config_path)
            if os.path.exists(archive_dir):
                shutil.rmtree(archive_dir)

    def test_local_saver_from_short(self):
        config_path = "test_saver_short.yaml"
        archive_dir = "test_archive_short"
        config_content = f"""
plugin_class: "LocalSaver"
directory: "{archive_dir}"
template: "{{year}}/{{month}}/{{date}}-{{from_short}}-{{subject_clean}}.eml"
"""
        with open(config_path, "w") as f:
            f.write(config_content)

        import shutil
        from datetime import datetime

        try:
            saver = LocalSaver(config_path)
            self.assertTrue(saver.enabled)
            self.assertEqual(saver.template, "{year}/{month}/{date}-{from_short}-{subject_clean}.eml")

            # Save test message
            this_ts = datetime(2026, 7, 30, 8, 0, 0)
            saver.save(b"raw structured short", this_ts, "John Doe <john@example.com>", "Structured Subject Short")

            expected_dir = os.path.join(archive_dir, "2026", "07")
            expected_file = os.path.join(expected_dir, "2026-07-30-john@example.com-Structured-Subject-Short.eml")
            self.assertTrue(os.path.exists(expected_file))
            with open(expected_file, 'rb') as f:
                self.assertEqual(f.read(), b"raw structured short")

        finally:
            if os.path.exists(config_path):
                os.remove(config_path)
            if os.path.exists(archive_dir):
                shutil.rmtree(archive_dir)

class TestImap2GmailPlugins(unittest.TestCase):

    def test_resolve_plugin_ollama(self):
        config_path = "test_plugin_ollama.yaml"
        config_content = """
plugin_class: "OllamaClassifier"
endpoint: "http://localhost:11434"
model: "test-model"
"""
        with open(config_path, "w") as f:
            f.write(config_content)
        try:
            plugin = resolve_plugin(config_path)
            self.assertIsInstance(plugin, OllamaClassifier)
            self.assertEqual(plugin.model, "test-model")
        finally:
            if os.path.exists(config_path):
                os.remove(config_path)

    def test_resolve_plugin_local_saver(self):
        config_path = "test_plugin_saver.yaml"
        config_content = """
plugin_class: "LocalSaver"
directory: "./test_dir"
template: "my-template.eml"
"""
        with open(config_path, "w") as f:
            f.write(config_content)
        try:
            plugin = resolve_plugin(config_path)
            self.assertIsInstance(plugin, LocalSaver)
            self.assertEqual(plugin.template, "my-template.eml")
        finally:
            if os.path.exists(config_path):
                os.remove(config_path)

    def test_resolve_plugin_legacy_compatibility(self):
        config_path = "test_plugin_legacy.yaml"
        config_content = """
ollama:
  endpoint: "http://localhost:11434"
local_saver:
  enabled: true
  directory: "./archive"
"""
        with open(config_path, "w") as f:
            f.write(config_content)
        try:
            plugins = resolve_plugin(config_path)
            self.assertIsInstance(plugins, list)
            self.assertEqual(len(plugins), 2)
            self.assertIsInstance(plugins[0], OllamaClassifier)
            self.assertIsInstance(plugins[1], LocalSaver)
        finally:
            if os.path.exists(config_path):
                os.remove(config_path)

if __name__ == '__main__':
    unittest.main()
