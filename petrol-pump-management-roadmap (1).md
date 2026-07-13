# Petrol Pump / Fuel Station Management System — Project Roadmap

A complete specification document for building a fuel station management application.
Hand this whole document to Cursor (or any AI coding tool) as the project brief.

---

## 1. Project Summary

A web-based management system for a petrol pump / fuel station that handles:
- Admin-controlled user & customer management
- Daily fuel price updates with automatic stock/value recalculation
- Meter-reading based sales entry (liters sold calculated automatically)
- Customer credit ledger (purchases, payments, due/clear tracking)
- Inventory tracking for Petrol, Diesel (and any other fuel/products)
- Dashboard with bar charts & pie/circular charts for sales, profit, and stock
- SMS integration for receipts, due reminders, and offers
- Clean, professional UI with a fixed top navigation bar

---

## 2. Tech Stack (Python Flask + MySQL)

| Layer | Recommendation |
|---|---|
| Backend | Python Flask (Application Factory pattern, Blueprints per module) |
| Frontend | Server-rendered Jinja2 templates + Bootstrap 5 (or Tailwind) + a little vanilla JS |
| Charts | Chart.js (loaded via CDN, fed data from Flask JSON endpoints) |
| Database | MySQL |
| ORM | SQLAlchemy (via Flask-SQLAlchemy) |
| Migrations | Alembic (via Flask-Migrate) |
| Auth | Flask-Login for session-based auth, with role checks (Admin / Staff) via a `@role_required` decorator |
| Forms | Flask-WTF (CSRF protection + form validation built in) |
| SMS Gateway | Twilio, or a local SMS API provider (Msg91/Fast2SMS) — called via `requests` from a small `sms_service.py` |
| PDF/Receipts | WeasyPrint or ReportLab for printable receipts |
| Hosting | PythonAnywhere / Railway / a VPS with Gunicorn + Nginx; MySQL hosted alongside or on a managed DB service |

### Suggested Project Structure
```
petrol_pump/
├── app/
│   ├── __init__.py          # app factory, extensions init
│   ├── models.py            # SQLAlchemy models (or models/ package per table group)
│   ├── auth/                # login, logout, user management (admin only)
│   ├── dashboard/            # dashboard routes + chart data endpoints
│   ├── pricing/               # fuel price CRUD + history
│   ├── inventory/             # stock entries + current stock
│   ├── sales/                  # meter readings + sales
│   ├── customers/              # customer CRUD + ledger
│   ├── payments/                # credit payment recording
│   ├── sms/                       # templates + send logic + logs
│   ├── templates/                  # Jinja2 templates (base.html with top navbar, per-module folders)
│   └── static/                       # css/js/images
├── migrations/                        # Alembic migration files
├── config.py                            # config classes (Dev/Prod), MySQL URI, secret keys
├── requirements.txt
└── run.py                                 # entry point
```

### Key Python Packages
```
Flask
Flask-SQLAlchemy
Flask-Migrate
Flask-Login
Flask-WTF
PyMySQL          # or mysqlclient — MySQL driver for SQLAlchemy
requests         # for SMS gateway API calls
WeasyPrint        # for PDF receipts (optional)
python-dotenv      # manage .env config
```

You can tell Cursor to scaffold this exact structure, or hand it the folder tree above directly.

---

## 3. User Roles

1. **Admin**
   - Only Admin can create/edit/delete Staff (user) accounts
   - Full access: pricing, inventory, reports, customer management, SMS templates
2. **Staff / Operator**
   - Can enter meter readings, record sales, add customer purchases, record payments
   - Cannot change prices or add/remove users
3. **(Optional) Customer Portal — future phase**
   - View own ledger/statement only (read-only)

---

## 4. Database Models (Schema Outline)

> Field types below are logical types — in MySQL/SQLAlchemy use: `Integer` (auto-increment PK), `String`, `Numeric(10,2)` for money/liters, `Date` / `DateTime`, and `Enum` for fixed-choice fields. Every table should have `id` as an `AUTO_INCREMENT` primary key.

### `users`
| Field | Type | Notes |
|---|---|---|
| id | UUID/int | PK |
| name | string | |
| email | string | unique |
| password_hash | string | |
| role | enum | admin, staff |
| phone | string | |
| status | enum | active, disabled |
| created_at | datetime | |

### `fuel_types`
| Field | Type | Notes |
|---|---|---|
| id | int | PK |
| name | string | Petrol, Diesel, etc. |
| unit | string | Liter |

### `fuel_prices` (daily price history)
| Field | Type | Notes |
|---|---|---|
| id | int | PK |
| fuel_type_id | FK | |
| price_per_liter | decimal | |
| effective_date | date | |
| updated_by | FK → users | |
| created_at | datetime | |

> Every price change inserts a new row here — never overwrite. This gives you full price history and lets profit calculations use the correct historical price per sale.

### `inventory`
| Field | Type | Notes |
|---|---|---|
| id | int | PK |
| fuel_type_id | FK | |
| current_stock_liters | decimal | live available stock |
| last_updated | datetime | |
| reorder_threshold | decimal | for low-stock alerts |

### `stock_entries` (incoming stock / tanker delivery)
| Field | Type | Notes |
|---|---|---|
| id | int | PK |
| fuel_type_id | FK | |
| liters_added | decimal | |
| cost_per_liter | decimal | purchase cost, for profit calc |
| supplier | string | |
| entry_date | datetime | |
| added_by | FK → users | |

### `meter_readings`
| Field | Type | Notes |
|---|---|---|
| id | int | PK |
| dispenser_nozzle_id | string/FK | which pump/nozzle |
| fuel_type_id | FK | |
| opening_reading | decimal | |
| closing_reading | decimal | |
| liters_sold | decimal (computed) | closing − opening |
| reading_date | date | |
| recorded_by | FK → users | |

### `customers`
| Field | Type | Notes |
|---|---|---|
| id | int | PK |
| name | string | |
| phone | string | for SMS |
| address | string | |
| credit_limit | decimal | optional |
| current_balance_due | decimal | running total |
| created_at | datetime | |

### `sales` (per transaction, cash or credit)
| Field | Type | Notes |
|---|---|---|
| id | int | PK |
| customer_id | FK, nullable | null = walk-in cash sale |
| fuel_type_id | FK | |
| liters | decimal | |
| price_per_liter | decimal | snapshot at sale time |
| total_amount | decimal (computed) | |
| payment_type | enum | cash, credit |
| sale_date | datetime | |
| recorded_by | FK → users | |

### `payments` (credit clearance / partial payments)
| Field | Type | Notes |
|---|---|---|
| id | int | PK |
| customer_id | FK | |
| amount_paid | decimal | |
| payment_date | datetime | |
| method | enum | cash, bank transfer, etc. |
| note | string | |

### `sms_logs`
| Field | Type | Notes |
|---|---|---|
| id | int | PK |
| customer_id | FK | |
| message_type | enum | receipt, due_reminder, offer, price_update |
| message_body | text | |
| status | enum | sent, failed |
| sent_at | datetime | |

### `sms_templates`
| Field | Type | Notes |
|---|---|---|
| id | int | PK |
| type | enum | receipt, due_reminder, offer, price_update |
| template_text | text | supports placeholders like {{name}}, {{amount}}, {{price}} |
| created_by | FK → users (admin only) | |

---

## 5. Core Feature Modules

### 5.1 Authentication
- Login page (email/username + password)
- JWT session, role stored in token
- Admin-only screen to create/edit/disable Staff accounts
- Password reset (optional, phase 2)

### 5.2 Dashboard (Home Screen)
- Top summary cards: Today's Sales, Today's Profit, Total Credit Outstanding, Current Stock (Petrol/Diesel)
- **Bar chart** — Sales & Profit trend (daily/weekly/monthly toggle)
- **Pie/circular chart** — Fuel type sales split (Petrol vs Diesel), or Cash vs Credit split
- Low-stock alert banner if inventory falls below threshold
- Quick links to: Add Sale, Add Stock, Update Price, Add Customer

### 5.3 Price Management
- Admin sets today's price per fuel type
- Price change auto-logged in `fuel_prices` with timestamp
- Stock valuation and future sales automatically use latest active price
- Price history table/chart (so you can see rate increase/decrease over time)

### 5.4 Inventory & Stock Entry
- Form to record new stock delivery (liters added, cost/liter, supplier)
- Auto-updates `inventory.current_stock_liters`
- View current available Petrol/Diesel liters live
- Low stock threshold alert

### 5.5 Meter Reading → Sales Calculation
- Staff enters opening & closing meter reading per nozzle/dispenser
- System computes liters sold = closing − opening
- Auto-deducts from `inventory`
- Auto-creates a `sales` record using today's active price

### 5.6 Customer & Credit Management
- Add Customer form (name, phone, address, credit limit)
- Record a purchase against a customer (cash or credit)
- Customer ledger page: full history of purchases + payments + running balance
- "Clear Credit" / partial payment button, updates `current_balance_due`
- Filter/search customers by outstanding due amount

### 5.7 Receipts & SMS
- Auto-generate receipt after each sale (printable/PDF + SMS text version)
- SMS sent on: sale receipt, price update, due reminder, promotional offers
- Admin manages **message templates** (designed messages) with placeholders
- "Send Message" button next to each customer — picks template, fills data, sends via SMS gateway
- SMS log/history per customer

### 5.8 Reports (Phase 2)
- Daily/Monthly sales report (export CSV/PDF)
- Profit report (sales price − cost price × liters)
- Customer due/aging report
- Stock movement report

---

## 6. UI/UX Requirements

- **Top bar navigation** (fixed): Logo | Dashboard | Sales | Inventory | Customers | Reports | Settings | User menu (top-right, avatar + logout)
- Left sidebar optional for sub-sections, or keep everything in the clean top nav for a lighter feel
- Professional color palette: deep navy/charcoal + one accent color (e.g. amber/teal — fits fuel branding), white/light-gray content area
- Cards with subtle shadows, rounded corners, consistent spacing (8px grid)
- Data tables: sortable, searchable, paginated
- Use **badges** for status (Paid / Due / Low Stock)
- Fully responsive (desktop-first, but usable on tablet at the pump counter)
- Loading skeletons instead of blank screens
- Confirmation modals for critical actions (delete user, clear large credit, send bulk SMS)

---

## 7. Suggested Build Roadmap (Phases)

### Phase 1 — Foundation
1. Project scaffold (frontend + backend + DB)
2. Auth system (login, JWT, roles)
3. Database models & migrations (all tables above)
4. Top nav layout + basic page shells

### Phase 2 — Core Operations
5. Price management module (set/update price, history log)
6. Inventory + stock entry module
7. Meter reading entry → auto sales calculation
8. Customer add/edit + basic ledger view

### Phase 3 — Money & Credit
9. Credit sales flow (assign sale to customer, mark cash/credit)
10. Payment recording + balance auto-update
11. Full customer ledger (purchase history, payment history, due summary)

### Phase 4 — Dashboard & Reports
12. Dashboard bar chart (sales/profit trend)
13. Dashboard pie chart (fuel split / cash vs credit)
14. Low stock alerts
15. Export reports (CSV/PDF)

### Phase 5 — Messaging
16. SMS gateway integration (Twilio/local provider)
17. Message template manager (admin only)
18. "Send Message" action buttons (receipt, reminder, offer)
19. SMS logs per customer

### Phase 6 — Polish
20. Print-friendly receipts
21. Role permission refinement
22. Mobile responsiveness pass
23. Final UI polish, empty states, error handling

---

## 8. Notes for the AI Coding Tool (Cursor)

When you paste this into Cursor, it helps to add these instructions at the top of your prompt:

- "Scaffold this as a Flask app using the Application Factory pattern with Blueprints, SQLAlchemy + Flask-Migrate for MySQL, and Flask-Login for auth. Use the folder structure given below exactly."
- "Set up `config.py` to read the MySQL connection string from a `.env` file (`DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_NAME`) — never hardcode credentials."
- "Build Phase 1 completely before moving to Phase 2."
- "Every price change must be logged as a new row in `fuel_prices`, never update in place."
- "All sales must snapshot the price at time of sale so historical profit stays accurate even after price changes."
- "Only Admin role can create users, edit prices, or manage SMS templates — enforce this with a `@role_required('admin')` decorator on the route, not just hidden in the template."
- "Use Jinja2 template inheritance: one `base.html` with the top navbar and sidebar-free layout, and `{% block content %}` for each page."
- "Use Chart.js in the dashboard template, fed by a `/dashboard/api/chart-data` JSON endpoint that Flask returns — don't hardcode chart data in HTML."
- "Run `flask db init/migrate/upgrade` after every model change to keep MySQL migrations in sync."

---

*This document is meant to be copy-pasted directly into Cursor as your project brief. You can trim any phase/module you don't need for v1.*
