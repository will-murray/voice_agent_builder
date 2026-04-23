"""
Google Cloud Secret Manager client for voice_agent_builder.

Requires GCP_PROJECT env var + Application Default Credentials.
"""
from functools import lru_cache
import os

from google.cloud import secretmanager

_client = secretmanager.SecretManagerServiceClient()
_project = os.environ["GCP_PROJECT"]


@lru_cache(maxsize=None)
def get_secret(name: str, version: str = "latest") -> str:
    """Fetch a secret from Google Cloud Secret Manager (cached)."""
    path = f"projects/{_project}/secrets/{name}/versions/{version}"
    return _client.access_secret_version(
        request={"name": path}
    ).payload.data.decode("utf-8")
