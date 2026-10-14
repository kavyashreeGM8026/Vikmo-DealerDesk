

# VIKMO DealerDesk — AI/ML Track

## Overview

DealerDesk is a Django REST API for a vehicle-parts distributor. This submission implements the catalogue/dealer/order backend, PostgreSQL-backed order and pricing logic, supplier catalogue synchronization, a Gemini-powered dealer assistant with database-backed tools, and a four-week demand forecast with reorder suggestions.

### Implemented areas

- Product catalogue, inventory, dealers, and draft orders through the REST API.
- Order pricing with `Decimal` and per-line GST rounding.
- PostgreSQL order-number allocation and transactional order workflows.
- Supplier catalogue pagination, retries, validation, and atomic reconciliation.
- Gemini tool-calling assistant for product search, live stock checks, and draft-order creation.
- Four-week forecasts for 40 SKUs and reorder suggestions using forecast demand, lead times, and safety stock.

No frontend is included. The API is configured without authentication for the assignment contract; do not expose it publicly as a production service without adding appropriate authentication and deployment hardening.

## Requirements

- Python 3.11+
- PostgreSQL
- A Gemini API key for assistant responses
- Supplier API URL and API key (the assignment's mock supplier can be used locally)

## Environment configuration

Create a private `.env` file in the repository root from `.env.example`. Do not commit `.env` or real credentials.

Set these variables:

```dotenv
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE
SUPPLIER_API_URL=http://127.0.0.1:8099
SUPPLIER_API_KEY=your-supplier-key
GEMINI_API_KEY=your-gemini-key
GEMINI_MODEL=gemini-3.5-flash-lite
DJANGO_SECRET_KEY=replace-with-a-private-secret
```

The database URL must point to PostgreSQL. SQLite is deliberately rejected because order concurrency requirements depend on PostgreSQL behavior. If your password contains URL-reserved characters, percent-encode them in the connection URL.

## Local setup

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env with your local PostgreSQL URL and keys
python manage.py migrate --noinput
python manage.py seed_data
python manage.py seed_data
python manage.py runserver 0.0.0.0:8000
```

### macOS/Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env with your PostgreSQL URL and keys
python manage.py migrate --noinput
python manage.py seed_data
python manage.py seed_data
python manage.py runserver 0.0.0.0:8000
```

`seed_data` is designed to be safe to run repeatedly. The second run should not create duplicate catalogue products or dealers.

## Supplier mock

In a separate terminal, run:

```bash
python supplier_mock/mock_supplier.py
```

Use the URL and API key printed by the mock to configure `SUPPLIER_API_URL` and `SUPPLIER_API_KEY`. The mock's outage mode can be used to exercise failure handling:

```bash
python supplier_mock/mock_supplier.py --outage
```

## Tests and smoke test

Run the automated test suite:

```bash
python manage.py test common core
```

Run the bundled smoke test against a freshly migrated and seeded database:

```bash
python smoke_test.py --base-url http://localhost:8000 --track aiml
```

The smoke test creates a test order, so use a disposable test database. The `aiml` track is selected because this submission includes the dealer assistant and forecasting. For an AI assistant request, configure a valid `GEMINI_API_KEY`; without a key, assistant calls intentionally report that the assistant is not configured.

Forecast generation and backtesting:

```bash
python ml/forecast.py
```

The committed `ml/forecast.csv` contains 160 forecast rows (40 SKUs × 4 weeks).

## LLM

- Provider: Google Gemini
- SDK: `google-genai`
- Default model: `gemini-3.5-flash-lite` (overridable with `GEMINI_MODEL`)

The assistant uses tool calling; catalogue and stock facts are retrieved from the database tools rather than relying on model memory.

## Assumptions and limitations

- PostgreSQL is required; SQLite is unsupported.
- The API uses the assignment's unauthenticated API configuration and is intended for evaluation/local use.
- The assistant depends on a configured Gemini API key and network access to the provider.
- Forecasts are generated from the supplied sales, promotion, lead-time, and festival-calendar CSV files. Forecast quality can be weaker for intermittent/new SKUs and unusual demand changes.
- No video walkthrough link has been added yet. Add an unlisted walkthrough URL here before submission if you record one.

## Video walkthrough

https://drive.google.com/file/d/1-dD3K0_JtpaBv6-mpZrodp9sQieFFybP/view?usp=sharing
