# 🏏 Cricket Turf Booking Website

A complete, self-contained cricket turf booking system built with
**HTML, CSS, Vanilla JavaScript, Python Flask, and SQLite**.
No Node.js, npm, React, or any frontend build tools are required.

---

## 1. Requirements

- Python 3.9 or newer installed on your Windows laptop
  (download from https://www.python.org/downloads/ — during install,
  tick **"Add Python to PATH"**)

That's it. No other software is required.

---

## 2. How to Run (Windows)

1. Extract/copy this project folder anywhere on your laptop, e.g.
   `C:\Users\YourName\cricket_turf_booking`

2. Open **Command Prompt** and navigate into the folder:
   ```
   cd C:\Users\YourName\cricket_turf_booking
   ```

3. (Recommended) Create a virtual environment:
   ```
   python -m venv venv
   venv\Scripts\activate
   ```

4. Install the required packages:
   ```
   pip install -r requirements.txt
   ```

5. Run the application:
   ```
   python app.py
   ```

6. Open your browser and go to:
   ```
   http://127.0.0.1:5000
   ```

The database (`database.db`) is created automatically the first time
you run `app.py`, complete with demo data so the site never looks empty.

To stop the server, press `CTRL + C` in the Command Prompt window.

---

## 3. Where the Database Lives

All data (users, bookings, pricing, reviews, etc.) is stored in a single
SQLite file: **`database.db`**, created in the project's root folder the
first time you run the app. You can open it with any SQLite browser
(e.g. "DB Browser for SQLite") if you want to inspect it directly.

To reset everything back to a fresh demo state, simply close the app,
delete `database.db`, and run `python app.py` again.

---

## 4. Demo Data

On first run the app seeds:
- One demo turf ("GreenStrike Cricket Turf") with facilities, rules, and hours
- Default weekday/weekend pricing
- A handful of sample bookings and reviews (clearly labeled `[DEMO DATA]`)
- One admin account: **username `admin`, password `admin123`**

Feel free to edit or delete this data from the admin panel.

---

## 5. Accessing the Admin Panel

Go to:
```
http://127.0.0.1:5000/admin/login
```
Login with:
- **Username:** `admin`
- **Password:** `admin123`

⚠️ For real-world use, change this password by editing the `admins`
table in `database.db`, or add an admin password-change feature.

From the admin panel you can:
- View the dashboard (today/weekly/monthly revenue, booking counts)
- Manage all bookings (filter, search, confirm/cancel/complete)
- Block/unblock specific time slots
- Change weekday/weekend pricing (existing bookings keep their original price)
- Edit turf details, facilities, and rules
- Approve/hide/delete customer reviews
- View revenue reports and export bookings to Excel

---

## 6. How to Export Bookings to Excel

1. Log into the admin panel.
2. Go to **Reports** (or click **Export** in the sidebar).
3. Choose a filter: Today, This Week, This Month, a Custom Date Range, or All Bookings.
4. An `.xlsx` file will download automatically, containing every booking
   record with customer, date, time, price, and status columns.

Exported files are also saved inside the project's `exports/` folder.

---

## 7. How to Change Pricing

1. Log into the admin panel → **Pricing**.
2. Update the Morning/Evening price per hour for Weekday and Weekend.
3. Click **Save Pricing**.

Important: **existing confirmed bookings keep the price that was active
at the time they were booked.** Only new bookings made after the change
will use the new price.

---

## 8. Project Structure

```
cricket_turf_booking/
│
├── app.py              # Flask routes (customer + admin)
├── database.py          # SQLite schema + demo data seeding
├── logic.py              # Pricing & slot-availability business logic
├── excel_export.py       # openpyxl-based Excel export
├── database.db           # created automatically on first run
├── requirements.txt
├── README.md
│
├── templates/            # Jinja2 HTML templates
│   └── admin/             # Admin panel templates
│
├── static/
│   ├── css/style.css       # All styling
│   ├── js/script.js         # Slot selection, price calc, nav toggle
│   └── images/
│
└── exports/               # Generated Excel exports land here
```

---

## 9. Notes

- **Payment**: customers can pay the **full amount online** or pay **30% (configurable) advance online + the rest at the turf**.
  There is no live payment-gateway API wired up — payment is confirmed by the customer submitting a
  UPI transaction/reference ID, which the admin then verifies manually from the Admin Panel
  (Admin → Bookings → **Verify Payment** / **Reject Payment**). See the **Payment Settings**
  page in the Admin Panel to change the UPI ID, QR code image, advance percentage, and payment
  instructions.
- Passwords (both customer and admin) are hashed with Werkzeug's
  `generate_password_hash` — never stored in plain text.
- Duplicate-booking protection is enforced on the backend: even if two
  people select the same slot at the same time, the second one to submit
  will be told the slot was just booked and asked to pick another.
