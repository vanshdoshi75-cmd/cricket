"""
app.py
Cricket Turf Booking Website — Flask backend.
Run with:  python app.py
Then open: http://127.0.0.1:5000
"""

import os
import time
import io
import secrets
import qrcode
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, send_file, g
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

import database
import logic
import mailer
import receipt
from excel_export import export_bookings_to_excel

app = Flask(__name__)
app.secret_key = os.environ.get("TURF_SECRET_KEY", "dev-secret-key-change-in-production-8843")

# ---------------------------------------------------------------------------
# Payment settings — QR image upload
# ---------------------------------------------------------------------------
ALLOWED_QR_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
QR_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "images")

# Payment statuses that are still awaiting a fresh transaction reference
# from the customer (i.e. it is safe to (re)submit one).
RESUBMITTABLE_PAYMENT_STATUSES = ("pending", "advance_pending", "payment_failed")

# How long an email-verification link stays valid before requiring a resend.
EMAIL_VERIFICATION_EXPIRY_HOURS = 24


def _send_verification_email(user_email, user_name, token):
    verify_link = url_for("verify_email", token=token, _external=True)
    body = (
        f"Hi {user_name},\n\n"
        f"Please verify your email address to activate your account:\n"
        f"{verify_link}\n\n"
        f"This link expires in {EMAIL_VERIFICATION_EXPIRY_HOURS} hours. "
        f"If you didn't create this account, you can ignore this email.\n"
    )
    mailer.send_email(user_email, "Verify your email — GreenStrike Cricket Turf", body)


# ---------------------------------------------------------------------------
# Database connection lifecycle
# ---------------------------------------------------------------------------

def get_conn():
    if "db_conn" not in g:
        g.db_conn = database.get_db()
    return g.db_conn


@app.teardown_appcontext
def close_conn(exception=None):
    conn = g.pop("db_conn", None)
    if conn is not None:
        conn.close()


@app.context_processor
def inject_turf():
    """Make turf info available in every template automatically."""
    conn = get_conn()
    turf = conn.execute("SELECT * FROM turf LIMIT 1").fetchone()
    return {"turf": turf, "current_user": session.get("user_name")}


def qr_image_url(payment_settings):
    """Build a cache-busted static URL for the configured QR image, or None."""
    if not payment_settings or not payment_settings["qr_image"]:
        return None
    full_path = os.path.join(QR_UPLOAD_DIR, os.path.basename(payment_settings["qr_image"]))
    version = int(os.path.getmtime(full_path)) if os.path.exists(full_path) else 0
    return url_for("static", filename=f"images/{payment_settings['qr_image']}", v=version)

def generate_dynamic_qr(upi_link):
    """Generate a fresh QR code from the UPI payment link."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(upi_link)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    return buffer


# ---------------------------------------------------------------------------
# Auth helpers / decorators
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "error")
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapped


def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# CUSTOMER ROUTES
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    conn = get_conn()
    reviews = conn.execute(
        "SELECT * FROM reviews WHERE status = 'approved' ORDER BY created_at DESC LIMIT 6"
    ).fetchall()
    pricing_rows = conn.execute("SELECT * FROM pricing").fetchall()
    pricing = {(r["day_type"], r["period"]): r["price_per_hour"] for r in pricing_rows}

    avg_rating = 0
    if reviews:
        approved_all = conn.execute(
            "SELECT rating FROM reviews WHERE status='approved'"
        ).fetchall()
        avg_rating = round(sum(r["rating"] for r in approved_all) / len(approved_all), 1)

    review_count = conn.execute("SELECT COUNT(*) c FROM reviews WHERE status='approved'").fetchone()["c"]

    return render_template(
        "index.html", reviews=reviews, pricing=pricing,
        avg_rating=avg_rating, review_count=review_count,
    )


@app.route("/turf")
def turf_details():
    conn = get_conn()
    pricing_rows = conn.execute("SELECT * FROM pricing").fetchall()
    pricing = {(r["day_type"], r["period"]): r["price_per_hour"] for r in pricing_rows}
    return render_template("turf.html", pricing=pricing)


@app.route("/booking")
def booking_page():
    selected_date = request.args.get("date") or date.today().isoformat()
    conn = get_conn()
    availability, day_name = logic.get_day_availability(conn, selected_date)
    settings = database.get_payment_settings(conn)
    return render_template(
        "booking.html",
        selected_date=selected_date,
        day_name=day_name,
        availability=availability,
        today=date.today().isoformat(),
        max_date=(date.today() + timedelta(days=60)).isoformat(),
        advance_percentage=settings["advance_percentage"],
    )


@app.route("/api/availability")
def api_availability():
    selected_date = request.args.get("date")
    if not selected_date:
        return jsonify({"error": "date is required"}), 400
    conn = get_conn()
    try:
        availability, day_name = logic.get_day_availability(conn, selected_date)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
    return jsonify({"date": selected_date, "day_name": day_name, "slots": availability})


@app.route("/api/quote")
def api_quote():
    selected_date = request.args.get("date")
    start_time = request.args.get("start")
    end_time = request.args.get("end")
    if not (selected_date and start_time and end_time):
        return jsonify({"error": "missing params"}), 400
    conn = get_conn()
    if not logic.is_range_available(conn, selected_date, start_time, end_time):
        return jsonify({"available": False}), 200
    total, hours, avg_price = logic.calculate_total(conn, selected_date, start_time, end_time)
    return jsonify({
        "available": True, "total": total, "hours": hours,
        "avg_price_per_hour": avg_price,
    })


@app.route("/book/confirm", methods=["POST"])
def confirm_booking():
    conn = get_conn()
    data = request.form

    booking_date = data.get("date", "")
    start_time = data.get("start", "")
    end_time = data.get("end", "")
    name = data.get("name", "").strip()
    mobile = data.get("mobile", "").strip()
    email = data.get("email", "").strip()
    players = data.get("players", "").strip()
    note = data.get("note", "").strip()
    payment_method = data.get("payment_method", "pay_at_turf").strip()
    if payment_method not in ("online", "pay_at_turf"):
        payment_method = "pay_at_turf"

    errors = []
    try:
        d = datetime.strptime(booking_date, "%Y-%m-%d").date()
        if d < date.today():
            errors.append("Booking date cannot be in the past.")
    except ValueError:
        errors.append("Invalid date selected.")

    if not name:
        errors.append("Full name is required.")
    if not logic.validate_mobile(mobile):
        errors.append("Enter a valid 10-digit Indian mobile number.")
    if email and not logic.validate_email(email):
        errors.append("Enter a valid email address.")
    if not start_time or not end_time:
        errors.append("Please select a valid time slot.")

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("booking_page", date=booking_date))

    # Re-check availability at booking time (prevents duplicate booking race)
    if not logic.is_range_available(conn, booking_date, start_time, end_time):
        flash("Sorry, this slot has just been booked. Please select another slot.", "error")
        return redirect(url_for("booking_page", date=booking_date))

    # Backend is the ONLY source of truth for the booking total — the
    # amount is never accepted from the browser/form.
    total, hours, avg_price = logic.calculate_total(conn, booking_date, start_time, end_time)
    day_name = d.strftime("%A")
    code = logic.generate_booking_code()

    settings = database.get_payment_settings(conn)
    advance_percentage = settings["advance_percentage"]
    amount_due_now, remaining_amount = logic.compute_payment_split(
        total, payment_method, advance_percentage
    )
    initial_payment_status = "pending" if payment_method == "online" else "advance_pending"

    cur = conn.cursor()
    cur.execute(
        """INSERT INTO bookings
           (booking_code, user_id, customer_name, mobile, email, players, note,
            booking_date, day_name, start_time, end_time, hours, price_per_hour,
            total_amount, status, payment_status, payment_method, amount_due,
            amount_paid, remaining_amount, advance_percentage)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            code, session.get("user_id"), name, mobile, email or None,
            int(players) if players.isdigit() else None, note or None,
            booking_date, day_name, start_time, end_time, hours, avg_price,
            total, "confirmed", initial_payment_status, payment_method,
            amount_due_now, 0, remaining_amount, advance_percentage,
        ),
    )
    conn.commit()
    return redirect(url_for("booking_payment", code=code))


@app.route("/booking/confirmation/<code>")
def booking_confirmation(code):
    conn = get_conn()
    booking = conn.execute("SELECT * FROM bookings WHERE booking_code = ?", (code,)).fetchone()
    if not booking:
        flash("Booking not found.", "error")
        return redirect(url_for("index"))
    return render_template("confirmation.html", booking=booking)


@app.route("/booking/receipt/<code>")
def booking_receipt(code):
    """
    Downloadable PDF receipt for a booking. Accessible the same way the
    confirmation/payment pages already are — via the booking code, which
    is how guest (non-logged-in) customers reach their own booking. If
    the booking belongs to a logged-in account, it's also reachable from
    My Bookings.
    """
    conn = get_conn()
    booking = conn.execute("SELECT * FROM bookings WHERE booking_code = ?", (code,)).fetchone()
    if not booking:
        flash("Booking not found.", "error")
        return redirect(url_for("index"))

    turf = conn.execute("SELECT * FROM turf LIMIT 1").fetchone()
    pdf_buffer = receipt.generate_receipt_pdf(booking, turf)
    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"receipt_{booking['booking_code']}.pdf",
    )


# --- Payment ------------------------------------------------------------

@app.route("/booking/payment/<code>")
def booking_payment(code):
    conn = get_conn()
    booking = conn.execute("SELECT * FROM bookings WHERE booking_code = ?", (code,)).fetchone()
    if not booking:
        flash("Booking not found.", "error")
        return redirect(url_for("index"))

    settings = dict(database.get_payment_settings(conn))

    upi_link = logic.build_upi_deep_link(
        settings["upi_id"], settings["upi_display_name"],
        booking["amount_due"], note=f"Turf booking {booking['booking_code']}",
    )
    can_submit = booking["payment_status"] in RESUBMITTABLE_PAYMENT_STATUSES
    return render_template(
        "payment.html",
        booking=booking,
        settings=settings,
        qr_url=url_for("dynamic_payment_qr", code=code),
        upi_link=upi_link,
        can_submit=can_submit,
    )


@app.route("/booking/payment/<code>/qr.png")
def dynamic_payment_qr(code):
    conn = get_conn()
    booking = conn.execute("SELECT * FROM bookings WHERE booking_code = ?", (code,)).fetchone()
    if not booking:
        return "Booking not found", 404

    settings = database.get_payment_settings(conn)
    upi_link = logic.build_upi_deep_link(
        settings["upi_id"], settings["upi_display_name"],
        booking["amount_due"], note=f"Turf booking {booking['booking_code']}",
    )

    qr = generate_dynamic_qr(upi_link)
    return send_file(
        qr,
        mimetype="image/png",
        download_name=f"{booking['booking_code']}.png",
    )


@app.route("/booking/payment/<code>/cancel", methods=["POST"])
def cancel_pending_booking(code):
    """
    Let a customer cancel their own booking while payment hasn't been
    verified yet. This frees the slot immediately — cancelling here sets
    status='cancelled', and get_day_availability() only ever treats
    status='confirmed' rows as occupying a slot, so the next availability
    check (even the very next request) will show it as available again.
    """
    conn = get_conn()
    booking = conn.execute("SELECT * FROM bookings WHERE booking_code = ?", (code,)).fetchone()
    if not booking:
        flash("Booking not found.", "error")
        return redirect(url_for("index"))

    if booking["status"] == "cancelled":
        flash("This booking is already cancelled.", "success")
        return redirect(url_for("index"))

    if booking["payment_status"] in ("paid", "advance_paid"):
        flash(
            "Payment for this booking has already been verified. "
            "Please contact the turf directly to cancel.",
            "error",
        )
        return redirect(url_for("booking_payment", code=code))

    conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking["id"],))
    conn.commit()
    flash("Booking cancelled. The slot has been released and is available again.", "success")
    return redirect(url_for("index"))


@app.route("/booking/payment/<code>/submit", methods=["POST"])
def submit_payment_reference(code):
    conn = get_conn()
    booking = conn.execute("SELECT * FROM bookings WHERE booking_code = ?", (code,)).fetchone()
    if not booking:
        flash("Booking not found.", "error")
        return redirect(url_for("index"))

    if booking["payment_status"] not in RESUBMITTABLE_PAYMENT_STATUSES:
        flash("A payment reference has already been submitted for this booking.", "error")
        return redirect(url_for("booking_payment", code=code))

    transaction_id = request.form.get("transaction_id", "").strip()
    if not transaction_id or len(transaction_id) < 3:
        flash("Please enter a valid transaction/reference ID.", "error")
        return redirect(url_for("booking_payment", code=code))
    if len(transaction_id) > 100:
        transaction_id = transaction_id[:100]

    conn.execute(
        """UPDATE bookings SET transaction_id = ?, payment_reference = ?,
           payment_status = 'payment_verification_pending' WHERE id = ?""",
        (transaction_id, transaction_id, booking["id"]),
    )
    conn.commit()
    flash(
        "Payment details submitted. Your payment is pending verification by the turf admin.",
        "success",
    )
    return redirect(url_for("booking_confirmation", code=code))


# --- Auth: customer ---------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        conn = get_conn()
        name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        errors = []
        if not name:
            errors.append("Full name is required.")
        if not email:
            errors.append("Email is required.")
        elif not logic.validate_email(email):
            errors.append("Enter a valid email address.")
        if len(password) < 6:
            errors.append("Password must be at least 6 characters.")

        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            errors.append("An account with this email already exists.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("register.html")

        token = secrets.token_urlsafe(32)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO users
               (full_name, email, password_hash, email_verified,
                email_verification_token, email_verification_sent_at)
               VALUES (?,?,?,0,?,?)""",
            (name, email, generate_password_hash(password), token, now),
        )
        conn.commit()
        _send_verification_email(email, name, token)
        flash(
            "Registration successful! We've sent a verification link to your "
            "email — please verify it before logging in.",
            "success",
        )
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/verify-email/<token>")
def verify_email(token):
    conn = get_conn()
    user = conn.execute(
        "SELECT * FROM users WHERE email_verification_token = ?", (token,)
    ).fetchone()
    if not user:
        flash("Invalid or already-used verification link.", "error")
        return redirect(url_for("login"))

    sent_at = user["email_verification_sent_at"]
    if sent_at:
        sent_dt = datetime.strptime(sent_at, "%Y-%m-%d %H:%M:%S")
        if datetime.now() - sent_dt > timedelta(hours=EMAIL_VERIFICATION_EXPIRY_HOURS):
            flash("This verification link has expired. Please request a new one.", "error")
            return redirect(url_for("resend_verification", email=user["email"]))

    conn.execute(
        """UPDATE users SET email_verified = 1, email_verification_token = NULL
           WHERE id = ?""",
        (user["id"],),
    )
    conn.commit()
    flash("Email verified! You can now log in.", "success")
    return redirect(url_for("login"))


@app.route("/resend-verification", methods=["GET", "POST"])
def resend_verification():
    if request.method == "POST":
        conn = get_conn()
        email = request.form.get("email", "").strip()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        # Same message whether or not the account exists / is already
        # verified, so this can't be used to probe which emails are
        # registered.
        generic_msg = (
            "If that account exists and isn't verified yet, we've sent a "
            "new verification email."
        )
        if user and not user["email_verified"]:
            token = secrets.token_urlsafe(32)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """UPDATE users SET email_verification_token = ?,
                   email_verification_sent_at = ? WHERE id = ?""",
                (token, now, user["id"]),
            )
            conn.commit()
            _send_verification_email(user["email"], user["full_name"], token)
        flash(generic_msg, "success")
        return redirect(url_for("login"))

    prefill_email = request.args.get("email", "")
    return render_template("resend_verification.html", prefill_email=prefill_email)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        conn = get_conn()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            if not user["email_verified"]:
                flash(
                    "Please verify your email before logging in. "
                    "Check your inbox, or request a new link below.",
                    "error",
                )
                return redirect(url_for("resend_verification", email=email))
            session["user_id"] = user["id"]
            session["user_name"] = user["full_name"]
            flash(f"Welcome back, {user['full_name']}!", "success")
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    session.pop("user_name", None)
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


@app.route("/my-bookings")
@login_required
def my_bookings():
    conn = get_conn()
    today = date.today().isoformat()
    upcoming = conn.execute(
        """SELECT * FROM bookings WHERE user_id = ? AND booking_date >= ? AND status != 'cancelled'
           ORDER BY booking_date ASC, start_time ASC""",
        (session["user_id"], today),
    ).fetchall()
    previous = conn.execute(
        """SELECT * FROM bookings WHERE user_id = ? AND (booking_date < ? OR status = 'cancelled')
           ORDER BY booking_date DESC, start_time DESC""",
        (session["user_id"], today),
    ).fetchall()
    return render_template("my_bookings.html", upcoming=upcoming, previous=previous)


@app.route("/my-bookings/cancel/<int:booking_id>", methods=["POST"])
@login_required
def cancel_booking(booking_id):
    conn = get_conn()
    booking = conn.execute(
        "SELECT * FROM bookings WHERE id = ? AND user_id = ?", (booking_id, session["user_id"])
    ).fetchone()
    if not booking:
        flash("Booking not found.", "error")
        return redirect(url_for("my_bookings"))

    b_date = datetime.strptime(booking["booking_date"], "%Y-%m-%d").date()
    if b_date < date.today():
        flash("Past bookings cannot be cancelled.", "error")
        return redirect(url_for("my_bookings"))

    conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
    conn.commit()
    flash("Booking cancelled successfully.", "success")
    return redirect(url_for("my_bookings"))


# --- Reviews -----------------------------------------------------------------

@app.route("/reviews/submit", methods=["POST"])
def submit_review():
    conn = get_conn()
    name = request.form.get("name", "").strip()
    rating = request.form.get("rating", "")
    text = request.form.get("review_text", "").strip()

    if not name or rating not in ("1", "2", "3", "4", "5"):
        flash("Please provide your name and a rating between 1 and 5.", "error")
        return redirect(url_for("index") + "#reviews")

    conn.execute(
        "INSERT INTO reviews (customer_name, rating, review_text, status) VALUES (?,?,?,?)",
        (name, int(rating), text or None, "pending"),
    )
    conn.commit()
    flash("Thank you! Your review has been submitted and is awaiting approval.", "success")
    return redirect(url_for("index") + "#reviews")


# ---------------------------------------------------------------------------
# ADMIN ROUTES
# ---------------------------------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        conn = get_conn()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        admin = conn.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
        if admin and check_password_hash(admin["password_hash"], password):
            session["admin_id"] = admin["id"]
            session["admin_username"] = admin["username"]
            return redirect(url_for("admin_dashboard"))
        flash("Invalid admin credentials.", "error")
    return render_template("admin/login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_id", None)
    session.pop("admin_username", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_conn()
    today = date.today().isoformat()
    week_start = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    month_start = date.today().replace(day=1).isoformat()

    def revenue_since(start_date):
        row = conn.execute(
            """SELECT COALESCE(SUM(total_amount),0) s FROM bookings
               WHERE booking_date >= ? AND status != 'cancelled'""",
            (start_date,),
        ).fetchone()
        return row["s"]

    stats = {
        "today_bookings": conn.execute(
            "SELECT COUNT(*) c FROM bookings WHERE booking_date = ? AND status != 'cancelled'", (today,)
        ).fetchone()["c"],
        "upcoming_bookings": conn.execute(
            "SELECT COUNT(*) c FROM bookings WHERE booking_date > ? AND status != 'cancelled'", (today,)
        ).fetchone()["c"],
        "today_revenue": revenue_since(today),
        "weekly_revenue": revenue_since(week_start),
        "monthly_revenue": revenue_since(month_start),
        "total_bookings": conn.execute("SELECT COUNT(*) c FROM bookings").fetchone()["c"],
        "cancelled_bookings": conn.execute(
            "SELECT COUNT(*) c FROM bookings WHERE status = 'cancelled'"
        ).fetchone()["c"],
        "total_revenue": revenue_since("2000-01-01"),
        "pending_verifications": conn.execute(
            "SELECT COUNT(*) c FROM bookings WHERE payment_status = 'payment_verification_pending'"
        ).fetchone()["c"],
    }

    recent = conn.execute(
        "SELECT * FROM bookings ORDER BY created_at DESC LIMIT 8"
    ).fetchall()

    return render_template("admin/dashboard.html", stats=stats, recent=recent)


@app.route("/admin/bookings")
@admin_required
def admin_bookings():
    conn = get_conn()
    status_filter = request.args.get("status", "")
    date_filter = request.args.get("date", "")
    search = request.args.get("search", "").strip()

    query = "SELECT * FROM bookings WHERE 1=1"
    params = []
    if status_filter:
        query += " AND status = ?"
        params.append(status_filter)
    if date_filter:
        query += " AND booking_date = ?"
        params.append(date_filter)
    if search:
        query += " AND (customer_name LIKE ? OR mobile LIKE ? OR booking_code LIKE ?)"
        like = f"%{search}%"
        params += [like, like, like]
    query += " ORDER BY booking_date DESC, start_time DESC"

    bookings = conn.execute(query, params).fetchall()
    return render_template(
        "admin/bookings.html", bookings=bookings, status_filter=status_filter,
        date_filter=date_filter, search=search,
    )


@app.route("/admin/bookings/<int:booking_id>/status", methods=["POST"])
@admin_required
def admin_update_booking_status(booking_id):
    new_status = request.form.get("status")
    if new_status not in ("confirmed", "cancelled", "completed"):
        flash("Invalid status.", "error")
        return redirect(url_for("admin_bookings"))
    conn = get_conn()
    conn.execute("UPDATE bookings SET status = ? WHERE id = ?", (new_status, booking_id))
    conn.commit()
    flash("Booking status updated.", "success")
    return redirect(url_for("admin_bookings"))


# --- Admin: payment verification ---------------------------------------

@app.route("/admin/bookings/<int:booking_id>/payment/verify", methods=["POST"])
@admin_required
def admin_verify_payment(booking_id):
    conn = get_conn()
    booking = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if not booking:
        flash("Booking not found.", "error")
        return redirect(url_for("admin_bookings"))

    if booking["payment_status"] != "payment_verification_pending":
        flash("This booking has no payment awaiting verification.", "error")
        return redirect(url_for("admin_bookings"))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if booking["payment_method"] == "online":
        # Full amount verified — nothing remains.
        conn.execute(
            """UPDATE bookings SET payment_status = 'paid', amount_paid = total_amount,
               remaining_amount = 0, paid_at = ? WHERE id = ?""",
            (now, booking_id),
        )
    else:
        # Pay-at-turf: only the advance has been verified online.
        conn.execute(
            """UPDATE bookings SET payment_status = 'advance_paid', amount_paid = amount_due,
               remaining_amount = total_amount - amount_due, paid_at = ? WHERE id = ?""",
            (now, booking_id),
        )
    conn.commit()
    flash("Payment verified successfully.", "success")
    return redirect(url_for("admin_bookings"))


@app.route("/admin/bookings/<int:booking_id>/payment/reject", methods=["POST"])
@admin_required
def admin_reject_payment(booking_id):
    conn = get_conn()
    booking = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if not booking:
        flash("Booking not found.", "error")
        return redirect(url_for("admin_bookings"))

    if booking["payment_status"] != "payment_verification_pending":
        flash("This booking has no payment awaiting verification.", "error")
        return redirect(url_for("admin_bookings"))

    conn.execute(
        "UPDATE bookings SET payment_status = 'payment_failed' WHERE id = ?", (booking_id,)
    )
    conn.commit()
    flash("Payment rejected. The customer can submit a new transaction ID.", "success")
    return redirect(url_for("admin_bookings"))


@app.route("/admin/bookings/<int:booking_id>/payment/mark-remaining-paid", methods=["POST"])
@admin_required
def admin_mark_remaining_paid(booking_id):
    conn = get_conn()
    booking = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if not booking:
        flash("Booking not found.", "error")
        return redirect(url_for("admin_bookings"))

    if booking["payment_method"] != "pay_at_turf" or booking["payment_status"] != "advance_paid":
        flash("Remaining payment can only be marked for a pay-at-turf booking with a paid advance.", "error")
        return redirect(url_for("admin_bookings"))

    if booking["remaining_amount"] <= 0:
        flash("There is no remaining amount to collect for this booking.", "error")
        return redirect(url_for("admin_bookings"))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """UPDATE bookings SET amount_paid = total_amount, remaining_amount = 0,
           payment_status = 'paid', paid_at = ? WHERE id = ?""",
        (now, booking_id),
    )
    conn.commit()
    flash("Remaining amount marked as paid at turf.", "success")
    return redirect(url_for("admin_bookings"))


@app.route("/admin/slots", methods=["GET", "POST"])
@admin_required
def admin_slots():
    conn = get_conn()
    if request.method == "POST":
        block_date = request.form.get("block_date")
        start_time = request.form.get("start_time")
        end_time = request.form.get("end_time")
        reason = request.form.get("reason", "").strip()
        if block_date and start_time and end_time:
            conn.execute(
                "INSERT INTO blocked_slots (block_date, start_time, end_time, reason) VALUES (?,?,?,?)",
                (block_date, start_time, end_time, reason or "Blocked by admin"),
            )
            conn.commit()
            flash("Slot blocked successfully.", "success")
        return redirect(url_for("admin_slots", date=block_date))

    selected_date = request.args.get("date") or date.today().isoformat()
    availability, day_name = logic.get_day_availability(conn, selected_date)
    blocked = conn.execute(
        "SELECT * FROM blocked_slots WHERE block_date = ? ORDER BY start_time", (selected_date,)
    ).fetchall()
    return render_template(
        "admin/slots.html", availability=availability, selected_date=selected_date,
        day_name=day_name, blocked=blocked,
    )


@app.route("/admin/slots/unblock/<int:block_id>", methods=["POST"])
@admin_required
def admin_unblock_slot(block_id):
    conn = get_conn()
    row = conn.execute("SELECT block_date FROM blocked_slots WHERE id = ?", (block_id,)).fetchone()
    conn.execute("DELETE FROM blocked_slots WHERE id = ?", (block_id,))
    conn.commit()
    flash("Slot unblocked.", "success")
    return redirect(url_for("admin_slots", date=row["block_date"] if row else None))


@app.route("/admin/pricing", methods=["GET", "POST"])
@admin_required
def admin_pricing():
    conn = get_conn()
    if request.method == "POST":
        for day_type in ("weekday", "weekend"):
            for period in ("morning", "evening"):
                field = f"{day_type}_{period}"
                value = request.form.get(field)
                if value and value.isdigit():
                    conn.execute(
                        "UPDATE pricing SET price_per_hour = ? WHERE day_type = ? AND period = ?",
                        (int(value), day_type, period),
                    )
        conn.commit()
        flash("Pricing updated. New prices apply to future bookings only.", "success")
        return redirect(url_for("admin_pricing"))

    rows = conn.execute("SELECT * FROM pricing").fetchall()
    pricing = {(r["day_type"], r["period"]): r["price_per_hour"] for r in rows}
    return render_template("admin/pricing.html", pricing=pricing)


@app.route("/admin/payment-settings", methods=["GET", "POST"])
@admin_required
def admin_payment_settings():
    conn = get_conn()
    settings = database.get_payment_settings(conn)

    if request.method == "POST":
        upi_id = request.form.get("upi_id", "").strip()
        upi_display_name = request.form.get("upi_display_name", "").strip()
        instructions = request.form.get("payment_instructions", "").strip()
        advance_raw = request.form.get("advance_percentage", "").strip()

        errors = []
        if not upi_id:
            errors.append("UPI ID is required.")
        if not upi_display_name:
            errors.append("UPI display name is required.")
        try:
            advance_percentage = float(advance_raw)
            if not (0 < advance_percentage <= 100):
                errors.append("Advance percentage must be between 1 and 100.")
        except ValueError:
            errors.append("Advance percentage must be a number.")
            advance_percentage = settings["advance_percentage"]

        qr_filename = settings["qr_image"]
        qr_file = request.files.get("qr_image")
        if qr_file and qr_file.filename:
            original = secure_filename(qr_file.filename)
            ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
            if ext not in ALLOWED_QR_EXTENSIONS:
                errors.append("QR image must be a PNG, JPG, JPEG, or WEBP file.")
            else:
                os.makedirs(QR_UPLOAD_DIR, exist_ok=True)
                qr_filename = f"payment_qr.{ext}"
                # Remove any previously stored QR file with a different extension
                for old_ext in ALLOWED_QR_EXTENSIONS:
                    if old_ext != ext:
                        stale = os.path.join(QR_UPLOAD_DIR, f"payment_qr.{old_ext}")
                        if os.path.exists(stale):
                            os.remove(stale)
                qr_file.save(os.path.join(QR_UPLOAD_DIR, qr_filename))

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin/payment_settings.html", settings=settings, qr_url=qr_image_url(settings))

        conn.execute(
            """UPDATE payment_settings SET upi_id = ?, upi_display_name = ?,
               qr_image = ?, advance_percentage = ?, payment_instructions = ?""",
            (upi_id, upi_display_name, qr_filename, advance_percentage, instructions or None),
        )
        conn.commit()
        flash("Payment settings updated successfully.", "success")
        return redirect(url_for("admin_payment_settings"))

    return render_template("admin/payment_settings.html", settings=settings, qr_url=qr_image_url(settings))


@app.route("/admin/turf-settings", methods=["GET", "POST"])
@admin_required
def admin_turf_settings():
    conn = get_conn()
    if request.method == "POST":
        conn.execute(
            """UPDATE turf SET name=?, tagline=?, description=?, address=?, contact_number=?,
               opening_time=?, closing_time=?, facilities=?, rules=?, map_embed=?""",
            (
                request.form.get("name", "").strip(),
                request.form.get("tagline", "").strip(),
                request.form.get("description", "").strip(),
                request.form.get("address", "").strip(),
                request.form.get("contact_number", "").strip(),
                request.form.get("opening_time", "06:00"),
                request.form.get("closing_time", "23:00"),
                request.form.get("facilities", "").strip(),
                request.form.get("rules", "").strip(),
                request.form.get("map_embed", "").strip(),
            ),
        )
        conn.commit()
        flash("Turf settings updated successfully.", "success")
        return redirect(url_for("admin_turf_settings"))

    turf = conn.execute("SELECT * FROM turf LIMIT 1").fetchone()
    return render_template("admin/turf_settings.html", turf=turf)


@app.route("/admin/reviews")
@admin_required
def admin_reviews():
    conn = get_conn()
    reviews = conn.execute("SELECT * FROM reviews ORDER BY created_at DESC").fetchall()
    return render_template("admin/reviews.html", reviews=reviews)


@app.route("/admin/reviews/<int:review_id>/status", methods=["POST"])
@admin_required
def admin_update_review(review_id):
    new_status = request.form.get("status")
    if new_status not in ("approved", "hidden", "pending"):
        flash("Invalid status.", "error")
        return redirect(url_for("admin_reviews"))
    conn = get_conn()
    conn.execute("UPDATE reviews SET status = ? WHERE id = ?", (new_status, review_id))
    conn.commit()
    flash("Review updated.", "success")
    return redirect(url_for("admin_reviews"))


@app.route("/admin/reviews/<int:review_id>/delete", methods=["POST"])
@admin_required
def admin_delete_review(review_id):
    conn = get_conn()
    conn.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
    conn.commit()
    flash("Review deleted.", "success")
    return redirect(url_for("admin_reviews"))


@app.route("/admin/reports")
@admin_required
def admin_reports():
    conn = get_conn()
    today = date.today().isoformat()
    week_start = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    month_start = date.today().replace(day=1).isoformat()

    def revenue_since(start_date, end_date=None):
        q = "SELECT COALESCE(SUM(total_amount),0) s, COUNT(*) c FROM bookings WHERE booking_date >= ? AND status != 'cancelled'"
        params = [start_date]
        if end_date:
            q += " AND booking_date <= ?"
            params.append(end_date)
        return conn.execute(q, params).fetchone()

    today_r = revenue_since(today, today)
    week_r = revenue_since(week_start)
    month_r = revenue_since(month_start)
    total_r = revenue_since("2000-01-01")

    daily_rows = conn.execute(
        """SELECT booking_date, SUM(total_amount) revenue, COUNT(*) bookings
           FROM bookings WHERE status != 'cancelled' AND booking_date >= ?
           GROUP BY booking_date ORDER BY booking_date DESC LIMIT 14""",
        (month_start,),
    ).fetchall()

    return render_template(
        "admin/reports.html", today_r=today_r, week_r=week_r, month_r=month_r,
        total_r=total_r, daily_rows=daily_rows,
    )


@app.route("/admin/export")
@admin_required
def admin_export():
    conn = get_conn()
    filter_name = request.args.get("filter", "all")
    start = request.args.get("start")
    end = request.args.get("end")
    try:
        filepath, filename, count = export_bookings_to_excel(conn, filter_name, start, end)
    except Exception:
        flash("Excel export failed. Please try again.", "error")
        return redirect(url_for("admin_dashboard"))

    if count == 0:
        flash("No bookings found for the selected filter. Export cancelled.", "error")
        return redirect(url_for("admin_dashboard"))

    return send_file(filepath, as_attachment=True, download_name=filename)


# ---------------------------------------------------------------------------
# Error handlers — never expose raw stack traces to the user
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Page not found."), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, message="Something went wrong on our end. Please try again."), 500


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    database.init_db()
    print("=" * 60)
    print(" Cricket Turf Booking Website")
    print(" Running at: http://127.0.0.1:5000")
    print(" Admin panel: http://127.0.0.1:5000/admin/login")
    print(" Admin login -> username: admin | password: admin123")
    print("=" * 60)
    app.run(debug=True, host="127.0.0.1", port=5000)
