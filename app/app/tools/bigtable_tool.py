# Copyright 2026 Google LLC
"""Cloud Bigtable MCP Toolset for Cashier Real-Time Alerts."""

import json
import logging
import os
import time
from typing import Any, Dict

import google.auth
from google.auth import impersonated_credentials
from google.auth.transport.requests import Request
import requests

from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StreamableHTTPConnectionParams

logger = logging.getLogger(__name__)

DEFAULT_BIGTABLE_MCP_URL = os.getenv(
    "BIGTABLE_MCP_SERVICE_URL",
    os.getenv("BIGTABLE_MCP_URL", "https://mcp-toolbox-bigtable-7erio3vcqa-uc.a.run.app"),
)
IMPERSONATE_SA = os.getenv(
    "MCP_INVOKER_SERVICE_ACCOUNT",
    "243199575379-compute@developer.gserviceaccount.com",
)


def _get_id_token(target_audience: str) -> str:
    """Generates an OIDC ID token for authenticating to the Cloud Run microservice."""
    try:
        auth_req = Request()
        # Direct fetch if running as service account or with metadata server
        import google.oauth2.id_token
        try:
            return google.oauth2.id_token.fetch_id_token(auth_req, target_audience)
        except Exception:
            pass

        # Fallback to service account impersonation for user ADC
        source_creds, _ = google.auth.default()
        target_creds = impersonated_credentials.Credentials(
            source_credentials=source_creds,
            target_principal=IMPERSONATE_SA,
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        id_creds = impersonated_credentials.IDTokenCredentials(
            target_credentials=target_creds,
            target_audience=target_audience,
            include_email=True,
        )
        id_creds.refresh(auth_req)
        return id_creds.token
    except Exception as e:
        logger.error("Failed to generate OIDC ID token for %s: %s", target_audience, e)
        raise


def get_cashier_realtime_metrics(row_key_prefix: str) -> str:
    """Queries real-time 1-hour rolling metrics and audit status flags for a cashier in Cloud Bigtable.

    Use this tool to inspect live operational metrics, cashier manual override rates,
    promo usage, total discounts, and anomaly audit status flags.

    Args:
        row_key_prefix: Bigtable row key prefix formatted as STORE_<store_id_3digits>#CASH_<cashier_id_4digits>,
                        e.g. STORE_048#CASH_1190 or STORE_001#CASH_1001.

    Returns:
        A markdown-formatted summary of live 1-hour cashier metrics, risk score, and audit flags.
    """
    max_retries = 3
    delay = 1.5

    for attempt in range(1, max_retries + 1):
        try:
            token = _get_id_token(DEFAULT_BIGTABLE_MCP_URL)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            payload = {
                "jsonrpc": "2.0",
                "id": int(time.time()),
                "method": "tools/call",
                "params": {
                    "name": "get_cashier_realtime_metrics",
                    "arguments": {
                        "row_key_prefix": row_key_prefix,
                    },
                },
            }
            resp = requests.post(
                f"{DEFAULT_BIGTABLE_MCP_URL}/mcp",
                json=payload,
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                result = data.get("result", {})
                content = result.get("content", [])
                if content:
                    records = []
                    for c in content:
                        text_val = c.get("text", "")
                        try:
                            records.append(json.loads(text_val))
                        except Exception:
                            records.append({"raw": text_val})

                    if not records:
                        return f"No active alerts or rolling metrics found for cashier prefix: `{row_key_prefix}`."

                    latest = records[0]
                    audit_status = latest.get("audit_status", "clear")
                    last_event_ts = latest.get("last_event_ts", "N/A")
                    override_count = latest.get("manual_override_count", 0)
                    promo_count = latest.get("promo_count", 0)
                    promo_rate = latest.get("promo_rate", 0.0)
                    avg_discount_pct = latest.get("avg_discount_pct", 0.0)
                    total_discount_usd = latest.get("total_discount_usd", 0.0)
                    txn_count = latest.get("txn_count", 0)
                    risk_score = latest.get("risk_score", 0.0)

                    # Compute live override rate if txn_count > 0
                    override_rate = (override_count / txn_count) if txn_count > 0 else 0.0

                    md = [
                        f"### Real-Time Cashier Metrics for `{row_key_prefix}`",
                        f"- **Audit Status Flag:** `{audit_status.upper()}`",
                        f"- **Last Event Timestamp:** {last_event_ts}",
                        f"- **Live 1-Hour Manual Override Rate:** {override_rate:.2%} ({override_count} overrides / {txn_count} txns)",
                        f"- **Live 1-Hour Promo Rate:** {promo_rate:.2%} ({promo_count} promos)",
                        f"- **Live 1-Hour Average Discount:** {avg_discount_pct:.2%}",
                        f"- **Live 1-Hour Total Discount USD:** ${total_discount_usd:,.2f}",
                        f"- **Rolling Risk Score:** {risk_score:.6f}",
                        f"- **Total Recent Alert Records:** {len(records)}",
                    ]
                    return "\n".join(md)

                return f"No real-time records found in Bigtable for `{row_key_prefix}`."

            logger.warning("Bigtable MCP call returned status %d on attempt %d", resp.status_code, attempt)
        except Exception as ex:
            logger.warning("Bigtable MCP call failed on attempt %d: %s", attempt, ex)

        if attempt < max_retries:
            time.sleep(delay)
            delay *= 2

    return f"Unable to reach Bigtable MCP service for row key `{row_key_prefix}`. Please verify microservice health."


def create_bigtable_mcp_toolset() -> McpToolset:
    """Factory to create ADK McpToolset instance connected to Cloud Run microservice."""
    token = _get_id_token(DEFAULT_BIGTABLE_MCP_URL)
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=f"{DEFAULT_BIGTABLE_MCP_URL}/mcp",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
    )


# Export toolset instance
try:
    bigtable_mcp_toolset = create_bigtable_mcp_toolset()
except Exception as e:
    logger.warning("Could not eagerly initialize bigtable_mcp_toolset: %s", e)
    bigtable_mcp_toolset = None
