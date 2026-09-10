# Copyright 2026 Google LLC
"""NL2SQL Data Agent Tool for Cymbal Operations Agent."""

import logging
import os
import time
from typing import Any

import google.auth
from google.adk.tools.data_agent.config import DataAgentToolConfig
from google.adk.tools.data_agent.data_agent_tool import ask_data_agent

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("PROJECT_ID", "")
DATA_AGENT_ID = os.getenv("DATA_AGENT_ID", "")
DATA_AGENT_NAME = os.getenv("DATA_AGENT_NAME")
if not DATA_AGENT_NAME and PROJECT_ID and DATA_AGENT_ID:
    DATA_AGENT_NAME = f"projects/{PROJECT_ID}/locations/global/dataAgents/{DATA_AGENT_ID}"
elif not DATA_AGENT_NAME:
    DATA_AGENT_NAME = ""


def cymbal_analytics_tool(query: str) -> str:
    """Answers natural language analytical questions across Cymbal enterprise data.

    Use this tool for queries regarding store analytics, sales revenue, transactions,
    promotions, store inventory reconciliation, warranties, and cross-cloud audits.
    Standardized enterprise terms (e.g., Net Transaction Revenue, Total On-Hand Inventory,
    Estimated Cover Hours, Cashier Manual Override Rate) are mapped semantically by the Data Agent.

    Args:
        query: The natural language question or request to pass verbatim to the BigQuery Data Agent.

    Returns:
        A markdown-formatted analytical response containing findings, executed SQL query, and retrieved data.
    """
    credentials, _ = google.auth.default()
    settings = DataAgentToolConfig(max_query_result_rows=50)

    max_retries = 3
    delay = 2.0

    for attempt in range(1, max_retries + 1):
        try:
            logger.info("Calling Data Agent (attempt %d/%d) with query: %s", attempt, max_retries, query)
            res = ask_data_agent(
                DATA_AGENT_NAME,
                query,
                credentials=credentials,
                settings=settings,
                tool_context=None,
            )

            if res.get("status") == "SUCCESS":
                resp_items = res.get("response", [])
                text_outputs = []
                sql_query = None
                data_retrieved = None

                for item in resp_items:
                    if "text" in item:
                        t_info = item["text"]
                        if t_info.get("textType") == "FINAL_RESPONSE":
                            parts = t_info.get("parts", [])
                            text_outputs.extend(parts)
                    if "data" in item and "generatedSql" in item["data"]:
                        sql_query = item["data"]["generatedSql"]
                    if "Data Retrieved" in item:
                        data_retrieved = item["Data Retrieved"]

                output_sections = []
                if text_outputs:
                    output_sections.append("\n\n".join(text_outputs))
                if sql_query:
                    output_sections.append(f"**Generated SQL:**\n```sql\n{sql_query}\n```")
                if data_retrieved:
                    summary = data_retrieved.get("summary", "")
                    headers = data_retrieved.get("headers", [])
                    rows = data_retrieved.get("rows", [])
                    if headers and rows:
                        header_line = "| " + " | ".join(headers) + " |"
                        sep_line = "| " + " | ".join(["---"] * len(headers)) + " |"
                        table_lines = [header_line, sep_line]
                        for row in rows[:20]:
                            table_lines.append("| " + " | ".join(str(c) for c in row) + " |")
                        output_sections.append("\n".join(table_lines))
                    if summary:
                        output_sections.append(f"*{summary}*")

                if output_sections:
                    return "\n\n".join(output_sections)
                return "Query executed successfully with no additional rows returned."

            error_details = res.get("error_details", "Unknown error")
            logger.warning("Data Agent returned error on attempt %d: %s", attempt, error_details)

        except Exception as ex:
            logger.warning("Data Agent request exception on attempt %d: %s", attempt, ex)

        if attempt < max_retries:
            time.sleep(delay)
            delay *= 2

    return "Store data is currently unreachable. Please verify database connectivity."
