"""Rules for staff accounts: how strong a password must be, what a username looks like, when an account locks.
Standard library only. The server (server.py) applies these rules; change a number here to change the rule everywhere.

TO CHANGE A RULE: edit the value below (for example MAX_FAILURES or MIN_LENGTH), then run the tests."""
import re

MIN_LENGTH = 8        # shortest password allowed
MAX_FAILURES = 5      # wrong passwords in a row before an account is locked
LOCK_MINUTES = 15     # how long a locked account stays locked (an admin can unlock it sooner)
ROLES = ("admin", "cashier")  # to add a role, add it here, in db.py (the users table check) and in server.py's api() router

# The accounts a development copy starts with. On a real install they are refused: whoever signs in with one must choose a new password.
DEMO_PASSWORDS = {"admin123", "cashier123"}
COMMON_PASSWORDS = {"password", "password1", "12345678", "123456789", "qwerty123", "11111111", "00000000", "admin1234", "letmein123", "iloveyou1"}
USERNAME = re.compile(r"^[a-z0-9._-]{3,30}$")


def check_password(pw, username=""):
    """Returns a plain-English reason if the password is too weak, or None if it is acceptable."""
    if len(pw) < MIN_LENGTH:
        return f"The password must be at least {MIN_LENGTH} characters long."
    if pw.lower() in COMMON_PASSWORDS or pw.lower() in DEMO_PASSWORDS:
        return "That password is too easy to guess. Please choose another."
    if len(set(pw)) < 4:
        return "That password is too repetitive. Please choose another."
    if username and len(username) >= 3 and username.lower() in pw.lower():
        return "The password must not contain the username."
    return None


def check_username(username):
    """Returns a reason if the username is not allowed, or None. Usernames are lowercase letters, numbers, dot, dash or underscore."""
    if not USERNAME.match(username or ""):
        return "The username must be 3 to 30 characters: lowercase letters, numbers, dot, dash or underscore."
    return None
