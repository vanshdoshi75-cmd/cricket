"""
logic.py
Business logic: pricing calculation, slot generation, availability
checking, and booking code generation. Kept separate from routes
so app.py stays readable.
"""

from datetime import datetime, timedelta, date
from decimal import Decimal, ROUND_HALF_UP
import random
import string


def get_day_type(d: date) -> str:
    """Return 'weekend' for Sat/Sun, else 'weekday'."""
    return "weekend" if d.weekday() >= 5 else "weekday"


def get_period(start_time: str) -> str:
    """Morning = before 3 PM, Evening = 3 PM onward. Simple, editable rule."""
    hour = int(start_time.split(":")[0])
    return "morning" if hour < 15 else "evening"


def get_price_map(conn):
    """Return {(day_type, period): price_per_hour} from the pricing table."""
    rows = conn.execute("SELECT day_type, period, price_per_hour FROM pricing").fetchall()
    return {(r["day_type"], r["period"]): r["price_per_hour"] for r in rows}


def price_for_slot(conn, booking_date: date, start_time: str) -> int:
    price_map = get_price_map(conn)
    day_type = get_day_type(booking_date)
    period = get_period(start_time)
    return price_map.get((day_type, period), 0)


def generate_slots(opening_time: str, closing_time: str):
    """Generate a list of (start,end) hourly tuples between opening/closing."""
    fmt = "%H:%M"
    t = datetime.strptime(opening_time, fmt)
    end_t = datetime.strptime(closing_time, fmt)
    slots = []
    while t < end_t:
        nxt = t + timedelta(hours=1)
        if nxt > end_t:
            break
        slots.append((t.strftime(fmt), nxt.strftime(fmt)))
        t = nxt
    return slots


def get_day_availability(conn, booking_date_str: str):
    """
    Build the full slot list for a given date (YYYY-MM-DD) with
    status: available / booked / blocked, and the price for each slot.
    """
    turf = conn.execute("SELECT * FROM turf LIMIT 1").fetchone()
    booking_date = datetime.strptime(booking_date_str, "%Y-%m-%d").date()
    raw_slots = generate_slots(turf["opening_time"], turf["closing_time"])

    booked_rows = conn.execute(
        """SELECT start_time, end_time FROM bookings
           WHERE booking_date = ? AND status = 'confirmed'""",
        (booking_date_str,),
    ).fetchall()

    def overlaps_existing_booking(st, et):
        # A booking can span multiple hours (e.g. 20:00-22:00 stored as one
        # row), so an hourly slot is "booked" if it overlaps ANY confirmed
        # booking's range — not just an exact start/end match.
        for r in booked_rows:
            if st < r["end_time"] and et > r["start_time"]:
                return True
        return False

    blocked_rows = conn.execute(
        "SELECT start_time, end_time FROM blocked_slots WHERE block_date = ?",
        (booking_date_str,),
    ).fetchall()
    blocked_set = {(r["start_time"], r["end_time"]) for r in blocked_rows}

    today = date.today()
    is_past_date = booking_date < today
    now_time = datetime.now().strftime("%H:%M")

    result = []
    for st, et in raw_slots:
        if overlaps_existing_booking(st, et):
            status = "booked"
        elif (st, et) in blocked_set:
            status = "blocked"
        elif is_past_date:
            status = "past"
        elif booking_date == today and st <= now_time:
            status = "past"
        else:
            status = "available"

        price = price_for_slot(conn, booking_date, st)
        result.append({"start": st, "end": et, "status": status, "price": price})

    return result, booking_date.strftime("%A")


def is_range_available(conn, booking_date_str, start_time, end_time):
    """Check every hourly sub-slot between start_time and end_time is available."""
    availability, _ = get_day_availability(conn, booking_date_str)
    lookup = {a["start"]: a for a in availability}

    fmt = "%H:%M"
    t = datetime.strptime(start_time, fmt)
    end_t = datetime.strptime(end_time, fmt)
    while t < end_t:
        st_str = t.strftime(fmt)
        slot = lookup.get(st_str)
        if slot is None or slot["status"] != "available":
            return False
        t += timedelta(hours=1)
    return True


def calculate_total(conn, booking_date_str, start_time, end_time):
    """Sum the price of each hourly slot in range (handles mixed morning/evening)."""
    booking_date = datetime.strptime(booking_date_str, "%Y-%m-%d").date()
    fmt = "%H:%M"
    t = datetime.strptime(start_time, fmt)
    end_t = datetime.strptime(end_time, fmt)
    total = 0
    per_hour_prices = []
    while t < end_t:
        price = price_for_slot(conn, booking_date, t.strftime(fmt))
        total += price
        per_hour_prices.append(price)
        t += timedelta(hours=1)
    hours = (end_t - datetime.strptime(start_time, fmt)).seconds / 3600
    avg_price = int(total / hours) if hours else 0
    return total, hours, avg_price


def compute_payment_split(total_amount, payment_method, advance_percentage):
    """
    Backend-authoritative payment split. NEVER trust an amount coming
    from the browser — always call this with a total_amount that was
    itself produced by calculate_total() on the server.

    Returns (amount_due_now, remaining_amount) as ints (paise-safe via
    Decimal rounding to the nearest rupee, half-up).

    payment_method == "online"      -> full amount due now, 0 remaining
    payment_method == "pay_at_turf" -> advance_percentage% due now,
                                        the rest remains payable at the turf
    """
    total = Decimal(str(total_amount))

    if payment_method == "online":
        amount_due_now = total
    else:
        pct = Decimal(str(advance_percentage))
        amount_due_now = (total * pct / Decimal("100")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        # Never let rounding push the advance above the total.
        if amount_due_now > total:
            amount_due_now = total

    amount_due_now = int(amount_due_now)
    remaining_amount = int(total) - amount_due_now
    if remaining_amount < 0:
        remaining_amount = 0
    return amount_due_now, remaining_amount


def build_upi_deep_link(upi_id, payee_name, amount, note=""):
    """
    Build a standard UPI deep link (upi://pay?...) pre-filled with the
    exact payable amount. Supported by most Indian UPI apps (GPay,
    PhonePe, Paytm, BHIM, etc.) when opened on a phone that has one
    installed. This is a link/intent only — it does NOT itself confirm
    or verify that payment was made.
    """
    from urllib.parse import quote

    params = (
        f"pa={quote(upi_id)}"
        f"&pn={quote(payee_name)}"
        f"&am={quote(str(amount))}"
        f"&cu=INR"
    )
    if note:
        params += f"&tn={quote(note)}"
    return f"upi://pay?{params}"


def generate_booking_code():
    year = date.today().year
    suffix = "".join(random.choices(string.digits, k=5))
    return f"BK-{year}-{suffix}"


def validate_mobile(mobile: str) -> bool:
    mobile = mobile.strip()
    return mobile.isdigit() and len(mobile) == 10 and mobile[0] in "6789"


def validate_email(email: str) -> bool:
    if not email:
        return True
    return "@" in email and "." in email.split("@")[-1]
