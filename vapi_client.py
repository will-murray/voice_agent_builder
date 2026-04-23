"""
Shared VAPI client for all voice_agent_builder modules.

Import `client` for SDK calls and `VAPI_BASE`/`VAPI_HEADERS` for raw HTTP calls.
Do not instantiate Vapi() or define VAPI_TOKEN elsewhere in this package.
"""
from secrets import get_secret
from vapi import Vapi

VAPI_TOKEN: str = get_secret("vapi-api-key")
VAPI_BASE = "https://api.vapi.ai"
VAPI_HEADERS = {
    "Authorization": f"Bearer {VAPI_TOKEN}",
    "Content-Type": "application/json",
}

client = Vapi(token=VAPI_TOKEN)
