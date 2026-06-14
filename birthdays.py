import json
import os

BIRTHDAY_FILE = "birthdays.json"


def load_birthdays():
    if not os.path.exists(BIRTHDAY_FILE):
        return {}
    with open(BIRTHDAY_FILE, "r") as f:
        return json.load(f)


def save_birthdays(data):
    with open(BIRTHDAY_FILE, "w") as f:
        json.dump(data, f, indent=2)


def set_birthday(user_id: str, month: int, day: int):
    data = load_birthdays()
    data[user_id] = {"month": month, "day": day}
    save_birthdays(data)


def get_todays_birthdays(month: int, day: int):
    data = load_birthdays()
    return [uid for uid, bday in data.items() if bday["month"] == month and bday["day"] == day]