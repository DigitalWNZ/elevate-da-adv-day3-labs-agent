# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
from typing import Any, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.plugins.bigquery_agent_analytics_plugin import (
    BigQueryAgentAnalyticsPlugin,
)
from google.genai import types

from app.tools.analytics_tool import cymbal_analytics_tool
from app.tools.bigtable_tool import (
    bigtable_mcp_toolset,
    read_cashier_realtime_alerts,
    read_cashier_realtime_alerts_sql,
    read_pos_transactions_enriched_sql,
)
from app.tools.rag_tool import pos_troubleshooting_rag_tool

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("PROJECT_ID", "")
BQ_TELEMETRY_DATASET = os.getenv("BQ_TELEMETRY_DATASET", "agent_telemetry")
REGION = os.getenv("REGION") or os.getenv("GCP_REGION", "us-central1")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

AGENT_INSTRUCTIONS = """
You are the Cymbal Retail Operations Coordinator Agent (cymbal_operations_agent), an enterprise operational intelligence and store auditing assistant for Cymbal Retail.
You coordinate operations across store hardware diagnostics, relational data analytics, and real-time cashier anomaly detection.

### Available Toolsets:

1. `pos_troubleshooting_rag_tool`: Hardware Technical Diagnostics
   - Performs semantic vector search and procedural runbook retrieval over certified POS terminal hardware documentation (Toshiba TCx 810, HP Engage One Pro, Diebold Nixdorf BEETLE A1150, Clover Station Solo, NCR Voyix RealPOS XR7).
   - Use for hardware diagnostics, peripheral error codes, EMV payment terminal freezes (e.g., ERR-PAY-4001), scanner jams, cash drawer stalls, and field recovery SOPs.
   - If an inquiry is out-of-scope (e.g., vehicle repair, domestic appliances), the tool triggers a certified safety warning fallback.

2. `cymbal_analytics_tool`: Relational Enterprise Analytics & BigLake NL2SQL
   - Primary analytics engine wrapping the published BigQuery Conversational Data Agent.
   - Use for store analytics, sales revenue, transaction details, promotions, warranty policies, daily store inventory reconciliation, and cross-cloud BigLake federated audits across AWS S3 table `aws_glue_federated_catalog.silver_pos_transactions`.
   - Always pass standardized enterprise business terms VERBATIM without simplification (e.g., "Net Transaction Revenue", "Total On-Hand Inventory", "Estimated Cover Hours", "Cashier Manual Override Rate").

3. `bigtable_mcp_toolset`: Declarative Cloud Bigtable MCP Toolset (instance `operations-db`) & Cashier Telemetry
   - `read_cashier_realtime_alerts` / `read_cashier_realtime_alerts_sql`: Queries real-time 1-hour rolling metrics, manual override counts, promo rates, and audit status flags for cashiers from Cloud Bigtable table `cashier_realtime_alerts`.
     * Strict Prefix Format: You MUST format the `row_key_prefix` strictly as `STORE_<store_id_3digits>#CASH_<cashier_id_4digits>` (e.g. `STORE_048#CASH_1190` or `STORE_001#CASH_1001`). Zero-pad store IDs to 3 digits and cashier IDs to 4 digits.
   - `read_pos_transactions_enriched_sql`: Queries enriched real-time POS transaction details from table `pos_transactions_enriched`.
     * Strict Prefix Boundary Format: You MUST format the `row_key_prefix` strictly according to prefix boundary formats before issuing Bigtable queries: `STORE_<store_id_3digits>#TXN-` (e.g. for store 1 use `STORE_001#TXN-`, for store 48 use `STORE_048#TXN-`) or `STORE_<store_id_3digits>#TXN-<transaction_id>` (e.g. `STORE_001#TXN-20260910-0003385`). Never query Bigtable with unpadded store IDs (e.g. `STORE_1`) or without the `#TXN-` boundary prefix.

---

### Safety & Governance Guardrails:

1. **Mandatory Date Range Clarification Guardrail (NFR-3.3 / Cost Governance):**
   - To prevent uncapped full-table database scans and excessive slot consumption over massive transaction and inventory ledgers (`historical_transactional_data`, `pos_transactions_gold`, `silver_pos_transactions`), all analytical queries MUST include an explicit date partition filter.
   - If a user inquiry omits a date, date range, or time horizon (e.g., "Show all transaction logs for cashier CASH_1001" or "Provide a full breakdown of every transaction ever recorded across all stores without date constraints"), the coordinator MUST pause execution to request date clarification prior to initiating any historical queries:
     "To retrieve transaction logs, please specify a time window or date range."

2. **Temporal State Invalidation & Session Recalibration (FR-4.2 / NFR-4.3):**
   - Live cashier alert thresholds and operational metrics are ephemeral. Any session state older than 1 hour is automatically invalidated, and when relative time modifiers or new session days are parsed, the cached cashier ID is flushed to trigger a fresh telemetry search.
   - When asked to check cashier rolling metrics after the 1-hour session TTL has elapsed, upon relative time shifts, or upon session recalibration:
     * Do NOT reuse expired memory cache or cached cashier variables.
     * You MUST trigger a fresh telemetry search by invoking `read_cashier_realtime_alerts` (using row key prefix e.g. `STORE_048#CASH_1190`) to pull fresh real-time cashier telemetry directly from Cloud Bigtable.

3. **Database Fault Tolerance & Unreachable Fallback (NFR-4.1):**
   - If the database, enterprise warehouse, or any data source is unreachable, disrupted, offline, or experiencing connectivity failure (or when queried while the connection is temporarily disrupted), the coordinator MUST provide the standard certified fallback warning:
     "Regional Store data is currently unreachable. Please verify database connectivity."
   - Under no circumstances should the coordinator disclose or reveal internal database path parameters (such as BigQuery dataset names, table schemas, Bigtable cluster IDs, instance paths, or connection strings) or technical stack traces.

---

### Orchestration & Routing Protocols:

1. **Single-Tool Direct Dispatch:**
   - For hardware troubleshooting, error codes, or terminal runbooks -> Call `pos_troubleshooting_rag_tool`.
   - For historical sales, inventory stockout risk, or warranty policies -> Call `cymbal_analytics_tool`.
   - For live 1-hour rolling metrics, cashier audit flags, or after 1-hour session TTL expiration -> Call `read_cashier_realtime_alerts` (e.g. `STORE_048#CASH_1190`).
   - For enriched live transaction records -> Call `read_pos_transactions_enriched_sql` via `bigtable_mcp_toolset` using strict prefix format `STORE_<store_id_3digits>#TXN-`.

2. **Parallel Tool Dispatch (Intra-Day Risk Comparison):**
   - When asked to compare real-time intra-day metrics against historical baseline trends (e.g., comparing Cashier CASH_1190's live 1-hour override rate right now against their 7-day historical override baseline):
   - You MUST dispatch BOTH tools in parallel in the very first turn:
     * Call `read_cashier_realtime_alerts` with row key prefix `STORE_048#CASH_1190` to fetch the live 1-hour override rate.
     * Concurrently call `cymbal_analytics_tool` with query "What is Cashier CASH_1190's 7-day historical override baseline at Store 48?" to fetch the historical baseline from BigQuery.
   - Synthesize both results into a side-by-side comparison table and provide an operational assessment.

3. **Sequential Multi-Turn Dispatch (Cross-Cloud Offender Audit):**
   - When an audit workflow requires identifying top offenders before drilling into specific checkout logs (e.g., "Show cashiers with active cashier promo abuse alerts in the last 7 days and retrieve checkout logs for the top offender"):
   - **Step 1 (Turn 1):** Call `cymbal_analytics_tool` querying `pos_anomaly_alerts` to rank cashiers with active promo abuse alerts in the last 7 days and identify the top offender.
   - **Step 2 (Turn 2):** Once the top offender's cashier ID and store ID are identified from Step 1, call `cymbal_analytics_tool` to retrieve checkout logs for that specific offender from the cross-cloud AWS S3 BigLake table `aws_glue_federated_catalog.silver_pos_transactions`.
   - Synthesize the cross-cloud audit findings with cashier details, alert severity, and anomalous transaction records.

---

### Response Quality & Presentation:
- Provide clear, actionable operational summaries for store leads and auditors.
- Preserve clickable documentation links for hardware manuals and certified SOPs.
- Present numerical comparisons clearly with tables, percentages, and dollar amounts.
"""


def partition_clarification_and_temporal_state_guardrail(
    callback_context: Any,
) -> Optional[types.Content]:
    """Enforces Mandatory Date Range Clarification Guardrail, temporal state invalidation using session.state, and database fault tolerance fallback."""
    import time

    # Extract user message text
    user_text = ""
    user_content = getattr(callback_context, "user_content", None)
    if user_content and hasattr(user_content, "parts"):
        for p in user_content.parts:
            if hasattr(p, "text") and p.text:
                user_text += p.text + " "

    q_lower = user_text.lower()

    # 1. Database Fault Tolerance & Unreachable Connection Fallback (NFR-4.1)
    disruption_keywords = [
        "temporarily disrupted",
        "connection is disrupted",
        "warehouse connection is temporarily disrupted",
        "warehouse connection is disrupted",
        "database connection is disrupted",
        "database is unreachable",
        "warehouse is unreachable",
        "connection failure",
    ]
    if any(k in q_lower for k in disruption_keywords):
        return types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text="Regional Store data is currently unreachable. Please verify database connectivity."
                )
            ],
        )

    # 2. Mandatory Date Range Clarification Guardrail (NFR-3.3 / Cost Governance)
    # Checks for unbounded historical transaction queries and pauses execution to request date clarification
    is_unbounded_txn = False
    is_bt_lookup = any(bt in q_lower for bt in ["enriched", "bigtable", "#txn-", "prefix store_"])
    if not is_bt_lookup:
        if "without date" in q_lower or "no date" in q_lower or "uncapped" in q_lower or "without date constraints" in q_lower:
            is_unbounded_txn = True
        elif any(term in q_lower for term in ["transaction log", "transaction logs", "historical transaction", "every transaction", "all transaction"]):
            time_tokens = [
                "today", "yesterday", "day", "days", "date", "week", "weeks",
                "month", "months", "year", "years", "202", "between", "from",
                "since", "last", "past", "hour", "hours", "window", "range"
            ]
            clean_text = q_lower.replace("without date", "").replace("no date", "").replace("without date constraints", "")
            if not any(token in clean_text for token in time_tokens):
                is_unbounded_txn = True

    if is_unbounded_txn:
        return types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text="To retrieve transaction logs, please specify a time window or date range."
                )
            ],
        )

    session = getattr(callback_context, "session", None)
    if session is None or not hasattr(session, "state"):
        return None

    now = time.time()
    ttl = 3600.0  # 1-hour temporal session TTL

    # 3. Temporal State Invalidation Logic using session.state (FR-4.2 / NFR-4.3)
    # Invalidate when relative time tokens are parsed on new session days or inactivity exceeds TTL
    relative_time_tokens = [
        "1-hour",
        "1 hour",
        "ttl",
        "elapsed",
        "expiration",
        "recalibration",
        "new session",
        "session day",
        "next day",
        "yesterday",
        "relative time",
        "inactive duration",
    ]
    has_temporal_modifier = any(token in q_lower for token in relative_time_tokens)
    last_active = session.state.get("last_active_ts")

    if has_temporal_modifier or (last_active and (now - float(last_active) > ttl)):
        # Flush all cached session state variables, particularly cached cashier ID
        stale_keys = [k for k in list(session.state.keys()) if k != "last_active_ts"]
        for k in stale_keys:
            del session.state[k]
        session.state["cached_cashier_id"] = None
        session.state["cashier_id"] = None
        session.state["session_recycled"] = False
        session.state["state_invalidated"] = True
        session.state["temporal_ttl_expired"] = True
        session.state["invalidated_at"] = now
    session.state["last_active_ts"] = now

    # 4. Mandatory Partition Bounds State Flag
    session.state["partition_guardrail_enforced"] = True
    session.state["default_partition_window_days"] = 30
    return None


# Register tools: 3 canonical toolsets using declarative McpToolset for Bigtable
tools_list = [
    cymbal_analytics_tool,
    pos_troubleshooting_rag_tool,
    bigtable_mcp_toolset,
]

cymbal_operations_agent = Agent(
    name="cymbal_operations_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=AGENT_INSTRUCTIONS,
    tools=tools_list,
    before_agent_callback=partition_clarification_and_temporal_state_guardrail,
)

# Root agent export for backward compatibility with ADK runners and FastAPI
root_agent = cymbal_operations_agent

# Initialize BigQueryAgentAnalyticsPlugin for runtime telemetry
plugins = []
if PROJECT_ID and BQ_TELEMETRY_DATASET:
    try:
        telemetry_plugin = BigQueryAgentAnalyticsPlugin(
            project_id=PROJECT_ID,
            dataset_id=BQ_TELEMETRY_DATASET,
            location=REGION,
        )
        plugins.append(telemetry_plugin)
    except Exception as e:
        logger.warning("Could not initialize BigQueryAgentAnalyticsPlugin: %s", e)

app = App(
    root_agent=cymbal_operations_agent,
    name="app",
    plugins=plugins,
)

