"""Safe backup and restore for smartpos.db. Standard library only.

  python3 backup.py                 make a backup now (the newest 14 are kept)
  python3 backup.py list            show your backups
  python3 backup.py restore FILE    restore a backup (stop the server first)

Backups use SQLite's own backup function, so they are complete and consistent even while
the server is running. Copying smartpos.db by hand is NOT safe, because recent sales can still
be waiting in smartpos.db-wal."""
# (See the instructions in the notes at the top of this file.)
import os, socket, sqlite3, sys
from datetime import datetime

import db

# Where backups are kept, and how many of the newest to keep.
DIR = os.environ.get("SMARTPOS_BACKUPS", os.path.join(db.ROOT, "backups"))
KEEP = int(os.environ.get("SMARTPOS_KEEP", "14"))


# Checks a database file for damage. A damaged file gives an error message instead of a crash.
def check(path):
    try:
        c = sqlite3.connect(path)
        try:
            return c.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            c.close()
    except sqlite3.DatabaseError as e:  # a damaged or non-database file
        return str(e)


# Makes a consistent backup using SQLite's own backup function (safe even while the server runs), then verifies it.
def make(prefix="smartpos"):
    if not os.path.exists(db.DB_PATH):
        sys.exit(f"No database found at {db.DB_PATH}")
    os.makedirs(DIR, exist_ok=True)
    dest = os.path.join(DIR, f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.db")
    src, out = sqlite3.connect(db.DB_PATH), sqlite3.connect(dest)
    try:
        src.backup(out)
        out.execute("PRAGMA journal_mode=DELETE")  # makes the backup one self-contained file
    finally:
        out.close()
        src.close()
    if check(dest) != "ok":
        os.remove(dest)
        sys.exit("The backup failed its integrity check and was discarded. Nothing was changed.")
    if prefix == "smartpos":  # safety copies made before a restore are never deleted automatically
        old = sorted(f for f in os.listdir(DIR) if f.startswith("smartpos_") and f.endswith(".db"))[:-KEEP]
        for f in old:
            os.remove(os.path.join(DIR, f))
    return dest


# Prints the backups that exist.
def listing():
    files = sorted(f for f in os.listdir(DIR) if f.endswith(".db")) if os.path.isdir(DIR) else []
    for f in files:
        print(f"{os.path.getsize(os.path.join(DIR, f)) / 1024:8.0f} KB  {f}")
    print(f"{len(files)} backup(s) in {DIR}")


# Is the POS server running? A restore must never happen while it is.
def server_running():
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", int(os.environ.get("SMARTPOS_PORT", "8000"))))
        return True
    except OSError:
        return False
    finally:
        s.close()


# Restores a backup. It refuses if the file is damaged or the server is running, and saves a safety copy of the current data first.
def restore(path):
    if not os.path.exists(path):
        sys.exit(f"Backup file not found: {path}")
    if check(path) != "ok":
        sys.exit("That backup failed its integrity check, so it was not used. Nothing was changed.")
    if server_running():
        sys.exit("The POS server is running. Stop it (Ctrl+C), then run the restore again.")
    live = sqlite3.connect(db.DB_PATH, timeout=1, isolation_level=None)
    try:
        live.execute("BEGIN EXCLUSIVE")
        live.execute("ROLLBACK")
    except sqlite3.OperationalError:
        sys.exit("The database is in use by another program. Close it and try again.")
    print("Safety copy of the current data saved to", make("before_restore"))
    src = sqlite3.connect(path)
    try:
        src.backup(live)
    finally:
        src.close()
    result = live.execute("PRAGMA integrity_check").fetchone()[0]
    live.close()
    print("Restored from", path, "| integrity check:", result)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print("Backup saved to", make())
    elif a[0] == "list":
        listing()
    elif a[0] == "restore" and len(a) == 2:
        restore(a[1])
    else:
        sys.exit(__doc__)
