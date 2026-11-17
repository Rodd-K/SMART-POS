"""EMERGENCY TOOL: set a new password for a forgotten admin (or any) account.

Run it ON THE SHOP COMPUTER, in the project folder:
    python3 reset_admin.py            resets the first admin account
    python3 reset_admin.py mary       resets the account with that username

It asks for a new starting password. The person must choose their own password at their next sign-in, and the account is unlocked and
switched on. No old password is needed: anyone who can run this already has the computer, so keep the computer itself secure.
Every use is written to the audit log."""
import getpass
import secrets
import sys

import accounts
import server


def reset(c, username, password):
    """Sets a new starting password. Raises ValueError with a plain message if the account is unknown or the password too weak."""
    r = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not r:
        raise ValueError(f"There is no account called '{username}'.")
    err = accounts.check_password(password, username)
    if err:
        raise ValueError(err)
    s = secrets.token_hex(8)
    c.execute("UPDATE users SET salt=?, hash=?, must_change_password=1, failed_logins=0, locked_until='', active=1 WHERE id=?", (s, server.hp(password, s), r["id"]))
    server.audit(c, {"name": "command line"}, "password reset (command line)", username)


def main():
    c = server.new_conn()
    if len(sys.argv) > 1:
        username = sys.argv[1].strip().lower()
    else:
        first = c.execute("SELECT username FROM users WHERE role='admin' ORDER BY created_at LIMIT 1").fetchone()
        if not first:
            sys.exit("There are no accounts yet. Open the program in the browser to set up the business.")
        username = first["username"]
    print(f"Resetting the password for '{username}'.")
    pw = getpass.getpass("New starting password: ")
    if pw != getpass.getpass("Repeat it: "):
        sys.exit("The two passwords did not match. Nothing was changed.")
    try:
        reset(c, username, pw)
    except ValueError as e:
        sys.exit(str(e))
    print("Done. They will be asked to choose their own password when they sign in.")


if __name__ == "__main__":
    main()
