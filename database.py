"""
database.py
Handles SQLite connection, schema creation, and demo data seeding
for the Cricket Turf Booking application.
"""

import sqlite3
import os
from datetime import date, timedelta, datetime
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")


def get_db():
    """Return a new SQLite connection with row access by column name."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turf (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tagline TEXT,
    description TEXT,
    address TEXT,
    contact_number TEXT,
    opening_time TEXT,
    closing_time TEXT,
    facilities TEXT,       -- comma separated
    rules TEXT,
    map_embed TEXT,
    hero_image TEXT
);

CREATE TABLE IF NOT EXISTS pricing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_type TEXT NOT NULL,       -- 'weekday' or 'weekend'
    period TEXT NOT NULL,         -- 'morning' or 'evening'
    price_per_hour INTEGER NOT NULL,
    UNIQUE(day_type, period)
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_code TEXT UNIQUE NOT NULL,
    user_id INTEGER,
    customer_name TEXT NOT NULL,
    mobile TEXT NOT NULL,
    email TEXT,
    players INTEGER,
    note TEXT,
    booking_date TEXT NOT NULL,     -- YYYY-MM-DD
    day_name TEXT NOT NULL,
    start_time TEXT NOT NULL,       -- HH:MM (24h)
    end_time TEXT NOT NULL,
    hours REAL NOT NULL,
    price_per_hour INTEGER NOT NULL,
    total_amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',  -- confirmed / cancelled / completed
    payment_status TEXT NOT NULL DEFAULT 'pay_at_turf',
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS blocked_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    block_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,
    rating INTEGER NOT NULL,
    review_text TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / hidden
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS payment_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    upi_id TEXT NOT NULL DEFAULT 'yourupi@bank',
    upi_display_name TEXT NOT NULL DEFAULT 'GreenStrike Cricket Turf',
    qr_image TEXT,
    advance_percentage REAL NOT NULL DEFAULT 30,
    payment_instructions TEXT
);
"""

# ---------------------------------------------------------------------------
# Payment-related columns added to the `bookings` table.
# Each entry: (column_name, "ALTER TABLE ... DDL fragment")
# Applied via a safe, idempotent migration so an existing database.db
# (created by an older version of this app) gets these columns added
# without losing any existing rows.
# ---------------------------------------------------------------------------
BOOKING_PAYMENT_COLUMNS = [
    ("payment_method", "TEXT NOT NULL DEFAULT 'pay_at_turf'"),
    ("amount_due", "INTEGER"),
    ("amount_paid", "INTEGER NOT NULL DEFAULT 0"),
    ("remaining_amount", "INTEGER"),
    ("advance_percentage", "REAL"),
    ("transaction_id", "TEXT"),
    ("payment_reference", "TEXT"),
    ("paid_at", "TEXT"),
]

# ---------------------------------------------------------------------------
# Email-verification columns added to the `users` table.
# ---------------------------------------------------------------------------
USER_VERIFICATION_COLUMNS = [
    ("email_verified", "INTEGER NOT NULL DEFAULT 0"),
    ("email_verification_token", "TEXT"),
    ("email_verification_sent_at", "TEXT"),
]


def slot_time_list(start="06:00", end="23:00"):
    """Generate hourly slot boundaries between start and end (24h strings)."""
    fmt = "%H:%M"
    t = datetime.strptime(start, fmt)
    end_t = datetime.strptime(end, fmt)
    slots = []
    while t < end_t:
        nxt = t + timedelta(hours=1)
        slots.append((t.strftime(fmt), nxt.strftime(fmt)))
        t = nxt
    return slots


def _column_exists(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def migrate_db(conn):
    """
    Safely add payment-related columns/tables to an EXISTING database
    without touching any existing rows. Idempotent: running this many
    times (e.g. every app start) never duplicates columns or resets data.
    """
    # 1. Add any missing payment columns to bookings.
    for col_name, ddl in BOOKING_PAYMENT_COLUMNS:
        if not _column_exists(conn, "bookings", col_name):
            conn.execute(f"ALTER TABLE bookings ADD COLUMN {col_name} {ddl}")
    conn.commit()

    # 1b. Add any missing email-verification columns to users. Existing
    #     accounts (created before this feature existed) are grandfathered
    #     in as already-verified so nobody gets locked out of an account
    #     they could already log into.
    users_missing_verification = not _column_exists(conn, "users", "email_verified")
    for col_name, ddl in USER_VERIFICATION_COLUMNS:
        if not _column_exists(conn, "users", col_name):
            conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {ddl}")
    if users_missing_verification:
        conn.execute("UPDATE users SET email_verified = 1")
    conn.commit()

    # 1c. Login/registration no longer uses mobile numbers — drop the
    #     `mobile` column from users by rebuilding the table (SQLite can't
    #     drop a column or an existing UNIQUE/NOT NULL constraint via a
    #     plain ALTER TABLE). Existing rows are preserved; if a row has no
    #     email on file, a unique placeholder is generated so the rebuild
    #     doesn't violate the new NOT NULL/UNIQUE constraint on email.
    if _column_exists(conn, "users", "mobile"):
        conn.execute("""
            CREATE TABLE users_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                email_verified INTEGER NOT NULL DEFAULT 0,
                email_verification_token TEXT,
                email_verification_sent_at TEXT
            )
        """)
        old_rows = conn.execute("SELECT * FROM users").fetchall()
        seen_emails = set()
        for row in old_rows:
            email = row["email"] or f"user{row['id']}@no-email.invalid"
            unique_email = email
            suffix = 1
            while unique_email in seen_emails:
                suffix += 1
                name_part, _, domain_part = email.partition("@")
                unique_email = f"{name_part}+{suffix}@{domain_part}"
            seen_emails.add(unique_email)
            conn.execute(
                """INSERT INTO users_new
                   (id, full_name, email, password_hash, created_at,
                    email_verified, email_verification_token, email_verification_sent_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    row["id"], row["full_name"], unique_email, row["password_hash"],
                    row["created_at"], row["email_verified"],
                    row["email_verification_token"], row["email_verification_sent_at"],
                ),
            )
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users_new RENAME TO users")
        conn.commit()

    # 2. Backfill sensible payment values for bookings created before the
    #    payment system existed (amount_due / remaining_amount were NULL).
    conn.execute(
        """UPDATE bookings
           SET amount_due = total_amount
           WHERE amount_due IS NULL"""
    )
    conn.execute(
        """UPDATE bookings
           SET remaining_amount = total_amount - amount_paid
           WHERE remaining_amount IS NULL"""
    )
    conn.commit()

    # 3. Ensure the payment_settings table exists (CREATE TABLE IF NOT
    #    EXISTS in SCHEMA already handles this on every init_db call) and
    #    has exactly one settings row.
    row = conn.execute("SELECT COUNT(*) c FROM payment_settings").fetchone()
    if row["c"] == 0:
        conn.execute(
            """INSERT INTO payment_settings
               (upi_id, upi_display_name, qr_image, advance_percentage, payment_instructions)
               VALUES (?,?,?,?,?)""",
            (
                "yourupi@bank",
                "GreenStrike Cricket Turf",
                None,
                30,
                "Scan the QR code or pay using the UPI ID above, then enter your "
                "transaction/reference ID below. Your payment will be verified by "
                "the turf admin shortly.",
            ),
        )
    conn.commit()


def get_payment_settings(conn):
    """Return the single payment_settings row (creating defaults if missing)."""
    row = conn.execute("SELECT * FROM payment_settings LIMIT 1").fetchone()
    if row is None:
        migrate_db(conn)
        row = conn.execute("SELECT * FROM payment_settings LIMIT 1").fetchone()
    return row


def init_db():
    """Create tables if they don't exist and seed demo data on first run."""
    first_run = not os.path.exists(DB_PATH)
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    migrate_db(conn)

    cur = conn.cursor()

    # Seed admin account
    cur.execute("SELECT COUNT(*) c FROM admins")
    if cur.fetchone()["c"] == 0:
        cur.execute(
            "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
            ("admin", generate_password_hash("admin123")),
        )

    # Seed turf info
    cur.execute("SELECT COUNT(*) c FROM turf")
    if cur.fetchone()["c"] == 0:
        cur.execute(
            """INSERT INTO turf (name, tagline, description, address, contact_number,
               opening_time, closing_time, facilities, rules, map_embed, hero_image)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "GreenStrike Cricket Turf",
                "Play. Compete. Repeat.",
                "GreenStrike Cricket Turf is Mumbai's premier box-cricket destination, "
                "featuring a professional-grade artificial pitch, floodlit night play, "
                "and a premium clubhouse experience. Whether it's a friendly weekend "
                "match or a corporate tournament, our turf is built for serious cricket "
                "lovers.  [DEMO DATA]",
                "Plot 14, Sports Complex Road, Andheri East, Mumbai, Maharashtra",
                "+91 98765 43210",
                "06:00",
                "23:00",
                "Floodlights,Parking,Washroom,Drinking Water,Changing Room,Cricket Nets,Seating Area",
                "No spikes allowed. Footwear must be turf shoes or sports shoes only. "
                "No smoking or alcohol on premises. Damage to nets/mats will be chargeable. "
                "Please vacate the turf 5 minutes before your slot ends. [DEMO DATA]",
                "",
                "",
            ),
        )

    # Seed pricing
    cur.execute("SELECT COUNT(*) c FROM pricing")
    if cur.fetchone()["c"] == 0:
        cur.executemany(
            "INSERT INTO pricing (day_type, period, price_per_hour) VALUES (?,?,?)",
            [
                ("weekday", "morning", 700),
                ("weekday", "evening", 900),
                ("weekend", "morning", 900),
                ("weekend", "evening", 1200),
            ],
        )

    conn.commit()

    # Seed demo bookings & reviews only on very first run
    if first_run:
        _seed_demo_bookings(cur)
        _seed_demo_reviews(cur)
        conn.commit()

    conn.close()


def _seed_demo_bookings(cur):
    today = date.today()
    demo = [
        (0, "18:00", "19:00", "Rahul Sharma", "9876543210"),
        (1, "19:00", "21:00", "Aditya Verma", "9823456712"),
        (-2, "07:00", "08:00", "Priya Nair", "9812345678"),
        (3, "20:00", "22:00", "Karan Mehta", "9898989898"),
    ]
    for offset, st, et, name, mobile in demo:
        d = today + timedelta(days=offset)
        day_name = d.strftime("%A")
        is_weekend = day_name in ("Saturday", "Sunday")
        hour = int(st.split(":")[0])
        period = "morning" if hour < 15 else "evening"
        price = (900 if period == "morning" else 1200) if is_weekend else (700 if period == "morning" else 900)
        h1 = datetime.strptime(st, "%H:%M")
        h2 = datetime.strptime(et, "%H:%M")
        hours = (h2 - h1).seconds / 3600
        total = int(price * hours)
        status = "completed" if offset < 0 else "confirmed"
        code = f"BK-{d.year}-{cur.lastrowid or 0:05d}{abs(offset)}"
        cur.execute(
            """INSERT INTO bookings (booking_code, customer_name, mobile, email, players, note,
               booking_date, day_name, start_time, end_time, hours, price_per_hour, total_amount,
               status, payment_status, payment_method, amount_due, amount_paid, remaining_amount)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                code, name, mobile, None, 10, "Demo booking",
                d.isoformat(), day_name, st, et, hours, price, total,
                status, "pay_at_turf", "pay_at_turf", total, 0, total,
            ),
        )


def _seed_demo_reviews(cur):
    demo_reviews = [
        ("Rahul Sharma", 5, "Excellent turf, great lighting and smooth booking process! [DEMO DATA]", "approved"),
        ("Priya Nair", 4, "Good pitch quality, parking could be a bit bigger. [DEMO DATA]", "approved"),
        ("Karan Mehta", 5, "Best turf in the area for night matches. [DEMO DATA]", "approved"),
        ("Aditya Verma", 3, "Decent experience, washroom needs better maintenance. [DEMO DATA]", "pending"),
    ]
    cur.executemany(
        "INSERT INTO reviews (customer_name, rating, review_text, status) VALUES (?,?,?,?)",
        demo_reviews,
    )


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
