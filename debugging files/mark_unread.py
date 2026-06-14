import os
import imaplib
from dotenv import load_dotenv

load_dotenv()

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

mail = imaplib.IMAP4_SSL("imap.gmail.com")
mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
mail.select("inbox")

status, message_nums = mail.search(None, "ALL")
nums = message_nums[0].split()[-3:]

print("Marking as unread:", nums)
for num in nums:
    mail.store(num, "-FLAGS", "\\Seen")
    print(f"Marked {num} as unread")

# Verify the UNSEEN search now finds them
status2, unseen = mail.search(None, '(UNSEEN FROM "Alan_Dang@student.uml.edu")')
print("UNSEEN search result:", unseen)

mail.logout()