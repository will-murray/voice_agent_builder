"""
VAPI tool definitions for voice agent v1.

Two tools, both proxied through cortex-hypervisor:
- `match_patient_by_name` — server-side patient match against
  Blueprint_PHI.ClientDemographics, filtered by clinic_id
- `submit_ticket` — writes one row to Users.voice_agent_tickets before hangup

The clinic_id is baked into each tool URL at assistant creation time — VAPI
never passes it as a parameter, so the agent cannot spoof it.

Auth from VAPI to cortex-hypervisor uses the X-Vapi-Secret header, configured
at the assistant's server config (not per-tool).

Environment variables:
    CORTEX_API_BASE_URL   Base URL of cortex-hypervisor (defaults to localhost)
"""
import os

_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")


def make_match_patient_tool(clinic_id: str) -> dict:
    """
    VAPI tool: match an existing patient by name + last 4 of phone.

    Called only after the caller confirms they have been to the clinic before.
    On an `ambiguous` result, the agent retries with a DOB tie-breaker.
    Returns a status + opaque patient_id — no PHI leaks back to the agent.

    Proxied via: POST /blueprint/{clinic_id}/patient/match
    """
    return {
        "type": "apiRequest",
        "name": "match_patient_by_name",
        "description": (
            "Look up an existing patient in the clinic's records by first name, "
            "last name, and the last 4 digits of the phone number on file. "
            "Only call this after the caller confirms they are an existing patient. "
            "Returns 'matched' (patient identified uniquely), 'ambiguous' (multiple "
            "candidates — retry with the caller's date of birth), or 'unmatched' "
            "(treat the caller as new). The tool never reveals a patient's name, "
            "phone number, or DOB — only a status and an opaque patient identifier."
        ),
        "url": f"{_CORTEX_BASE}/blueprint/{clinic_id}/patient/match",
        "method": "POST",
        "body": {
            "type": "object",
            "properties": {
                "first_name": {
                    "type": "string",
                    "description": "Caller's first name as they gave it.",
                },
                "last_name": {
                    "type": "string",
                    "description": "Caller's last name as they gave it.",
                },
                "last4_phone": {
                    "type": "string",
                    "description": (
                        "Last 4 digits of the phone number the caller has on file "
                        "with the clinic. Exactly 4 digits."
                    ),
                },
                "dob": {
                    "type": "string",
                    "description": (
                        "Optional date of birth in YYYY-MM-DD format. Provide this "
                        "on a retry when the initial match returned 'ambiguous'."
                    ),
                },
            },
            "required": ["first_name", "last_name", "last4_phone"],
        },
    }


def make_submit_ticket_tool(clinic_id: str) -> dict:
    """
    VAPI tool: submit a ticket summarizing the call, before hanging up.

    Called exactly once per call, at the end. The ticket goes to
    Users.voice_agent_tickets where clinic staff read it to follow up.

    Proxied via: POST /clinics/{clinic_id}/voice_agent/tickets
    """
    return {
        "type": "apiRequest",
        "name": "submit_ticket",
        "description": (
            "Submit a ticket summarizing this call. Call this exactly once, right "
            "before you end the conversation. The ticket is what clinic staff will "
            "see to follow up. If you do not call this, the call is lost from "
            "the clinic's point of view."
        ),
        "url": f"{_CORTEX_BASE}/clinics/{clinic_id}/voice_agent/tickets",
        "method": "POST",
        "body": {
            "type": "object",
            "properties": {
                "vapi_call_id": {
                    "type": "string",
                    "description": "The current VAPI call ID, if available.",
                },
                "caller_phone": {
                    "type": "string",
                    "description": "Caller's phone in E.164 format (e.g. +16045551234).",
                },
                "caller_name": {
                    "type": "string",
                    "description": "The caller's name as they gave it during the call.",
                },
                "patient_match_status": {
                    "type": "string",
                    "enum": ["matched", "unmatched", "new", "ambiguous"],
                    "description": (
                        "'matched' = match_patient_by_name returned matched. "
                        "'ambiguous' = still ambiguous after the DOB retry. "
                        "'unmatched' = match_patient_by_name returned unmatched. "
                        "'new' = the caller self-identified as a new patient."
                    ),
                },
                "blueprint_patient_id": {
                    "type": "string",
                    "description": (
                        "The patient_id returned by match_patient_by_name. "
                        "Omit when patient_match_status is not 'matched'."
                    ),
                },
                "last4_confirmed": {
                    "type": "boolean",
                    "description": (
                        "True if the caller confirmed the last 4 digits of the "
                        "phone on file during the match flow."
                    ),
                },
                "intent_category": {
                    "type": "string",
                    "description": (
                        "Which of the clinic's Caller's Needs categories best "
                        "fits this call. Use the label from the script's "
                        "Caller's Needs section."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "1-2 sentence recap of the call for clinic staff to read."
                    ),
                },
                "details": {
                    "type": "object",
                    "description": (
                        "Intent-specific fields collected during the call. "
                        "Free-form JSON — include whatever is relevant to the "
                        "protocol you followed."
                    ),
                },
                "suggested_followup": {
                    "type": "string",
                    "description": (
                        "What clinic staff should do next, based on the call. "
                        "e.g. 'Call back to book hearing test', 'Send wax-removal "
                        "referral', 'No action required'."
                    ),
                },
                "urgency": {
                    "type": "string",
                    "enum": ["normal", "urgent"],
                    "description": (
                        "'urgent' if the caller mentioned a time-sensitive issue "
                        "(severe distress, sudden hearing loss, etc.). Otherwise 'normal'."
                    ),
                },
            },
            "required": ["patient_match_status"],
        },
    }


def make_voice_agent_v1_tools(clinic_id: str) -> list[dict]:
    """Return both voice agent v1 tools for a clinic."""
    return [
        make_match_patient_tool(clinic_id),
        make_submit_ticket_tool(clinic_id),
    ]
