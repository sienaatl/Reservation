# Siena Reservations

A lightweight OpenTable-style restaurant reservation MVP built with Flask and SQLite.

## Features

- Guest-facing booking flow
- Real-time availability by date and party size
- Automatic table assignment using the smallest available suitable table
- Conflict prevention for overlapping reservations
- Confirmation and self-service cancellation page
- Host dashboard with bookings, covers, table assignments, and status updates
- Dining-room table management
- Mobile-friendly Siena-inspired design

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open:

- Guest booking: http://127.0.0.1:5000
- Admin dashboard: http://127.0.0.1:5000/admin?pin=2468

## Configuration

Set environment variables before starting:

```bash
export APP_NAME="Siena Reservations"
export ADMIN_PIN="change-this"
export SECRET_KEY="use-a-long-random-value"
export OPEN_HOUR="16"
export CLOSE_HOUR="22"
export SLOT_MINUTES="30"
export TURN_MINUTES="90"
```

## Production upgrades recommended

1. PostgreSQL instead of SQLite
2. Staff authentication and role permissions
3. SMS/email confirmations through Twilio and SendGrid
4. Deposits and no-show fees through Stripe
5. Table-combination logic and floor-plan editor
6. Waitlist and walk-in management
7. Google Calendar/POS integration
8. Reservation modification, audit logs, and analytics
9. Deployment through Render, Railway, Fly.io, or AWS
10. Privacy policy, rate limiting, backups, and monitoring

## Data model

`restaurant_tables`: table name, capacity, and dining area.

`reservations`: guest details, party size, date/time, assigned table, duration, status, occasion, and notes.

## Important

The included admin PIN and secret key are development defaults. Change both before putting the app online.


## Deploy on Render

1. Upload this folder to a GitHub repository.
2. In Render, choose **New > Blueprint**.
3. Connect the repository.
4. Render will read `render.yaml`, install dependencies, create the web service, and attach a persistent disk.
5. After deployment, open the generated Render URL.
6. In the Render dashboard, open the service's **Environment** page to view or replace `ADMIN_PIN`.
7. Admin URL: `https://YOUR-SERVICE.onrender.com/admin?pin=YOUR_PIN`

The persistent disk stores SQLite at `/var/data/reservations.db`.


## Corrected Siena floor-plan mapping

- Main Dining: D26-D37
- Covered Patio: P1-P25 plus round tables 2, 3, and 4
- Existing table positions and combinations are preserved.


## Backend dashboard

Open `/admin?pin=YOUR_ADMIN_PIN` to access the full Siena host dashboard. It includes daily metrics, reservation filtering, guest search, quick reservation/walk-in creation, table assignment, status controls, service snapshots, and live floor-plan navigation.


## Reservation editing

The backend dashboard now includes an Edit button for each booking. Staff can update guest details, party size, date/time, duration, assigned table, status, occasion, and notes. Conflicting table assignments are blocked.


## Confirmation email and guest modification

Every new booking now receives a confirmation email containing a private, random management link. Guests can use that link to:

- Change their date and time
- Change party size
- Update their name, phone, and email
- Update occasion and special requests
- Cancel the reservation

The system re-checks table availability before accepting a change and sends an updated or cancellation email.

### Email configuration

Set these environment variables in Render:

```text
PUBLIC_BASE_URL=https://your-real-domain.com
SMTP_HOST=smtp.your-email-provider.com
SMTP_PORT=587
SMTP_USERNAME=your-smtp-username
SMTP_PASSWORD=your-smtp-password
SMTP_FROM_EMAIL=rsvp@sienaatl.com
SMTP_FROM_NAME=Siena Restaurant and Bar
SMTP_USE_TLS=true
```

For Gmail or Google Workspace, use an app password rather than the normal account password. A transactional provider such as SendGrid, Postmark, Mailgun, Amazon SES, or Resend SMTP is recommended for production delivery.

Bookings still complete if SMTP is unavailable; the application logs the email error and shows the guest a warning.


## Bar floor plan

A third floor-plan area named **Bar** is included with nine round bar seats:

- B1 through B9
- Capacity: 1 guest per seat
- Arranged in one horizontal row to match the supplied OpenTable layout
- Available in the live floor-plan area selector
- Reservations can be assigned to these seats from the backend dashboard


## Tappable table seating

The live floor plan now supports host seating actions:

- Tap any available table.
- Select a confirmed reservation that fits the table capacity.
- Tap **Seat at Table**.
- The reservation is assigned to that table and marked `seated`.
- The table immediately changes to the occupied/seated color and displays the guest's first name.
- Tap an occupied table to view guest details.
- Use **Complete & Release Table** when the party leaves.
- Use **Return to Confirmed** to undo a seating action.

The floor plan refreshes automatically every 30 seconds.


## Dynamic Siena turn times

- Bar: 60 minutes
- Covered Patio: 90 minutes for 1-2 guests; 105 minutes for 3+
- Main Dining: 90 minutes for 1-2; 120 for 3-4; 150 for 5-6; 180 for 7+
- The timer starts when the host seats the guest.
- Tables show expected end time, minutes remaining, ending-soon warnings at 15 minutes, and overdue alerts.
- Completing a reservation releases the table immediately.
- Managers can still override dining duration on the Edit Reservation page.
- Online availability uses the appropriate duration for each candidate table.


## Host Suite Phase 1

This build adds:

- Manager overview with covers, bookings, waitlist count, overdue tables, and large parties.
- 30-minute reservation timeline organized by table.
- Digital waitlist and walk-in seating workflow.
- Server list, table-section assignments, and cover counts.
- Guest CRM with VIP status, allergies, preferences, favorite table, preferred server, and visit count.
- Navigation links for all new host tools.

SMS delivery, drag-and-drop movement, POS integrations, and QR self-check-in remain future phases.


## Large-party online reservations

- Public reservations now accept parties from 1 through 20 guests.
- Two manager-arranged large-party inventory blocks are included for groups exceeding normal table capacity.
- Large-party bookings use a default 180-minute dining duration.
- The virtual inventory does not appear as an inaccurate physical table icon on the floor plan.
- Hosts can later assign the exact combined tables used when preparing the room.


## Dynamic combined-table availability

- Parties of 1-20 are matched against real available tables.
- The availability engine combines multiple free tables within the same dining area.
- Slots appear only when combined seat capacity meets the requested party size for the full turn time.
- The algorithm minimizes unused seats first, then minimizes the number of tables.
- Every table in the allocation is reserved and shown occupied on the floor plan.
- Placeholder 20-seat inventory has been removed.


## Twilio SMS configuration

Set these Render environment variables on the web service:

- `SMS_ENABLED=true`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_PHONE_NUMBER` in E.164 format, such as `+14045551234`
- `CRON_SECRET` to the same secret used by the reminder cron service
- `PUBLIC_BASE_URL` to the deployed HTTPS URL

SMS features included:

- Confirmation immediately after booking
- Updated-reservation confirmation
- Cancellation confirmation
- 24-hour reminder
- 2-hour reminder
- Waitlist table-ready message
- Admin resend-SMS action

Guests must opt in using the checkbox on the booking or management form. The protected reminder endpoint is `/tasks/send-sms-reminders?secret=...` and should be called every 15 minutes. Update the cron service `REMINDER_URL` after deployment if the Render URL differs.


## Complete SMS automation suite

Added running-late acknowledgment links, automatic birthday greetings, post-dining review requests, marketing campaigns for opted-in guests, and a Twilio inbound opt-out webhook.

Additional environment variables:

```env
REVIEW_URL=https://your-review-link
BIRTHDAY_SMS_ENABLED=true
REVIEW_SMS_ENABLED=true
RUNNING_LATE_MINUTES=15
```

Schedule both protected endpoints every 15 minutes or hourly:

- `/tasks/send-sms-reminders?secret=YOUR_CRON_SECRET`
- `/tasks/send-guest-messages?secret=YOUR_CRON_SECRET`

Configure the Twilio incoming-message webhook as `/twilio/incoming` to record STOP-style opt-outs in the local guest database. Transactional SMS consent and marketing consent are stored separately.


## Production Readiness Phase 1

Added secure staff accounts with manager/host/server roles, session cookies, login/logout, operating-hour and pacing settings, closure controls, audit logs, CSV export, minimum notice and booking-window enforcement, and SQLite write locking/recheck for safer concurrent bookings.

Set these production environment variables before deployment:

```
SECRET_KEY=<long-random-secret>
ADMIN_USERNAME=<initial-manager-username>
ADMIN_PASSWORD=<strong-password-at-least-10-characters>
COOKIE_SECURE=true
RESTAURANT_TIMEZONE=America/New_York
```

The legacy ADMIN_PIN still works as a migration fallback but should be removed after staff accounts are confirmed. PostgreSQL migration, payment deposits, provider delivery webhooks, and external uptime monitoring remain separate deployment phases.
