import os
import imaplib
import email
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

mail = imaplib.IMAP4_SSL("imap.gmail.com")
mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
mail.select("inbox")

status, message_nums = mail.search(None, '(UNSEEN FROM "Alan_Dang@student.uml.edu")')
nums = message_nums[0].split()

for num in nums:
    status, msg_data = mail.fetch(num, "(RFC822)")
    msg = email.message_from_bytes(msg_data[0][1])

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
        payload = msg.get_payload(decode=True)
        if payload:
            body_text = payload.decode(errors="ignore")

    print("=" * 50)
    print(repr(body_text[:500]))

mail.logout()