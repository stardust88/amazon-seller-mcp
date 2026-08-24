"""
Amazon SP-API MCP server.

Runs on YOUR machine, holds YOUR credentials, exposes Seller Central data to
Claude Cowork over the desktop MCP bridge.

Scoped to two granted LWA roles:

  Inventory and Order Tracking -> listings, orders, returns
  Brand Analytics             -> sales & traffic, search terms, market basket,
                                 repeat purchase, search catalog performance

FBA report types are deliberately kept in a separate "fba" group. They require
the Amazon Fulfillment role AND actual FBA enrolment; for a merchant-fulfilled
or Easy Ship seller they return 403. They are listed so probe_access() can show
that plainly, not because they are expected to work.

Tools:
  whoami()                          - verify credentials + config, pull nothing
  probe_access()                    - empirically test what this app may call
  list_report_types(group)          - what can be pulled, grouped by role
  pull_report(key, ...)             - pull one report, save TSV, return preview
  pull_standard_set(days_back)      - the merchant-fulfilled analysis set
  list_saved_reports()              - what is already on disk in OUTPUT_DIR
  list_orders(days_back, ...)       - Orders API, seconds not minutes
  get_order_items(order_id)         - line items for one order
  sales_metrics(days_back, ...)     - Sales API aggregated order metrics

Design notes:
  - Reports are saved as TSV files in OUTPUT_DIR. Tools return a small preview
    (columns + first rows + row count), never the whole file, so a big catalogue
    can't blow up the conversation. Connect OUTPUT_DIR as a folder in the Claude
    desktop app and Claude reads the full files from there.
  - Credentials are read from the environment (Desktop Extension user_config) or
    from a .env beside this script, and are never returned by any tool.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load .env from beside this script, NOT from the working directory. When run as
# a Desktop Extension there is no .env at all and the values arrive as real
# environment variables instead - load_dotenv simply finds nothing, which is fine.
SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

REGION_ENDPOINTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}

MARKETPLACE_IDS = {
    "US": "ATVPDKIKX0DER",
    "CA": "A2EUQ1WTGCTBG2",
    "MX": "A1AM78C64UM0Y8",
    "BR": "A2Q3Y263D00KWC",
    "UK": "A1F83G8C2ARO7P",
    "DE": "A1PA6795UKMFR9",
    "FR": "A13V1IB3VIYZZH",
    "IT": "APJ6JRA9NG5V4",
    "ES": "A1RKKUPIHCS9HS",
    "NL": "A1805IZSGTT6HS",
    "SE": "A2NODRKZP88ZB9",
    "PL": "A1C3SOZRARQ6R3",
    "AE": "A2VIGQ35RCS4UG",
    "SA": "A17E79C6D8DWNP",
    "IN": "A21TJRUUN4KGV",
    "JP": "A1VC38T7YXB528",
    "AU": "A39IBJ37TRP1C6",
    "SG": "A19VAU5U5O7RUS",
}


@dataclass(frozen=True)
class ReportSpec:
    report_type: str
    group: str
    role: str
    description: str
    needs_dates: bool = False
    max_days: int | None = None
    # Brand Analytics reports must start/end on period boundaries.
    period: str | None = None  # "DAY" | "WEEK" | "MONTH"
    options: dict[str, str] = field(default_factory=dict)


REPORTS: dict[str, ReportSpec] = {
    # ---- Listings: role "Inventory and Order Tracking" --------------------
    "listings_all": ReportSpec(
        report_type="GET_MERCHANT_LISTINGS_ALL_DATA",
        group="listings",
        role="Inventory and Order Tracking",
        description=(
            "All listings: every SKU with ASIN, price, quantity and status. "
            "This is the merchant-fulfilled equivalent of an FBA inventory report."
        ),
    ),
    "listings_active": ReportSpec(
        report_type="GET_MERCHANT_LISTINGS_DATA",
        group="listings",
        role="Inventory and Order Tracking",
        description="Active listings only: currently buyable SKUs with price and quantity.",
    ),
    "listings_inactive": ReportSpec(
        report_type="GET_MERCHANT_LISTINGS_INACTIVE_DATA",
        group="listings",
        role="Inventory and Order Tracking",
        description="Inactive listings: SKUs that are not currently buyable. Finds dead stock.",
    ),
    "listings_cancelled": ReportSpec(
        report_type="GET_MERCHANT_CANCELLED_LISTINGS_DATA",
        group="listings",
        role="Inventory and Order Tracking",
        description="Cancelled or closed listings.",
    ),
    "listings_open": ReportSpec(
        report_type="GET_FLAT_FILE_OPEN_LISTINGS_DATA",
        group="listings",
        role="Inventory and Order Tracking",
        description="Open listings, compact form: sku, asin, price, quantity only.",
    ),
    # ---- Orders: role "Inventory and Order Tracking" ----------------------
    "orders": ReportSpec(
        report_type="GET_FLAT_FILE_ALL_ORDERS_DATA_BY_LAST_UPDATE_GENERAL",
        group="orders",
        role="Inventory and Order Tracking",
        description=(
            "All orders by LAST UPDATE date. Catches status changes on older orders, "
            "so it is the right choice for keeping a rolling picture current."
        ),
        needs_dates=True,
        max_days=30,
    ),
    "orders_by_order_date": ReportSpec(
        report_type="GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL",
        group="orders",
        role="Inventory and Order Tracking",
        description=(
            "All orders by PURCHASE date. Use this for true period sales - each order "
            "appears in the period it was actually placed."
        ),
        needs_dates=True,
        max_days=30,
    ),
    # ---- Returns: role "Inventory and Order Tracking", 60-day ceiling -----
    "returns": ReportSpec(
        report_type="GET_FLAT_FILE_RETURNS_DATA_BY_RETURN_DATE",
        group="returns",
        role="Inventory and Order Tracking",
        description="Merchant-fulfilled returns by return date: RMA, ASIN, reason code.",
        needs_dates=True,
        max_days=60,
    ),
    "return_attributes": ReportSpec(
        report_type="GET_FLAT_FILE_MFN_SKU_RETURN_ATTRIBUTES_REPORT",
        group="returns",
        role="Inventory and Order Tracking",
        description="Per-SKU return attributes and settings.",
        needs_dates=True,
        max_days=60,
    ),
    # ---- Brand Analytics: role "Brand Analytics" (needs Brand Registry) ---
    "sales_and_traffic": ReportSpec(
        report_type="GET_SALES_AND_TRAFFIC_REPORT",
        group="analytics",
        role="Brand Analytics",
        description=(
            "Sales and traffic by ASIN/SKU: sessions, page views, buy box percentage and "
            "unit session percentage (conversion). Probably the most useful report you have."
        ),
        needs_dates=True,
        period="DAY",
        options={"dateGranularity": "DAY", "asinGranularity": "CHILD"},
    ),
    "search_terms": ReportSpec(
        report_type="GET_BRAND_ANALYTICS_SEARCH_TERMS_REPORT",
        group="analytics",
        role="Brand Analytics",
        description="Top search terms with click and conversion share. Requires Brand Registry.",
        needs_dates=True,
        period="WEEK",
        options={"reportPeriod": "WEEK"},
    ),
    "market_basket": ReportSpec(
        report_type="GET_BRAND_ANALYTICS_MARKET_BASKET_REPORT",
        group="analytics",
        role="Brand Analytics",
        description="What customers buy alongside your products. Requires Brand Registry.",
        needs_dates=True,
        period="WEEK",
        options={"reportPeriod": "WEEK"},
    ),
    "repeat_purchase": ReportSpec(
        report_type="GET_BRAND_ANALYTICS_REPEAT_PURCHASE_REPORT",
        group="analytics",
        role="Brand Analytics",
        description="Repeat purchase behaviour by ASIN. Requires Brand Registry.",
        needs_dates=True,
        period="WEEK",
        options={"reportPeriod": "WEEK"},
    ),
    "search_catalog_performance": ReportSpec(
        report_type="GET_BRAND_ANALYTICS_SEARCH_CATALOG_PERFORMANCE_REPORT",
        group="analytics",
        role="Brand Analytics",
        description="Catalogue-level search performance. Requires Brand Registry.",
        needs_dates=True,
        period="WEEK",
        options={"reportPeriod": "WEEK"},
    ),
    # ---- FBA: role "Amazon Fulfillment" - NOT granted, kept for diagnosis --
    "fba_inventory": ReportSpec(
        report_type="GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA",
        group="fba",
        role="Amazon Fulfillment (not granted)",
        description="FBA inventory on hand. 403s without the Amazon Fulfillment role.",
    ),
    "fba_inventory_health": ReportSpec(
        report_type="GET_FBA_INVENTORY_PLANNING_DATA",
        group="fba",
        role="Amazon Fulfillment (not granted)",
        description="FBA inventory health and age. 403s without the Amazon Fulfillment role.",
    ),
    "fba_restock": ReportSpec(
        report_type="GET_RESTOCK_INVENTORY_RECOMMENDATIONS_REPORT",
        group="fba",
        role="Amazon Fulfillment (not granted)",
        description="FBA restock recommendations. 403s without the Amazon Fulfillment role.",
    ),
}

# Merchant-fulfilled analysis set. Deliberately excludes every FBA report.
STANDARD_SET = ["listings_all", "orders", "returns", "sales_and_traffic"]

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", Path.home() / "AmazonReports")).expanduser()
ENV_FILE = SCRIPT_DIR / ".env"

mcp = FastMCP("amazon-spapi")


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing {name}. Set it in the extension's settings "
            f"(Settings -> Extensions -> Amazon SP-API), or in a .env beside this script."
        )
    return value


def _access_token() -> str:
    """Exchange the refresh token for an access token, cached until ~1 min before expiry."""
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    response = requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": _require_env("LWA_REFRESH_TOKEN"),
            "client_id": _require_env("LWA_CLIENT_ID"),
            "client_secret": _require_env("LWA_CLIENT_SECRET"),
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"LWA token exchange failed ({response.status_code}). "
            f"Check client id/secret/refresh token. Response: {response.text[:400]}"
        )

    payload = response.json()
    _token_cache["access_token"] = payload["access_token"]
    _token_cache["expires_at"] = time.time() + payload.get("expires_in", 3600) - 60
    return _token_cache["access_token"]


def _endpoint() -> str:
    region = os.getenv("SPAPI_REGION", "na").lower()
    if region not in REGION_ENDPOINTS:
        raise RuntimeError(
            f"SPAPI_REGION must be one of {list(REGION_ENDPOINTS)}, got {region!r}. "
            f"na = North America, eu = Europe/UK/India/Middle East, fe = Japan/Australia/Singapore."
        )
    return REGION_ENDPOINTS[region]


def _marketplace_ids() -> list[str]:
    raw = os.getenv("SPAPI_MARKETPLACE_ID", "").strip()
    if not raw:
        raise RuntimeError(
            "Set the marketplace in the extension settings. Either a marketplace id, "
            f"or a country code from: {', '.join(sorted(MARKETPLACE_IDS))}"
        )
    return [MARKETPLACE_IDS.get(t.strip().upper(), t.strip()) for t in raw.split(",") if t.strip()]


def _call(method: str, path: str, **kwargs) -> requests.Response:
    """SP-API call. Auth is a single LWA bearer header - no AWS SigV4 needed."""
    headers = {
        "x-amz-access-token": _access_token(),
        "content-type": "application/json",
        "accept": "application/json",
    }
    headers.update(kwargs.pop("headers", {}))
    return requests.request(
        method, f"{_endpoint()}{path}", headers=headers, timeout=60, **kwargs
    )


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _explain_403(report_type: str, spec: ReportSpec | None = None) -> str:
    role = spec.role if spec else "an additional"
    return (
        f"403 Unauthorized for {report_type}. The app's authorization does not cover it - "
        f"it needs the '{role}' role. Two things cause this: the role was never granted, or "
        f"it was added in Developer Central AFTER the refresh token was issued (roles are "
        f"bound at authorization time, so you must re-authorize and paste the new refresh "
        f"token into the extension settings). FBA reports additionally require the account "
        f"to actually be FBA-enrolled."
    )


# --------------------------------------------------------------------------
# Date handling
# --------------------------------------------------------------------------


def _align_period(period: str, days_back: int) -> tuple[str, str]:
    """Brand Analytics reports must start and end on period boundaries.

    Returns the most recent COMPLETE period(s) covering roughly days_back days.
    Amazon's Brand Analytics weeks run Sunday to Saturday.
    """
    today = datetime.now(timezone.utc).date()

    if period == "DAY":
        # Yesterday is the last day with settled data.
        end = today - timedelta(days=1)
        start = end - timedelta(days=max(days_back, 1) - 1)
        return start.isoformat(), end.isoformat()

    if period == "WEEK":
        # weekday(): Mon=0 .. Sun=6. Amazon weeks run Sun..Sat, so step back to
        # the most recent completed Saturday.
        days_since_saturday = (today.weekday() - 5) % 7
        end = today - timedelta(days=days_since_saturday or 7)
        weeks = max(1, round(max(days_back, 7) / 7))
        start = end - timedelta(days=7 * weeks - 1)
        return start.isoformat(), end.isoformat()

    if period == "MONTH":
        end = today.replace(day=1) - timedelta(days=1)  # last day of previous month
        months = max(1, round(max(days_back, 30) / 30))
        start = end.replace(day=1)
        for _ in range(months - 1):
            start = (start - timedelta(days=1)).replace(day=1)
        return start.isoformat(), end.isoformat()

    raise ValueError(f"Unknown period {period!r}")


def _date_range(spec: ReportSpec, days_back: int) -> tuple[str | None, str | None]:
    if not spec.needs_dates:
        return None, None

    if spec.period:
        return _align_period(spec.period, days_back)

    capped = min(days_back, spec.max_days) if spec.max_days else days_back
    now = datetime.now(timezone.utc)
    return _iso(now - timedelta(days=capped)), _iso(now)


def _windows(spec: ReportSpec, days_back: int, start_date: str, end_date: str) -> list[tuple[str, str]]:
    """Split the requested span into windows Amazon will actually accept.

    Several report types cap the range they will serve - orders at 30 days, returns at
    60. Rather than silently truncating, split the span into consecutive windows and let
    pull_report stitch the results back together.
    """
    if not spec.needs_dates:
        return [(None, None)]  # type: ignore[list-item]

    if spec.period:
        # Brand Analytics reports are snapped to whole periods and take the span as-is.
        return [_align_period(spec.period, days_back)]

    now = datetime.now(timezone.utc)
    if end_date:
        end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
    else:
        end = now
    if start_date:
        start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    else:
        start = end - timedelta(days=days_back)

    if start >= end:
        raise ValueError(f"start_date {start.date()} must be before end_date {end.date()}")

    step = timedelta(days=spec.max_days) if spec.max_days else (end - start)
    windows: list[tuple[str, str]] = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + step, end)
        windows.append((_iso(cursor), _iso(window_end)))
        cursor = window_end
    return windows


def _merge_tsv(chunks: list[str]) -> str:
    """Concatenate TSV chunks, keeping one header and dropping duplicate rows."""
    chunks = [c for c in chunks if c.strip()]
    if not chunks:
        return ""
    if len(chunks) == 1:
        return chunks[0]

    header: str | None = None
    seen: set[str] = set()
    body: list[str] = []
    for chunk in chunks:
        lines = chunk.splitlines()
        if not lines:
            continue
        if header is None:
            header = lines[0]
        for line in lines[1:]:
            if line and line not in seen:
                seen.add(line)
                body.append(line)
    return "\n".join([header or ""] + body) + "\n"


# --------------------------------------------------------------------------
# Report workflow: createReport -> poll getReport -> getReportDocument
# --------------------------------------------------------------------------


def _create_report(
    report_type: str,
    marketplace_ids: list[str],
    start: str | None,
    end: str | None,
    options: dict[str, str] | None = None,
    spec: ReportSpec | None = None,
) -> str:
    body: dict[str, Any] = {"reportType": report_type, "marketplaceIds": marketplace_ids}
    if start:
        body["dataStartTime"] = start
    if end:
        body["dataEndTime"] = end
    if options:
        body["reportOptions"] = options

    response = _call("POST", "/reports/2021-06-30/reports", json=body)
    if response.status_code == 403:
        raise RuntimeError(_explain_403(report_type, spec))
    if response.status_code not in (200, 202):
        raise RuntimeError(
            f"createReport failed for {report_type} ({response.status_code}): "
            f"{response.text[:400]}"
        )
    return response.json()["reportId"]


def _wait_for_report(report_id: str, timeout_seconds: int = 900) -> dict[str, Any]:
    """Poll until the report is done. Amazon queues these; minutes is normal."""
    deadline = time.time() + timeout_seconds
    delay = 5.0
    while time.time() < deadline:
        response = _call("GET", f"/reports/2021-06-30/reports/{report_id}")
        if response.status_code == 429:
            time.sleep(delay := min(delay * 2, 60))
            continue
        if response.status_code != 200:
            raise RuntimeError(f"getReport failed ({response.status_code}): {response.text[:300]}")

        report = response.json()
        status = report.get("processingStatus")
        if status == "DONE":
            return report
        if status in ("CANCELLED", "FATAL"):
            raise RuntimeError(
                f"Report {report_id} finished with status {status}. "
                f"CANCELLED usually means there was no data for the period."
            )
        time.sleep(delay := min(delay * 1.4, 30))

    raise TimeoutError(
        f"Report {report_id} still processing after {timeout_seconds}s. "
        f"It may finish later - large date ranges can take a while."
    )


def _download_document(document_id: str) -> str:
    response = _call("GET", f"/reports/2021-06-30/documents/{document_id}")
    if response.status_code != 200:
        raise RuntimeError(
            f"getReportDocument failed ({response.status_code}): {response.text[:300]}"
        )
    document = response.json()

    payload = requests.get(document["url"], timeout=180).content
    if document.get("compressionAlgorithm") == "GZIP":
        payload = gzip.decompress(payload)

    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _preview(text: str, rows: int = 5) -> dict[str, Any]:
    if text.lstrip()[:1] in ("{", "["):
        # Sales and traffic and some Brand Analytics reports come back as JSON.
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"format": "unknown", "row_count": 0, "sample_rows": []}
        return {
            "format": "json",
            "top_level_keys": list(parsed) if isinstance(parsed, dict) else None,
            "row_count": len(parsed) if isinstance(parsed, list) else None,
            "sample": json.dumps(parsed, indent=2)[:1500],
        }

    reader = csv.reader(io.StringIO(text), delimiter="\t")
    try:
        header = next(reader)
    except StopIteration:
        return {"format": "tsv", "columns": [], "row_count": 0, "sample_rows": []}

    sample, count = [], 0
    for row in reader:
        count += 1
        if len(sample) < rows:
            sample.append(dict(zip(header, row)))
    return {"format": "tsv", "columns": header, "row_count": count, "sample_rows": sample}


# --------------------------------------------------------------------------
# Tools: diagnostics
# --------------------------------------------------------------------------


@mcp.tool()
def whoami() -> dict[str, Any]:
    """Verify credentials and configuration without pulling any report data.

    Returns the region endpoint, resolved marketplace ids, output directory and
    whether the refresh-token exchange currently succeeds. Never returns secrets.
    """
    result: dict[str, Any] = {
        "python": sys.executable,
        "script_dir": str(SCRIPT_DIR),
        "env_file_found": ENV_FILE.is_file(),
        "endpoint": _endpoint(),
        "marketplace_ids": _marketplace_ids(),
        "output_dir": str(OUTPUT_DIR),
        "output_dir_exists": OUTPUT_DIR.is_dir(),
        "roles_this_server_assumes": ["Inventory and Order Tracking", "Brand Analytics"],
        "credentials_present": {
            name: bool(os.getenv(name))
            for name in ("LWA_CLIENT_ID", "LWA_CLIENT_SECRET", "LWA_REFRESH_TOKEN")
        },
    }
    try:
        _access_token()
        result["token_exchange"] = "ok"
    except Exception as exc:
        result["token_exchange"] = f"failed: {exc}"
        return result

    try:
        response = _call("GET", "/sellers/v1/marketplaceParticipations")
        result["marketplace_participations_status"] = response.status_code
        if response.status_code == 200:
            result["selling_in"] = [
                p["marketplace"].get("countryCode") for p in response.json().get("payload", [])
            ]
        elif response.status_code == 403:
            result["marketplace_participations_note"] = (
                "403 is expected - this endpoint needs the 'Selling Partner Insights' role, "
                "which is not among your granted roles. Cosmetic only; it does not affect "
                "reports or orders."
            )
    except Exception as exc:
        result["marketplace_participations_status"] = f"error: {exc}"

    return result


@mcp.tool()
def probe_access(include_fba: bool = False, include_analytics: bool = True) -> dict[str, Any]:
    """Empirically test which reports and endpoints this app is actually allowed to call.

    Submits a create request for each report type and records whether Amazon accepts it
    (202) or refuses it (403), without waiting for any report to finish. Also pings the
    Orders and Sellers endpoints. Use this after changing roles or re-authorizing to see
    exactly what opened up, instead of guessing from documentation.

    Args:
        include_fba: also probe the FBA reports, which need the Amazon Fulfillment role.
        include_analytics: also probe the Brand Analytics reports.

    Takes roughly a minute. createReport allows a burst of about 15 calls.
    """
    marketplaces = _marketplace_ids()
    reports: dict[str, Any] = {}

    for key, spec in REPORTS.items():
        if spec.group == "fba" and not include_fba:
            continue
        if spec.group == "analytics" and not include_analytics:
            continue

        start, end = _date_range(spec, 7)
        body: dict[str, Any] = {"reportType": spec.report_type, "marketplaceIds": marketplaces}
        if start:
            body["dataStartTime"] = start
        if end:
            body["dataEndTime"] = end
        if spec.options:
            body["reportOptions"] = spec.options

        try:
            response = _call("POST", "/reports/2021-06-30/reports", json=body)
            code = response.status_code
            if code in (200, 202):
                verdict = "allowed"
            elif code == 403:
                verdict = "forbidden (missing role)"
            elif code == 429:
                verdict = "unknown (rate limited, retry later)"
            else:
                verdict = f"error {code}: {response.text[:160]}"
        except Exception as exc:
            verdict = f"error: {exc}"

        reports[key] = {"report_type": spec.report_type, "role": spec.role, "result": verdict}
        time.sleep(1.0)

    endpoints: dict[str, Any] = {}
    probes = [
        ("sellers/marketplaceParticipations", "/sellers/v1/marketplaceParticipations", None),
        (
            "orders/getOrders",
            "/orders/v0/orders",
            {
                "MarketplaceIds": ",".join(marketplaces),
                "LastUpdatedAfter": _iso(datetime.now(timezone.utc) - timedelta(days=1)),
                "MaxResultsPerPage": 1,
            },
        ),
    ]
    for name, path, params in probes:
        try:
            response = _call("GET", path, params=params)
            endpoints[name] = (
                "allowed"
                if response.status_code == 200
                else f"{response.status_code}: {response.text[:160]}"
            )
        except Exception as exc:
            endpoints[name] = f"error: {exc}"

    return {
        "reports": reports,
        "endpoints": endpoints,
        "summary": {
            "allowed_reports": [k for k, v in reports.items() if v["result"] == "allowed"],
            "forbidden_reports": [
                k for k, v in reports.items() if v["result"].startswith("forbidden")
            ],
        },
        "note": (
            "Probed reports were actually created and will finish in the background. "
            "That is harmless - they are simply not downloaded here."
        ),
    }


@mcp.tool()
def list_report_types(group: str = "") -> dict[str, Any]:
    """List the report keys this server can pull, with what each one contains.

    Args:
        group: optionally filter to one of "listings", "orders", "returns",
            "analytics", "fba". Omit for everything.
    """
    return {
        "reports": {
            key: {
                "report_type": spec.report_type,
                "group": spec.group,
                "role": spec.role,
                "needs_date_range": spec.needs_dates,
                "max_days": spec.max_days,
                "report_options": spec.options or None,
                "description": spec.description,
            }
            for key, spec in REPORTS.items()
            if not group or spec.group == group
        },
        "groups": sorted({spec.group for spec in REPORTS.values()}),
        "standard_set": STANDARD_SET,
        "output_dir": str(OUTPUT_DIR),
    }


@mcp.tool()
def list_saved_reports() -> dict[str, Any]:
    """List report files already downloaded to the output folder, newest first."""
    if not OUTPUT_DIR.is_dir():
        return {"output_dir": str(OUTPUT_DIR), "exists": False, "files": []}

    files = []
    for path in OUTPUT_DIR.iterdir():
        if path.is_file() and path.suffix.lower() in (".tsv", ".json", ".csv"):
            stat = path.stat()
            files.append(
                {
                    "name": path.name,
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                }
            )
    files.sort(key=lambda f: f["modified"], reverse=True)
    return {"output_dir": str(OUTPUT_DIR), "exists": True, "files": files}


# --------------------------------------------------------------------------
# Tools: reports
# --------------------------------------------------------------------------


@mcp.tool()
def pull_report(
    key: str,
    days_back: int = 30,
    preview_rows: int = 5,
    report_options: str = "",
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    """Pull one Amazon report, save it dated to the output folder, and return a small preview.

    Amazon caps the span some reports will serve - orders at 30 days, returns at 60. For a
    longer span this splits the request into consecutive windows, pulls each, and stitches
    the rows back into one file. A 90-day orders pull is three windows and takes several
    minutes, because createReport is rate-limited to roughly one call per minute.

    Args:
        key: a report key from list_report_types, e.g. "listings_all", "orders",
            "returns", "sales_and_traffic".
        days_back: how far back to reach, for reports that take a date range. Ignored if
            start_date is given. Brand Analytics reports are snapped to whole periods.
        preview_rows: how many sample rows to return inline. Keep this small - the full
            file is on disk for Claude to read from the connected output folder.
        report_options: optional JSON object overriding reportOptions, for example
            '{"asinGranularity": "SKU"}' for sales_and_traffic.
        start_date: optional explicit start, "YYYY-MM-DD". Overrides days_back.
        end_date: optional explicit end, "YYYY-MM-DD". Defaults to now.

    Returns the saved file path, column names, row count and sample rows.
    """
    if key not in REPORTS:
        raise ValueError(f"Unknown report key {key!r}. Valid keys: {', '.join(REPORTS)}")

    spec = REPORTS[key]
    options = dict(spec.options)
    if report_options:
        try:
            overrides = json.loads(report_options)
        except json.JSONDecodeError as exc:
            raise ValueError(f"report_options must be a JSON object: {exc}") from exc
        if not isinstance(overrides, dict):
            raise ValueError("report_options must be a JSON object")
        options.update({str(k): str(v) for k, v in overrides.items()})

    windows = _windows(spec, days_back, start_date, end_date)
    marketplaces = _marketplace_ids()

    chunks: list[str] = []
    window_status: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(windows):
        if index:
            time.sleep(60)  # createReport is limited to roughly one call per minute
        try:
            report_id = _create_report(spec.report_type, marketplaces, start, end, options or None, spec)
            report = _wait_for_report(report_id)
            text = _download_document(report["reportDocumentId"])
            chunks.append(text)
            rows = max(0, len(text.splitlines()) - 1) if text.lstrip()[:1] not in ("{", "[") else None
            window_status.append({"start": start, "end": end, "status": "ok", "rows": rows})
        except Exception as exc:
            # One empty or refused window should not lose the windows that worked.
            window_status.append({"start": start, "end": end, "status": "failed", "error": str(exc)})

    if not chunks:
        raise RuntimeError(
            f"No window of {key} returned data. Per-window detail: {json.dumps(window_status)}"
        )

    is_json = chunks[0].lstrip()[:1] in ("{", "[")
    if is_json and len(chunks) > 1:
        raise RuntimeError(
            f"{key} returns JSON and cannot be stitched across windows. Request a shorter span."
        )
    text = chunks[0] if is_json else _merge_tsv(chunks)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    suffix = "json" if is_json else "tsv"
    if spec.needs_dates and windows[0][0]:
        span = f"_{windows[0][0][:10]}_to_{windows[-1][1][:10]}"
    else:
        span = ""
    path = OUTPUT_DIR / f"{stamp}_{key}{span}.{suffix}"
    path.write_text(text, encoding="utf-8")

    return {
        "key": key,
        "report_type": spec.report_type,
        "group": spec.group,
        "saved_to": str(path),
        "date_range": (
            {"start": windows[0][0], "end": windows[-1][1]}
            if spec.needs_dates
            else "point-in-time snapshot"
        ),
        "windows": window_status if len(windows) > 1 else None,
        "report_options": options or None,
        **_preview(text, preview_rows),
    }


@mcp.tool()
def pull_standard_set(days_back: int = 30) -> dict[str, Any]:
    """Pull the merchant-fulfilled analysis set: listings, orders, returns, sales and traffic.

    Amazon rate-limits report creation to roughly one per minute, so this takes a few
    minutes. Each report is saved to the output folder; the return value is a per-report
    status summary, not the data itself. FBA reports are deliberately excluded.
    """
    results = []
    for index, key in enumerate(STANDARD_SET):
        if index:
            time.sleep(60)  # respect the createReport rate limit
        try:
            outcome = pull_report(key, days_back=days_back, preview_rows=2)
            results.append(
                {
                    "key": key,
                    "status": "ok",
                    "saved_to": outcome["saved_to"],
                    "row_count": outcome.get("row_count"),
                }
            )
        except Exception as exc:
            results.append({"key": key, "status": "failed", "error": str(exc)})

    return {
        "output_dir": str(OUTPUT_DIR),
        "results": results,
        "next_step": (
            "Connect the output folder in the Claude desktop app, then ask Claude to build "
            "the analysis from these files."
        ),
    }


# --------------------------------------------------------------------------
# Tools: live APIs (seconds, not minutes)
# --------------------------------------------------------------------------


@mcp.tool()
def list_orders(days_back: int = 7, statuses: str = "", max_orders: int = 200) -> dict[str, Any]:
    """List recent orders via the Orders API. Returns in seconds, unlike the report queue.

    Use this for "what came in today" questions. For bulk analysis pull the "orders"
    report instead - it has far more columns.

    Args:
        days_back: how far back to look, by last-updated date.
        statuses: optional comma-separated filter, e.g. "Unshipped,PartiallyShipped".
            Valid: PendingAvailability, Pending, Unshipped, PartiallyShipped, Shipped,
            Canceled, Unfulfillable, InvoiceUnconfirmed.
        max_orders: stop after this many orders.

    Buyer personal information is omitted - that needs the restricted
    Direct to Consumer Shipping role.
    """
    base_params: dict[str, Any] = {
        "MarketplaceIds": ",".join(_marketplace_ids()),
        "LastUpdatedAfter": _iso(datetime.now(timezone.utc) - timedelta(days=days_back)),
        "MaxResultsPerPage": 100,
    }
    if statuses.strip():
        base_params["OrderStatuses"] = ",".join(
            s.strip() for s in statuses.split(",") if s.strip()
        )

    orders: list[dict[str, Any]] = []
    next_token: str | None = None
    pages = 0

    while len(orders) < max_orders and pages < 10:
        params = (
            {"MarketplaceIds": base_params["MarketplaceIds"], "NextToken": next_token}
            if next_token
            else base_params
        )
        response = _call("GET", "/orders/v0/orders", params=params)

        if response.status_code == 403:
            raise RuntimeError(
                "403 from the Orders API. This needs the 'Inventory and Order Tracking' role; "
                "if you recently changed roles, re-authorize and update the refresh token."
            )
        if response.status_code == 429:
            time.sleep(2)
            continue
        if response.status_code != 200:
            raise RuntimeError(f"getOrders failed ({response.status_code}): {response.text[:300]}")

        payload = response.json().get("payload", {})
        for order in payload.get("Orders", []):
            total = order.get("OrderTotal") or {}
            address = order.get("ShippingAddress") or {}
            orders.append(
                {
                    "amazon_order_id": order.get("AmazonOrderId"),
                    "purchase_date": order.get("PurchaseDate"),
                    "last_update_date": order.get("LastUpdateDate"),
                    "order_status": order.get("OrderStatus"),
                    "fulfillment_channel": order.get("FulfillmentChannel"),
                    "ship_service_level": order.get("ShipServiceLevel"),
                    "items_shipped": order.get("NumberOfItemsShipped"),
                    "items_unshipped": order.get("NumberOfItemsUnshipped"),
                    "order_total": total.get("Amount"),
                    "currency": total.get("CurrencyCode"),
                    "is_prime": order.get("IsPrime"),
                    "is_business_order": order.get("IsBusinessOrder"),
                    "ship_city": address.get("City"),
                    "ship_state": address.get("StateOrRegion"),
                }
            )

        pages += 1
        next_token = payload.get("NextToken")
        if not next_token:
            break
        time.sleep(1)

    orders = orders[:max_orders]
    by_status: dict[str, int] = {}
    for order in orders:
        status = order["order_status"] or "unknown"
        by_status[status] = by_status.get(status, 0) + 1

    return {
        "days_back": days_back,
        "order_count": len(orders),
        "by_status": by_status,
        "orders": orders,
    }


@mcp.tool()
def get_order_items(amazon_order_id: str) -> dict[str, Any]:
    """Get the line items for a single order: SKU, ASIN, quantity, item price.

    Args:
        amazon_order_id: the 3-7-7 style id, for example "405-9161726-7887533".
    """
    response = _call("GET", f"/orders/v0/orders/{amazon_order_id}/orderItems")
    if response.status_code == 403:
        raise RuntimeError("403 from getOrderItems - needs the 'Inventory and Order Tracking' role.")
    if response.status_code != 200:
        raise RuntimeError(f"getOrderItems failed ({response.status_code}): {response.text[:300]}")

    payload = response.json().get("payload", {})
    items = []
    for item in payload.get("OrderItems", []):
        price = item.get("ItemPrice") or {}
        items.append(
            {
                "sku": item.get("SellerSKU"),
                "asin": item.get("ASIN"),
                "title": item.get("Title"),
                "quantity_ordered": item.get("QuantityOrdered"),
                "quantity_shipped": item.get("QuantityShipped"),
                "item_price": price.get("Amount"),
                "currency": price.get("CurrencyCode"),
            }
        )
    return {"amazon_order_id": amazon_order_id, "item_count": len(items), "items": items}


@mcp.tool()
def sales_metrics(days_back: int = 30, granularity: str = "Day") -> dict[str, Any]:
    """Aggregated sales metrics from the Sales API: order count, units and revenue per bucket.

    Much faster than a report when you only need totals over time.

    Args:
        days_back: how far back to aggregate.
        granularity: Hour, Day, Week, Month, Year or Total.

    Note: this endpoint may require the 'Selling Partner Insights' role. If it returns 403,
    pull the "orders" report and aggregate instead - probe_access() shows which you have.
    """
    valid = {"Hour", "Day", "Week", "Month", "Year", "Total"}
    granularity = granularity.capitalize()
    if granularity not in valid:
        raise ValueError(f"granularity must be one of {sorted(valid)}")

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=days_back)
    params = {
        "marketplaceIds": ",".join(_marketplace_ids()),
        "interval": f"{_iso(start)}--{_iso(now)}".replace("Z", "-00:00"),
        "granularity": granularity,
    }
    if granularity != "Total":
        params["granularityTimeZone"] = os.getenv("SPAPI_TIMEZONE", "UTC")

    response = _call("GET", "/sales/v1/orderMetrics", params=params)
    if response.status_code == 403:
        raise RuntimeError(
            "403 from the Sales API. It needs the 'Selling Partner Insights' role, which is "
            "not among your granted roles. Pull the 'orders' report and aggregate instead."
        )
    if response.status_code != 200:
        raise RuntimeError(f"getOrderMetrics failed ({response.status_code}): {response.text[:300]}")

    buckets = []
    for entry in response.json().get("payload", []):
        total = entry.get("totalSales") or {}
        buckets.append(
            {
                "interval": entry.get("interval"),
                "order_count": entry.get("orderCount"),
                "unit_count": entry.get("unitCount"),
                "order_item_count": entry.get("orderItemCount"),
                "average_unit_price": (entry.get("averageUnitPrice") or {}).get("amount"),
                "total_sales": total.get("amount"),
                "currency": total.get("currencyCode"),
            }
        )
    return {"granularity": granularity, "days_back": days_back, "buckets": buckets}


if __name__ == "__main__":
    mcp.run()
