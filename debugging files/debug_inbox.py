import os
import imaplib
import email
from email.header import decode_header
from dotenv import load_dotenv

load_dotenv()

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

mail = imaplib.IMAP4_SSL("imap.gmail.com")
mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
mail.select("inbox")

status, message_nums = mail.search(None, "ALL")
nums = message_nums[0].split()[-5:]  # last 5 emails

for num in nums:
    status, msg_data = mail.fetch(num, "(RFC822 FLAGS)")
    msg = email.message_from_bytes(msg_data[0][1])

    subject = decode_header(msg["Subject"])[0][0]
    if isinstance(subject, bytes):
        subject = subject.decode(errors="ignore")

    status2, flag_data = mail.fetch(num, "(FLAGS)")

    print("=" * 50)
    print("Subject:", subject)
    print("From:", msg["From"])
    print("To:", msg["To"])
    print("Flags:", flag_data)

mail.logout()