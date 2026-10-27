"""Unit tests for google_tools module."""

import unittest
from unittest.mock import patch, MagicMock
from langchain_core.messages import ToolMessage
import google_tools


class GoogleToolsTest(unittest.TestCase):
    def test_tool_registration(self):
        tool_names = [t.name for t in google_tools.GOOGLE_TOOLS]
        self.assertIn("gmail_search_emails", tool_names)
        self.assertIn("gmail_read_email", tool_names)
        self.assertIn("gmail_create_draft", tool_names)
        self.assertIn("gmail_send_email", tool_names)
        self.assertIn("calendar_list_events", tool_names)
        self.assertIn("calendar_create_event", tool_names)
        self.assertIn("calendar_delete_event", tool_names)

    def test_missing_credentials_graceful_error(self):
        # Ensure that invoking calendar_list_events returns a helpful guide, not an uncaught crash
        with patch("os.path.exists", return_value=False):
            result = google_tools.calendar_list_events.invoke({})
            self.assertIn("Google OAuth client secrets file", result)
            self.assertIn("credentials.json", result)

    @patch("google_tools.interrupt")
    def test_gmail_send_email_declined(self, mock_interrupt):
        mock_interrupt.return_value = "no"
        result = google_tools.gmail_send_email.invoke({
            "to": "test@example.com",
            "subject": "Hello",
            "body": "Test message",
        })
        self.assertIn("DECLINED", result)
        mock_interrupt.assert_called_once()

    @patch("google_tools.interrupt")
    def test_calendar_delete_event_declined(self, mock_interrupt):
        mock_interrupt.return_value = "no"
        result = google_tools.calendar_delete_event.invoke({
            "event_id": "event123",
            "summary": "Team Sync",
        })
        self.assertIn("DECLINED", result)
        mock_interrupt.assert_called_once()


if __name__ == "__main__":
    unittest.main()
