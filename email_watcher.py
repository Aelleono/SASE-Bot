import os
import re
import imaplib
import email
from email.header import decode_header
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

# Sender address that Campus Groups sends notifications from.
CAMPUS_GROUPS_SENDER = "ADD_EMAIL_HERE"

# Text that should appear in the subject or body to identify it as a SASE email
# (since other clubs also forward through the same Gmail inbox).
SASE_IDENTIFIER = "Hello SASE Members"


def _decode(value):
    if value is None:
        return ""
    decoded, encoding = decode_header(value)[0]
    if isinstance(decoded, bytes):
        return decoded.decode(encoding or "utf-8", errors="ignore")
    return decoded


# Campus Groups wraps links in tracking redirects (urldefense.com / click
# links) that change on every send. Map known link text to its real,
# permanent destination so the embed points there instead.
LINK_OVERRIDES = {
    "E-Board Application Google Form": "https://docs.google.com/forms/d/12IKXtjzePzJ3FOjDBLxqjW3TP3CCSy6VNd5Z8USyna4",
}


def _linkify(body_text):
    """Converts plain-text 'Link Text<https://example.com>' patterns (the way
    email clients render HTML links in their plain-text part) into Discord
    markdown hyperlinks: [Link Text](https://example.com)."""
    body_text = re.sub(
        r"([^\n<>]+?)<(https?://[^\s>]+)>",
        r"[\1](\2)",
        body_text,
    )

    # Swap in real URLs for known tracking links
    for link_text, real_url in LINK_OVERRIDES.items():
        body_text = re.sub(
            rf"\[{re.escape(link_text)}\]\(https?://[^\s)]+\)",
            f"[{link_text}]({real_url})",
            body_text,
            flags=re.IGNORECASE,
        )

    return body_text


def get_unread_campus_groups_emails():
    """Returns a list of dicts: {subject, body_text, received}"""
    results = []

    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    mail.select("inbox")

    status, message_nums = mail.search(
        None, f'(UNSEEN FROM "{CAMPUS_GROUPS_SENDER}")'
    )

    if status != "OK":
        mail.logout()
        return results

    for num in message_nums[0].split():
        status, msg_data = mail.fetch(num, "(RFC822)")
        if status != "OK":
            continue

        msg = email.message_from_bytes(msg_data[0][1])
        subject = _decode(msg["Subject"])

        body_text = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                disposition = str(part.get("Content-Disposition", ""))

                if "attachment" in disposition:
                    continue

                if content_type == "text/plain":
                    body_text = part.get_payload(decode=True).decode(errors="ignore")
                    break
                elif content_type == "text/html" and not body_text:
                    html = part.get_payload(decode=True).decode(errors="ignore")
                    body_text = BeautifulSoup(html, "html.parser").get_text(separator="\n")
        else:
            content_type = msg.get_content_type()
            payload = msg.get_payload(decode=True)
            if payload:
                decoded = payload.decode(errors="ignore")
                if content_type == "text/html":
                    body_text = BeautifulSoup(decoded, "html.parser").get_text(separator="\n")
                else:
                    body_text = decoded

        body_text = _linkify(body_text)

        # Mark as seen so we don't re-process it next time, regardless of whether it's SASE
        mail.store(num, "+FLAGS", "\\Seen")

        # Only keep emails that look like SASE GBM emails
        if SASE_IDENTIFIER.lower() not in subject.lower() and SASE_IDENTIFIER.lower() not in body_text.lower():
            continue

        results.append({
            "subject": subject,
            "body_text": body_text,
            "received": msg.get("Date"),
        })

    mail.logout()
    return results
