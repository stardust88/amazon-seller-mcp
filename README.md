# Amazon SP-API → Claude Cowork

An MCP server that runs on your Windows machine, holds your Amazon credentials
locally, and lets Claude pull Seller Central data on request.

## Why it has to run on your machine

Claude Cowork's cloud sandbox cannot reach `api.amazon.com` or the SP-API endpoints —
outbound network access is allow-listed and Amazon isn't on the list. So the API calls
happen here, on your PC, and only the results travel to Claude. Your refresh token
never leaves this machine.

## What this build is scoped to

Two granted LWA roles:

| Role | What it unlocks |
|---|---|
| **Inventory and Order Tracking** | Merchant listings, orders, returns |
| **Brand Analytics** | Sales and traffic, search terms, market basket, repeat purchase |

**FBA reports are not in the standard set.** They need the *Amazon Fulfillment* role and
an FBA-enrolled account. On a merchant-fulfilled or Easy Ship account they return 403 and
always will. They're still registered under the `fba` group so `probe_access()` can show
you that plainly rather than leaving you guessing.

`marketplaceParticipations` in `whoami()` returning 403 is also expected — that needs
*Selling Partner Insights*, which you don't have. It's cosmetic.

## Install as a desktop extension

Editing `claude_desktop_config.json` does **not** work for Cowork sessions — servers
declared there are never spawned and leave no logs. Install the packaged extension
instead:

**Settings → Extensions → Advanced settings → install from file** → pick
`amazon-spapi.mcpb`.

Then fill in the settings it prompts for: Python path, the three LWA credentials,
region, marketplace, output folder, and optionally a time zone.

Credentials live in the OS keychain, not in a `.env` on disk.

### Running it standalone instead

If you want to run it outside the extension host:

```
pip install "mcp[cli]" requests python-dotenv
copy .env.example .env      # then fill it in
python spapi_server.py
```

The script loads `.env` from beside itself, so the working directory doesn't matter.

## Connect the output folder

In the Claude desktop app, click **Add folder** and pick your output folder. This is how
Claude reads full report files — the tools only return small previews so a large
catalogue can't flood the conversation.

## The tools

### Diagnostics

| Tool | What it does |
|---|---|
| `whoami` | Verify credentials, region, marketplace. Returns no secrets. |
| `probe_access` | Actually try every report type and endpoint, report allowed vs forbidden. |
| `list_report_types` | Report keys available, grouped by the role each needs. |
| `list_saved_reports` | What's already downloaded to the output folder. |

`probe_access()` is the one to run after any role change or re-authorization. It tells
you what your token really permits instead of what the documentation implies.

### Reports (queued — minutes)

| Tool | What it does |
|---|---|
| `pull_report` | Pull one report, save dated to the output folder, return a preview. |
| `pull_standard_set` | `listings_all` + `orders` + `returns` + `sales_and_traffic`. |

Report keys by group:

- **listings** — `listings_all`, `listings_active`, `listings_inactive`,
  `listings_cancelled`, `listings_open`
- **orders** — `orders` (by last update), `orders_by_order_date` (by purchase date)
- **returns** — `returns`, `return_attributes` *(60-day ceiling, enforced automatically)*
- **analytics** — `sales_and_traffic`, `search_terms`, `market_basket`,
  `repeat_purchase`, `search_catalog_performance`
- **fba** — `fba_inventory`, `fba_inventory_health`, `fba_restock` *(will 403)*

### Live APIs (seconds)

| Tool | What it does |
|---|---|
| `list_orders` | Recent orders with status breakdown. No report queue. |
| `get_order_items` | Line items for one order: SKU, ASIN, quantity, price. |
| `sales_metrics` | Order count, units and revenue per bucket. May need Selling Partner Insights. |

Use `list_orders` for "what came in today". Use the `orders` report for bulk analysis —
it has far more columns.

## Things that will bite you

- **Span caps.** Amazon refuses order reports over 30 days and returns reports over 60,
  with a header row reading "Date range exceeded" rather than an error. `pull_report`
  splits longer spans into consecutive windows and stitches the rows back together, so a
  90-day orders pull is three requests and takes several minutes. `start_date` /
  `end_date` let you pull an explicit window instead of a rolling `days_back`.
- **Rate limits.** `createReport` allows roughly one call per minute with a burst of ~15.
  `pull_standard_set` and multi-window pulls sleep 60s between requests; don't remove it.
- **`CANCELLED` status** almost always means "no data for that period", not an error.
- **Roles are frozen at authorization time.** Adding a role in Developer Central does
  nothing to an existing refresh token — you must re-authorize and paste the new token
  into the extension settings. This is the single most common cause of a mystery 403.
- **Brand Analytics needs Brand Registry** on top of the role.
- **Period-aligned reports.** Brand Analytics reports only accept whole periods, so
  `days_back` is snapped to the last complete week or month. The returned `date_range`
  tells you what was actually requested.
- **Sales and traffic returns JSON**, not TSV. It's saved as `.json` and previewed
  differently. Everything else is tab-delimited.
- **Columns move.** Amazon adds and renames report columns without notice. The parser
  reads headers dynamically rather than by position, but check the preview if numbers
  look odd.
- **Reports are queued.** `pull_report` waits up to 15 minutes before giving up.

## Running it unattended

If you want a fresh pull waiting every Monday, set up a scheduled task in Claude that
calls `pull_standard_set` and then builds the analysis from the saved files. Keeping the
pull and the analysis in one task is simplest; if the machine is asleep the task just
runs late rather than failing.
