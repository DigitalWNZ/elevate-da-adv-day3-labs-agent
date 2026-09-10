# Copyright 2026 Google LLC
"""Hardware Technical Diagnostics RAG Tool using BigQuery VECTOR_SEARCH with Context Stitching."""

import logging
import os
import re
import time
from typing import Any

from google.cloud import bigquery

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("PROJECT_ID", "")
CHUNK_TABLE = os.getenv(
    "POS_CHUNK_EMBEDDINGS_TABLE",
    f"{PROJECT_ID}.cymbal_gold.pos_manual_chunk_embeddings" if PROJECT_ID else "cymbal_gold.pos_manual_chunk_embeddings",
)
SIMILARITY_THRESHOLD = 0.70
CERTIFIED_WARNING = (
    "I cannot find certified warranty or repair rules for this specific error in our technical repository. Please contact Support."
)


def _format_gcs_link(uri: str) -> str:
    """Converts a gs:// URI to a clickable HTTPS console/cloud storage link."""
    if uri and uri.startswith("gs://"):
        return uri.replace("gs://", "https://storage.cloud.google.com/")
    return uri or ""


def pos_troubleshooting_rag_tool(query: str) -> str:
    """Searches POS terminal technical manuals and diagnostics runbooks in BigQuery.

    Performs dense vector similarity search and keyword search over sliding-window
    chunks of hardware documentation for supported POS terminals (Toshiba TCx 810,
    HP Engage One Pro, Diebold Nixdorf BEETLE A1150, Clover Station Solo, NCR Voyix RealPOS XR7),
    stitching adjacent context windows for complete procedural runbooks.

    Args:
        query: The hardware diagnostics, troubleshooting error code, or terminal recovery question.

    Returns:
        A markdown-formatted technical diagnostics runbook with step-by-step recovery SOPs,
        equipment details, similarity score, and certified documentation link.
    """
    client = bigquery.Client(project=PROJECT_ID) if PROJECT_ID else bigquery.Client()
    max_retries = 3
    delay = 1.5

    # Inline error code regex parsing
    error_match = re.search(r"\b[A-Za-z0-9]+-[A-Za-z0-9-]+\b", query)
    error_code = error_match.group(0) if error_match else ""

    # 1. Vector Search Query with Adjacent Context Stitching and CASE WHEN Boosting
    vector_sql = f"""
    WITH query_emb AS (
      SELECT AI.EMBED(@query_text, endpoint => "text-embedding-005").result AS q_emb
    ),
    matches AS (
      SELECT
        base.document_filename,
        base.document_title,
        base.equipment_covered,
        base.source_pdf_uri,
        base.chunk_index,
        base.chunk_content,
        CASE
          WHEN @error_code != '' AND (
            REGEXP_CONTAINS(base.chunk_content, CONCAT(r'(?i)', @error_code))
            OR REGEXP_CONTAINS(base.document_title, CONCAT(r'(?i)', @error_code))
          ) THEN LEAST(1.0, ROUND(1 - distance, 4) + 0.30)
          ELSE ROUND(1 - distance, 4)
        END AS similarity_score
      FROM VECTOR_SEARCH(
        TABLE `{CHUNK_TABLE}`,
        "embedding",
        TABLE query_emb,
        top_k => 10,
        distance_type => "COSINE"
      )
    )
    SELECT
      m.document_filename,
      m.document_title,
      m.equipment_covered,
      m.source_pdf_uri,
      m.similarity_score,
      m.chunk_index,
      STRING_AGG(c.chunk_content, @delim ORDER BY c.chunk_index ASC) AS stitched_procedure
    FROM matches m
    JOIN `{CHUNK_TABLE}` c
      ON m.document_filename = c.document_filename
     AND c.chunk_index BETWEEN (m.chunk_index - 1) AND (m.chunk_index + 1)
    GROUP BY
      m.document_filename,
      m.document_title,
      m.equipment_covered,
      m.source_pdf_uri,
      m.similarity_score,
      m.chunk_index
    ORDER BY m.similarity_score DESC
    LIMIT 1;
    """

    best_match = None

    for attempt in range(1, max_retries + 1):
        try:
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("query_text", "STRING", query),
                    bigquery.ScalarQueryParameter("error_code", "STRING", error_code),
                    bigquery.ScalarQueryParameter("delim", "STRING", "\n\n"),
                ],
                labels={"datacloud": "jetski"},
            )
            query_job = client.query(vector_sql, job_config=job_config)
            rows = list(query_job.result())
            if rows:
                row = rows[0]
                if row.similarity_score >= SIMILARITY_THRESHOLD:
                    best_match = row
                    break
                else:
                    logger.info(
                        "Vector search similarity %.4f below threshold %.2f; attempting fallback",
                        row.similarity_score,
                        SIMILARITY_THRESHOLD,
                    )
            break
        except Exception as ex:
            logger.warning("Vector search query attempt %d failed: %s", attempt, ex)
            if attempt < max_retries:
                time.sleep(delay)
                delay *= 2

    # 2. If vector search failed or fell below threshold, try full-text SEARCH fallback
    if not best_match:
        # Extract alphanumeric error tokens (e.g. ERR-PAY-4001, ERR_*, or salient keywords)
        error_tokens = re.findall(r"[A-Za-z0-9]+-[A-Za-z0-9-]+", query)
        search_terms = []
        if error_tokens:
            search_terms = [f"`{tok}`" for tok in error_tokens]
        else:
            # Clean words for SEARCH
            words = [re.sub(r"[^A-Za-z0-9]", "", w) for w in query.split()]
            words = [w for w in words if len(w) > 3]
            if words:
                search_terms = [f"`{w}`" for w in words[:3]]

        for term in search_terms:
            search_sql = f"""
            WITH matches AS (
              SELECT
                document_filename,
                document_title,
                equipment_covered,
                source_pdf_uri,
                chunk_index,
                chunk_content,
                0.90 AS similarity_score
              FROM `{CHUNK_TABLE}`
              WHERE SEARCH(chunk_content, @search_term)
              LIMIT 1
            )
            SELECT
              m.document_filename,
              m.document_title,
              m.equipment_covered,
              m.source_pdf_uri,
              m.similarity_score,
              m.chunk_index,
              STRING_AGG(c.chunk_content, @delim ORDER BY c.chunk_index ASC) AS stitched_procedure
            FROM matches m
            JOIN `{CHUNK_TABLE}` c
              ON m.document_filename = c.document_filename
             AND c.chunk_index BETWEEN (m.chunk_index - 1) AND (m.chunk_index + 1)
            GROUP BY
              m.document_filename,
              m.document_title,
              m.equipment_covered,
              m.source_pdf_uri,
              m.similarity_score,
              m.chunk_index;
            """
            try:
                job_config = bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("search_term", "STRING", term),
                        bigquery.ScalarQueryParameter("delim", "STRING", "\n\n"),
                    ],
                    labels={"datacloud": "jetski"},
                )
                search_rows = list(client.query(search_sql, job_config=job_config).result())
                if search_rows:
                    best_match = search_rows[0]
                    break
            except Exception as ex:
                logger.warning("Full-text SEARCH fallback failed for term %s: %s", term, ex)

    if not best_match or best_match.similarity_score < SIMILARITY_THRESHOLD:
        return CERTIFIED_WARNING

    https_link = _format_gcs_link(best_match.source_pdf_uri)
    return (
        f"### {best_match.document_title}\n"
        f"**Equipment Covered:** {best_match.equipment_covered}\n"
        f"**Confidence / Relevance Score:** {best_match.similarity_score:.4f}\n"
        f"**Certified Technical Manual:** [{best_match.document_filename}]({https_link})\n\n"
        f"#### Field Troubleshooting Procedure:\n"
        f"{best_match.stitched_procedure}"
    )
