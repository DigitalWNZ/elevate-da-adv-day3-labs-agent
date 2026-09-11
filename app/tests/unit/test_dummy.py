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
"""
Unit test suite for Cymbal Retail Operations Agent and tools.
Covers core business logic, edge cases, error code regex parsing,
Bigtable metric calculations, RAG decline specifications, and coordinator tool bindings.
"""

import json
import re
import time
from unittest.mock import MagicMock, patch

import pytest
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from app.agent import (
    cymbal_operations_agent,
    partition_clarification_and_temporal_state_guardrail,
    tools_list,
)
from app.tools.analytics_tool import cymbal_analytics_tool
from app.tools.bigtable_tool import (
    bigtable_mcp_toolset,
    get_cashier_realtime_metrics,
    read_cashier_realtime_alerts_sql,
    read_pos_transactions_enriched_sql,
)
from app.tools.rag_tool import (
    CERTIFIED_WARNING,
    SIMILARITY_THRESHOLD,
    _format_gcs_link,
    pos_troubleshooting_rag_tool,
)


def test_rag_certified_warning_specification() -> None:
    """Verifies that the RAG decline warning strictly conforms to SDD Section 3.3 / Table 5.2."""
    expected_warning = (
        "I cannot find certified warranty or repair rules for this specific error "
        "in our technical repository."
    )
    assert CERTIFIED_WARNING == expected_warning
    assert SIMILARITY_THRESHOLD == 0.70


def test_rag_error_code_regex_parsing() -> None:
    """Tests regex extraction for hardware and peripheral error codes."""
    pattern = r"\b(ERR-[A-Z0-9_-]+)\b"

    q1 = "How do we resolve ERR-PAY-4001 when contactless freezes?"
    m1 = re.search(pattern, q1)
    assert m1 is not None
    assert m1.group(1) == "ERR-PAY-4001"

    q2 = "Printer error ERR-DN-PRNT-24V knife cutter lockup"
    m2 = re.search(pattern, q2)
    assert m2 is not None
    assert m2.group(1) == "ERR-DN-PRNT-24V"

    q3 = "How to replace engine oil on a Ford F-150 truck?"
    m3 = re.search(pattern, q3)
    assert m3 is None  # Narrowed regex strictly avoids matching non-error tokens like F-150

    q4 = "General checkout inquiry with no code"
    m4 = re.search(pattern, q4)
    assert m4 is None


def test_rag_gcs_to_https_formatting() -> None:
    """Tests conversion of gs:// cloud storage URIs to clickable HTTPS links."""
    gs_uri = "gs://cymbal-ops-runbooks/pos_manual_toshiba_tcx810.pdf"
    https_url = _format_gcs_link(gs_uri)
    assert https_url == "https://storage.cloud.google.com/cymbal-ops-runbooks/pos_manual_toshiba_tcx810.pdf"

    # Already https
    assert _format_gcs_link("https://example.com/doc.pdf") == "https://example.com/doc.pdf"
    # Empty
    assert _format_gcs_link("") == ""


def test_rag_out_of_scope_query_mock() -> None:
    """Tests that low-scoring / out-of-scope queries return the certified warning."""
    with patch("google.cloud.bigquery.Client") as mock_bq:
        mock_client = MagicMock()
        mock_bq.return_value = mock_client

        # Mock vector search returning a low similarity score (< 0.70)
        mock_row = MagicMock()
        mock_row.similarity_score = 0.45
        mock_job = MagicMock()
        mock_job.result.return_value = [mock_row]
        mock_client.query.return_value = mock_job

        res = pos_troubleshooting_rag_tool("How do I replace the engine oil on a Ford F-150 truck?")
        assert res == CERTIFIED_WARNING


def test_bigtable_cashier_realtime_alerts_metrics_and_markdown() -> None:
    """Tests live cashier metrics parsing, manual override rate calculation, and markdown generation."""
    sample_response_data = {
        "result": {
            "content": [
                {
                    "text": json.dumps({
                        "row_key": "STORE_048#CASH_1190#20260312",
                        "audit_status": "under_review",
                        "last_event_ts": "2026-03-12T14:32:00Z",
                        "manual_override_count": 6,
                        "txn_count": 26,
                        "promo_count": 5,
                        "promo_rate": 0.1923,
                        "avg_discount_pct": 0.154,
                        "total_discount_usd": 285.50,
                        "risk_score": 0.8872,
                    })
                }
            ]
        }
    }

    with patch("app.tools.bigtable_tool._get_id_token", return_value="mock_token"), \
         patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_response_data
        mock_post.return_value = mock_resp

        # Test both canonical and alias functions
        res1 = read_cashier_realtime_alerts_sql("STORE_048#CASH_1190")
        assert "Real-Time Cashier Metrics for `STORE_048#CASH_1190`" in res1
        assert "`UNDER_REVIEW`" in res1
        assert "23.08%" in res1  # 6 / 26 = 23.08%
        assert "$285.50" in res1
        assert "0.8872" in res1

        res2 = get_cashier_realtime_metrics("STORE_048#CASH_1190")
        assert res2 == res1


def test_bigtable_pos_transactions_enriched_markdown() -> None:
    """Tests enriched POS transaction querying and markdown table generation."""
    sample_response_data = {
        "result": {
            "content": [
                {
                    "text": json.dumps({
                        "transaction_id": "TXN-20260910-0003385",
                        "store_id": "STORE_001",
                        "pos_terminal_id": "TERM_01",
                        "cashier_id": "CASH_1001",
                        "total": 45.99,
                        "payment_method": "credit_card",
                        "promo_code_applied": "CYMBAL10",
                        "event_timestamp": "2026-09-10T15:00:00Z",
                    })
                }
            ]
        }
    }

    with patch("app.tools.bigtable_tool._get_id_token", return_value="mock_token"), \
         patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_response_data
        mock_post.return_value = mock_resp

        res = read_pos_transactions_enriched_sql("STORE_001#TXN-")
        assert "Enriched POS Transactions for `STORE_001#TXN-`" in res
        assert "`TXN-20260910-0003385`" in res
        assert "STORE_001" in res
        assert "$45.99" in res
        assert "`CYMBAL10`" in res


def test_bigtable_empty_records_fallback() -> None:
    """Tests graceful handling when Bigtable contains no records for prefix."""
    with patch("app.tools.bigtable_tool._get_id_token", return_value="mock_token"), \
         patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"result": {"content": []}}
        mock_post.return_value = mock_resp

        res1 = read_cashier_realtime_alerts_sql("STORE_999#CASH_9999")
        assert "No real-time records found in Bigtable for `STORE_999#CASH_9999`" in res1

        res2 = read_pos_transactions_enriched_sql("STORE_999#TXN-")
        assert "No enriched transaction records found in Bigtable for `STORE_999#TXN-`" in res2


def test_analytics_tool_fallback_on_exception() -> None:
    """Tests that analytics tool returns the required fallback string when unreachable."""
    with patch("app.tools.analytics_tool.ask_data_agent", side_effect=Exception("Database connection timeout")):
        res = cymbal_analytics_tool("What is the Net Transaction Revenue for Store 48?")
        assert res == "Store data is currently unreachable. Please verify database connectivity."


def test_coordinator_agent_structure_and_bindings() -> None:
    """Verifies that the root coordinator agent has proper tools, model, and instructions."""
    assert cymbal_operations_agent.name == "cymbal_operations_agent"
    assert len(tools_list) == 3

    assert tools_list[0] is cymbal_analytics_tool
    assert tools_list[1] is pos_troubleshooting_rag_tool
    assert isinstance(tools_list[2], McpToolset) or tools_list[2] is bigtable_mcp_toolset

    instructions = cymbal_operations_agent.instruction
    assert "pos_troubleshooting_rag_tool" in instructions
    assert "cymbal_analytics_tool" in instructions
    assert "bigtable_mcp_toolset" in instructions
    assert "read_cashier_realtime_alerts_sql" in instructions
    assert "read_pos_transactions_enriched_sql" in instructions
    assert "Parallel Tool Dispatch" in instructions
    assert "Sequential Multi-Turn Dispatch" in instructions
    assert "Mandatory Partition Clarification Guardrail" in instructions
    assert cymbal_operations_agent.before_agent_callback is partition_clarification_and_temporal_state_guardrail


def test_partition_clarification_and_temporal_guardrail() -> None:
    """Verifies that partition_clarification_and_temporal_state_guardrail enforces guardrails and temporal invalidation."""
    # Test 1: Stale session (> 3600 seconds)
    stale_time = time.time() - 4000
    mock_session = MagicMock()
    mock_session.state = {
        "last_active_ts": stale_time,
        "old_cached_metric": 42,
    }
    mock_context = MagicMock()
    mock_context.session = mock_session

    partition_clarification_and_temporal_state_guardrail(mock_context)

    assert "old_cached_metric" not in mock_session.state
    assert mock_session.state["state_invalidated"] is True
    assert mock_session.state["partition_guardrail_enforced"] is True
    assert mock_session.state["default_partition_window_days"] == 30
    assert mock_session.state["last_active_ts"] > stale_time

    # Test 2: Active session (< 3600 seconds)
    recent_time = time.time() - 100
    mock_session.state = {
        "last_active_ts": recent_time,
        "active_data": "persist",
    }
    partition_clarification_and_temporal_state_guardrail(mock_context)
    assert mock_session.state["active_data"] == "persist"
    assert mock_session.state["partition_guardrail_enforced"] is True
