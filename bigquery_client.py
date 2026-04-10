"""
BigQuery client for pulling receptionist config and tool definitions.
Authenticates using the GCS_SERVICE_ACCOUNT JSON from .env.
"""

import json
import os
from google.cloud import bigquery
from google.oauth2 import service_account
from dotenv import load_dotenv

load_dotenv()

_client: bigquery.Client | None = None


def get_client() -> bigquery.Client:
    """Return a cached BigQuery client authenticated via service account."""
    global _client
    if _client is None:
        sa_json = os.environ["GCS_SERVICE_ACCOUNT"]
        sa_info = json.loads(sa_json)
        credentials = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        _client = bigquery.Client(
            project=os.environ["GCP_PROJECT"],
            credentials=credentials,
        )
    return _client


def run_query(sql: str, params: list[bigquery.ScalarQueryParameter] | None = None) -> list[dict]:
    """Execute a SQL query and return rows as a list of dicts."""
    client = get_client()
    job_config = bigquery.QueryJobConfig(query_parameters=params or [])
    rows = client.query(sql, job_config=job_config).result()
    return [dict(row) for row in rows]


def get_table_schema(table: str, dataset: str | None = None) -> list[dict]:
    """Return schema for a table as a list of {name, field_type, mode} dicts."""
    client = get_client()
    dataset = dataset or os.environ["BQ_DATASET"]
    ref = client.get_table(f"{os.environ['GCP_PROJECT']}.{dataset}.{table}")
    return [
        {"name": f.name, "type": f.field_type, "mode": f.mode}
        for f in ref.schema
    ]




