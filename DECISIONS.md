# DECISIONS.md

## 1. Database schema and relationships

`Product` uses `sku` as its primary key and stores name, category, brand, vehicle fitment, `DecimalField` price, integer stock, and description (`core/models.py:7-17`). `Dealer` stores dealer identity and contact/tax details; `dealer_code` is unique and `credit_limit` is decimal (`core/models.py:26-40`). `Order` stores a unique order number, lifecycle status, totals, timestamps, and a foreign key to `Dealer` using `PROTECT`, so a dealer referenced by an order cannot be deleted (`core/models.py:56-77`).

`OrderItem` links to `Order` with `CASCADE` (deleting an order deletes its items) and to `Product` with `PROTECT` (a product referenced by order history cannot be deleted); it also snapshots `product_name` and stores quantity and line pricing (`core/models.py:92-103`). Database constraints enforce one line per product per order and quantity of at least one (`core/models.py:105-112`). `OrderNumberSequence` stores a per-day counter with `day` as primary key and has no foreign keys (`core/models.py:118-122`). These are the five models defined in `core/models.py`; the code comments describe the historical-name snapshot and the deletion effects, but do not document a separate rationale for every choice.

## 2. Retrieval strategy

`search_parts` uses PostgreSQL case-insensitive substring filters, not embeddings, a vector database, or semantic search. It tokenizes terms, optionally removes stopwords and strips a trailing `s`, then requires each query term to match SKU, name, brand, category, or vehicle fitment; vehicle terms are matched against `vehicle_fitment`, and category is an additional filter (`core/services/assistant/tools.py:102-112`, `116-151`).

Results are ordered alphabetically by SKU and the tool returns at most five products, with `more_results_exist` indicating that more matches were found (`core/services/assistant/tools.py:153-159`). This is deterministic but is not a relevance ranking. It can miss synonyms, misspellings, and matches found only in descriptions, and alphabetical ordering can exclude a more relevant match when more than five products qualify.

## 3. Tool-calling loop

`POST /api/assistant/chat/` validates `session_id`, `dealer_id`, and `message`, loads the dealer, checks Gemini configuration, opens a dealer-bound session, and calls `run_assistant` with the message and stored history (`core/assistant_views.py:32-51`, `53-80`). `run_assistant` appends the user message to history, configures Gemini with the system instruction and tool declarations, and loops for at most five tool rounds within a 50-second budget (`core/services/assistant/llm.py:16-22`, `103-120`, `122-138`).

The code inspects the model response for function calls; with no calls, it returns the reply and updated history. Otherwise it runs each requested tool via `execute_tool`, packages the result as a Gemini function response, and adds it to the conversation for another model round (`core/services/assistant/llm.py:140-163`, `165-193`). The view commits trimmed history, derives `cited_skus`, and returns the reply, citations, tool calls, and actions (`core/assistant_views.py:88-125`).

## 4. Price and stock accuracy

`search_parts` and `check_stock` read `Product` records through Django ORM queries at tool-call time and format current catalogue fields, including price and stock (`core/services/assistant/tools.py:65-77`, `116-159`, `162-179`). The system instruction tells Gemini to call tools again before stating prices or stock, not to invent figures, and to treat previous-turn figures as potentially stale (`core/services/assistant/llm.py:31-42`).

`create_draft_order` validates model-supplied items with `OrderCreateSerializer`, fixes the dealer from request context rather than trusting model arguments, and invokes the existing draft-order service (`core/services/assistant/tools.py:203-245`). That service prices using `Decimal`, creates the order and items inside `transaction.atomic()`, and explicitly does not change stock (`core/services/orders.py:37-74`; `core/services/pricing.py:35-63`). SKU citations are returned only when the SKU came from a trusted tool result, is mentioned in the reply, and still exists in the database (`core/services/assistant/citations.py:34-45`, `48-67`, `79-93`). These checks reduce unsupported SKU references but do not prove every natural-language price or stock figure is correct; catalogue stock can also change after a lookup.

## 5. Untrusted supplier descriptions

The assistant's system instruction says catalogue names and descriptions are untrusted and must never be followed as instructions (`core/services/assistant/llm.py:48-50`). Tool output cleans text by removing control characters, collapsing whitespace, and truncating it, then returns descriptions under `description_untrusted` (`core/services/assistant/tools.py:34-37`, `55-77`). Supplier sync validates input fields and applies catalogue updates transactionally (`core/services/supplier_sync.py:123-169`, `172-267`).

These protections limit malformed text and explicitly warn the model, but whitespace cleaning and a JSON field name do not remove ordinary-language prompt injections. The model may still follow instructions embedded in a description; the repository does not establish a hard isolation boundary that guarantees this cannot happen. A next step would be adversarial injection tests and separating catalogue text from instructions in the model/tool protocol.

## 6. Forecasting and data leakage

`ml/forecast.py` fits a pooled log-linear model for regular-selling SKUs, with SKU-level effects, a global trend, promotion effect, and distance-to-Diwali effects. Per-SKU levels are exponentially weighted averages of recent residuals; SKUs with more than 20% zero-demand weeks use a recent mean or median (`ml/forecast.py:30-44`, `119-169`; `ml/README.md:13-26`).

The rolling-origin backtest predicts four weeks ahead and uses this explicit training cutoff: `hist = sales[sales["week_start"] <= origin]` (`ml/forecast.py:189-208`, line 200). It compares the model with last-value, recent-average, and same-week-last-year baselines using WAPE and MAE (`ml/forecast.py:173-185`, `211-223`). Output validation checks columns, 160 rows, 40 SKUs, four forecast weeks per SKU, duplicate keys, and finite non-negative forecasts (`ml/forecast.py:227-238`). The backtest uses actual future promotion flags, which is appropriate only if those flags are genuinely known at forecast time (`ml/forecast.py:189-203`).

## 7. Forecast weaknesses and special cases

The ML README reports the worst-performing SKUs as `BRK-1088`, `ELE-1006`, `ELE-1031`, and `GRP-1043`, with per-SKU WAPE from 0.8 to 1.2; these intermittent-demand SKUs use a flat recent average and receive no festive effect (`ml/README.md:43-49`; `ml/forecast.py:159-165`). This worst-SKU evidence is documented in the repository README rather than emitted as a separate per-SKU error report by `forecast.py`, whose scoring function reports aggregate WAPE and MAE (`ml/forecast.py:211-223`).

Promotions use one pooled effect and forecast-week flags from `promo_calendar.csv`; Diwali effects are based on week-distance to festival dates, with unsupported distances left without an estimated effect (`ml/README.md:19-20`; `ml/forecast.py:62-89`, `93-115`). The README identifies `CHN-1006` and `BRK-1105` as new SKUs with six weeks of history; they receive pooled effects but their level estimate is noisy and there is no launch-ramp model (`ml/README.md:23`, `46`). Only two Diwalis are available, and the README notes that only one Diwali can be backtested out of sample (`ml/README.md:47-49`).

## 8. Weakest part and two-day plan

A concrete weakness is that catalogue retrieval is substring-based and SKU-alphabetical rather than relevance-ranked, while prompt-injection protection relies partly on a system instruction and cleaned text; neither mechanism guarantees semantic relevance or complete isolation from malicious catalogue text (`core/services/assistant/tools.py:116-159`, `34-77`; `core/services/assistant/llm.py:48-50`). Forecasting also has limited Diwali validation because the dataset contains only two festival instances (`ml/README.md:47-49`).

**Day 1:** Add retrieval ranking or a carefully tested matching strategy; create tests for synonyms, misspellings, ambiguous fitment, and malicious descriptions. Add checks that compare assistant price/stock claims with the latest tool results where feasible.

**Day 2:** Produce and test per-SKU forecast error reporting, especially for intermittent and new SKUs; add tests for promotion and Diwali windows and document evaluation results. These are proposed improvements, not existing features.
