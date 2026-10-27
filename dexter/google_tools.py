"""Google Workspace tools: Gmail and Google Calendar integrations for LangGraph.

Supports OAuth 2.0 with desktop client credentials (credentials.json) and token caching (token.json).
Destructive operations (sending emails, deleting calendar events) require Human-in-the-Loop approval
via LangGraph's interrupt().
"""

import base64
from datetime import datetime, timezone
from email.mime.text import MIMEText
import os
from typing import Optional

from langchain_core.tools import tool
from langgraph.types import interrupt

# Google API Scopes required for Gmail and Google Calendar
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

DEFAULT_CREDENTIALS_PATH = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
DEFAULT_TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_FILE", "token.json")


def get_google_credentials():
    """Authenticate with Google OAuth 2.0.

    Looks for an existing token.json file. If missing or expired, uses
    credentials.json to prompt local browser authentication, then stores token.json.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise RuntimeError(
            "Missing Google client dependencies. Install with: "
            "uv pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        )

    creds = None
    token_path = os.environ.get("GOOGLE_TOKEN_FILE", DEFAULT_TOKEN_PATH)
    creds_path = os.environ.get("GOOGLE_CREDENTIALS_FILE", DEFAULT_CREDENTIALS_PATH)

    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

        if not creds:
            if not os.path.exists(creds_path):
                raise FileNotFoundError(
                    f"Google OAuth client secrets file '{creds_path}' not found.\n"
                    "To enable Gmail & Google Calendar tools:\n"
                    "1. Go to Google Cloud Console (https://console.cloud.google.com/)\n"
                    "2. Enable Gmail API and Google Calendar API.\n"
                    "3. Create an OAuth 2.0 Client ID (Application type: Desktop App).\n"
                    f"4. Download the JSON file and save it as '{creds_path}' in the project root."
                )

            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)

        # Cache credentials for next run
        with open(token_path, "w", encoding="utf-8") as token_file:
            token_file.write(creds.to_json())

    return creds


def get_gmail_service():
    """Build and return an authorized Gmail API service."""
    from googleapiclient.discovery import build
    creds = get_google_credentials()
    return build("gmail", "v1", credentials=creds)


def get_calendar_service():
    """Build and return an authorized Google Calendar API service."""
    from googleapiclient.discovery import build
    creds = get_google_credentials()
    return build("calendar", "v3", credentials=creds)


# =====================================================================
# Gmail Tools
# =====================================================================

@tool
def gmail_search_emails(query: str, max_results: int = 5) -> str:
    """Search Gmail messages using standard Gmail query syntax.

    Args:
        query: Gmail search query (e.g., 'is:unread', 'from:someone@example.com', 'subject:invoice', 'newer_than:2d').
        max_results: Maximum number of emails to retrieve (default: 5, max: 20).

    Returns:
        A list of matching emails with sender, subject, date, snippet, and message ID.
    """
    try:
        service = get_gmail_service()
        limit = min(max(1, int(max_results)), 20)
        results = service.users().messages().list(userId="me", q=query, maxResults=limit).execute()
        messages = results.get("messages", [])

        if not messages:
            return f"No emails found matching query: '{query}'"

        lines = [f"Found {len(messages)} email(s) for query '{query}':\n"]
        for msg in messages:
            msg_id = msg["id"]
            meta = service.users().messages().get(
                userId="me", id=msg_id, format="metadata",
                metadataHeaders=["Subject", "From", "Date"]
            ).execute()

            headers = {h["name"]: h["value"] for h in meta.get("payload", {}).get("headers", [])}
            subject = headers.get("Subject", "(No Subject)")
            sender = headers.get("From", "(Unknown Sender)")
            date = headers.get("Date", "")
            snippet = meta.get("snippet", "")

            lines.append(
                f"- ID: {msg_id}\n"
                f"  From: {sender}\n"
                f"  Subject: {subject}\n"
                f"  Date: {date}\n"
                f"  Snippet: {snippet}\n"
            )

        return "\n".join(lines).strip()
    except Exception as exc:
        return f"Error searching Gmail: {exc}"


@tool
def gmail_read_email(email_id: str) -> str:
    """Read full email message details and body by Gmail message ID.

    Args:
        email_id: The ID of the Gmail message to read.

    Returns:
        Email headers (From, To, Subject, Date) and extracted plain text body content.
    """
    try:
        service = get_gmail_service()
        msg = service.users().messages().get(userId="me", id=email_id.strip(), format="full").execute()

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("Subject", "(No Subject)")
        sender = headers.get("From", "(Unknown Sender)")
        recipient = headers.get("To", "(Unknown Recipient)")
        date = headers.get("Date", "")

        def _extract_body(payload):
            body_text = ""
            mime_type = payload.get("mimeType", "")

            if mime_type == "text/plain" and "data" in payload.get("body", {}):
                data = payload["body"]["data"]
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

            parts = payload.get("parts", [])
            for part in parts:
                if part.get("mimeType") == "text/plain" and "data" in part.get("body", {}):
                    data = part["body"]["data"]
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                if "parts" in part:
                    sub_body = _extract_body(part)
                    if sub_body:
                        return sub_body

            # Fallback to snippet if plain text payload was not parsed
            return ""

        body = _extract_body(msg.get("payload", {}))
        if not body:
            body = msg.get("snippet", "(No content)")

        return (
            f"Message ID: {email_id}\n"
            f"From: {sender}\n"
            f"To: {recipient}\n"
            f"Subject: {subject}\n"
            f"Date: {date}\n\n"
            f"Body:\n{body.strip()}"
        )
    except Exception as exc:
        return f"Error reading email {email_id}: {exc}"


@tool
def gmail_create_draft(to: str, subject: str, body: str) -> str:
    """Create a draft email in Gmail without sending it.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Email body text.

    Returns:
        Status message with the draft ID.
    """
    try:
        service = get_gmail_service()
        message = MIMEText(body)
        message["to"] = to.strip()
        message["subject"] = subject.strip()

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        draft = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        draft_id = draft.get("id", "unknown")
        return f"Draft created successfully. Draft ID: {draft_id} (Recipient: {to}, Subject: '{subject}')"
    except Exception as exc:
        return f"Error creating draft email: {exc}"


@tool
def gmail_send_email(to: str, subject: str, body: str) -> str:
    """Send an email via Gmail.

    HUMAN-IN-THE-LOOP:
    Before actually sending, this tool interrupts the graph to ask the user
    for approval ('yes' to approve, 'no' to decline).

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Email body text.

    Returns:
        Status message confirming email sent, or notification of cancellation.
    """
    to_clean = to.strip()
    subject_clean = subject.strip()

    # Human-in-the-loop approval step
    prompt_msg = (
        f"Approve sending email?\n"
        f"To: {to_clean}\n"
        f"Subject: {subject_clean}\n"
        f"Body:\n{body.strip()[:300]}"
    )
    decision = interrupt(prompt_msg)

    if not (isinstance(decision, str) and decision.strip().lower() == "yes"):
        return f"Sending email to '{to_clean}' was DECLINED or cancelled by the user."

    try:
        service = get_gmail_service()
        message = MIMEText(body)
        message["to"] = to_clean
        message["subject"] = subject_clean

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        sent_id = sent.get("id", "unknown")
        return f"Email sent successfully to {to_clean}! Message ID: {sent_id}"
    except Exception as exc:
        return f"Error sending email to {to_clean}: {exc}"


# =====================================================================
# Google Calendar Tools
# =====================================================================

@tool
def calendar_list_events(max_results: int = 10, time_min: str = "") -> str:
    """List upcoming Google Calendar events from the user's primary calendar.

    Args:
        max_results: Maximum number of events to return (default: 10, max: 30).
        time_min: ISO 8601 string for start time threshold (e.g., '2026-10-06T00:00:00Z'). Defaults to current UTC time.

    Returns:
        A formatted list of scheduled calendar events including event ID, summary, start, end, location, and description.
    """
    try:
        service = get_calendar_service()
        limit = min(max(1, int(max_results)), 30)

        if not time_min or not time_min.strip():
            time_min = datetime.now(timezone.utc).isoformat()
        else:
            time_min = time_min.strip()

        events_result = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            maxResults=limit,
            singleEvents=True,
            orderBy="startTime"
        ).execute()

        events = events_result.get("items", [])
        if not events:
            return f"No upcoming events found on your calendar starting from {time_min}."

        lines = [f"Upcoming Calendar Events ({len(events)} found):\n"]
        for event in events:
            event_id = event.get("id", "")
            summary = event.get("summary", "(No title)")
            start = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
            end = event.get("end", {}).get("dateTime") or event.get("end", {}).get("date")
            location = event.get("location", "")
            description = event.get("description", "")

            loc_str = f"  Location: {location}\n" if location else ""
            desc_str = f"  Description: {description}\n" if description else ""

            lines.append(
                f"- ID: {event_id}\n"
                f"  Title: {summary}\n"
                f"  Start: {start}\n"
                f"  End: {end}\n"
                f"{loc_str}"
                f"{desc_str}"
            )

        return "\n".join(lines).strip()
    except Exception as exc:
        return f"Error fetching calendar events: {exc}"


@tool
def calendar_create_event(
    summary: str,
    start_time: str,
    end_time: str,
    description: str = "",
    location: str = "",
) -> str:
    """Create a new event on Google Calendar.

    Args:
        summary: Title/name of the calendar event.
        start_time: Start time in ISO 8601 format (e.g. '2026-10-07T14:00:00+05:00' or '2026-10-07T14:00:00Z') or 'YYYY-MM-DD HH:MM'.
        end_time: End time in ISO 8601 format (e.g. '2026-10-07T15:00:00+05:00' or '2026-10-07T15:00:00Z') or 'YYYY-MM-DD HH:MM'.
        description: Optional notes or description for the event.
        location: Optional location or meeting link.

    Returns:
        Status message with confirmation link and event ID.
    """
    def _parse_time(t_str: str) -> dict:
        t = t_str.strip()
        # If it looks like a full date (YYYY-MM-DD), check if time is included
        if "T" in t:
            return {"dateTime": t}
        # If space separated date and time: '2026-10-07 14:00'
        if " " in t:
            date_part, time_part = t.split(" ", 1)
            return {"dateTime": f"{date_part}T{time_part}:00Z"}
        # If bare date '2026-10-07'
        if len(t) == 10 and t.count("-") == 2:
            return {"date": t}
        return {"dateTime": t}

    try:
        service = get_calendar_service()
        event_body = {
            "summary": summary.strip(),
            "start": _parse_time(start_time),
            "end": _parse_time(end_time),
        }
        if description.strip():
            event_body["description"] = description.strip()
        if location.strip():
            event_body["location"] = location.strip()

        event = service.events().insert(calendarId="primary", body=event_body).execute()
        event_id = event.get("id")
        html_link = event.get("htmlLink", "")

        return (
            f"Calendar event '{summary}' created successfully!\n"
            f"Event ID: {event_id}\n"
            f"Link: {html_link}"
        )
    except Exception as exc:
        return f"Error creating calendar event: {exc}"


@tool
def calendar_delete_event(event_id: str, summary: str = "") -> str:
    """Delete an event from Google Calendar by its event ID.

    HUMAN-IN-THE-LOOP:
    Before deleting, this tool interrupts the graph to ask the user for approval ('yes'/'no').

    Args:
        event_id: The ID of the event to delete.
        summary: Optional summary/title of the event for confirmation context.

    Returns:
        Status message confirming deletion or cancellation.
    """
    event_id_clean = event_id.strip()
    title_display = summary.strip() if summary else event_id_clean

    # Human-in-the-loop approval step
    prompt_msg = f"Approve deleting calendar event '{title_display}' (ID: {event_id_clean})? yes/no"
    decision = interrupt(prompt_msg)

    if not (isinstance(decision, str) and decision.strip().lower() == "yes"):
        return f"Deleting calendar event '{title_display}' was DECLINED or cancelled by the user."

    try:
        service = get_calendar_service()
        service.events().delete(calendarId="primary", eventId=event_id_clean).execute()
        return f"Calendar event '{title_display}' (ID: {event_id_clean}) was successfully deleted."
    except Exception as exc:
        return f"Error deleting calendar event {event_id_clean}: {exc}"


GOOGLE_TOOLS = [
    gmail_search_emails,
    gmail_read_email,
    gmail_create_draft,
    gmail_send_email,
    calendar_list_events,
    calendar_create_event,
    calendar_delete_event,
]
