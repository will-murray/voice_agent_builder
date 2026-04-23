"""
Builds VAPI assistant configuration for a clinic — voice agent v1.

Reads:
- Clinic metadata (name, address, hours) — from Users.clinics
- Approved FAQ rows — from ClinicData.faq WHERE voice_assistant = TRUE
- Approved script sections — from Users.agent_script_sections (latest approved
  row per section; all four required: scope_of_practice, not_offered,
  callers_needs, protocols)

Tool set depends on clinic.pms_type:
- "blueprint" → [match_patient_by_name, submit_ticket]
- other       → [submit_ticket] only (no PHI source available for matching)

See voice_agent_builder/CLAUDE.md "Agent Specification" for the full contract.

Usage:
    config = build_agent_config(clinic, faqs, script_sections)
    assistant = client.assistants.create(**config)
"""
import datetime
import json
import os

from secrets import get_secret
from tools.blueprint import (
    make_match_patient_tool,
    make_submit_ticket_tool,
)

_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")
_VAPI_SECRET = get_secret("vapi-webhook-secret")

SECTION_ORDER = [
    ("scope_of_practice", "Scope of Practice"),
    ("not_offered", "Not Offered"),
    ("callers_needs", "Caller's Needs"),
    ("protocols", "Protocols"),
]


def build_first_message(clinic_name: str) -> str:
    return f"You've reached {clinic_name}, how can I assist you today?"


def _format_script_sections(script_sections: dict[str, str]) -> str:
    """
    Concatenate the four script sections in canonical order with headings.
    Raises KeyError if any required section is missing from the dict.
    """
    parts = []
    for key, heading in SECTION_ORDER:
        if key not in script_sections:
            raise KeyError(f"Missing required script section: {key}")
        parts.append(f"## {heading}\n{script_sections[key].strip()}")
    return "\n\n".join(parts)


def build_system_prompt(
    clinic: dict,
    faqs: list,
    script_sections: dict[str, str],
    pms_type: str,
) -> str:
    has_patient_match = pms_type == "blueprint"

    script_block = _format_script_sections(script_sections)

    if has_patient_match:
        patient_flow = """
## Patient Identification Flow
1. Early in the call, ask whether the caller has been to the clinic before.
2. If yes:
   a. Collect their first and last name.
   b. Ask for the last 4 digits of the phone number on file.
   c. Call `match_patient_by_name` with those three fields.
   d. If the result is `matched`, note the returned `patient_id` for the ticket.
   e. If the result is `ambiguous`, ask for the caller's date of birth and retry with the `dob` field.
   f. If the result is `unmatched` after your best effort, treat the caller as new and ask for a callback phone number.
3. If no: treat as a new patient — collect full name and callback phone number directly.

You never learn the patient's full record — only a yes/no/ambiguous status and an opaque patient_id. Never pretend you know details about a patient beyond what the caller has told you directly.
"""
    else:
        patient_flow = """
## Patient Identification Flow
This clinic does not have electronic patient lookup enabled. Collect the caller's full name and callback phone number directly, and record whether they state they are a new or existing patient in the ticket.
"""

    faqs_block = (
        "## Frequently Asked Questions (reference)\n"
        "These answers are curated by the clinic for common caller questions. "
        "They are reference material only — if any FAQ conflicts with the Script "
        "above, the Script is authoritative.\n\n"
        f"{json.dumps(faqs or [], indent=2)}"
    )

    return f"""The date today is {datetime.datetime.now().strftime("%Y-%m-%d")}.

You are the friendly, professional receptionist at {clinic["clinic_name"]}. Your job is to identify the caller's need, triage against the clinic's scope of practice, collect the information needed for follow-up, and create a ticket that clinic staff will use to call the patient back. Be empathetic when callers express frustration or distress about hearing difficulties — you are often the first voice they reach when they are already stressed.

## About the Clinic
- Name: {clinic["clinic_name"]}
- Address: {clinic["address"]}

## Hours of Operation
- Monday: {clinic.get("hours_monday", "Unknown")}
- Tuesday: {clinic.get("hours_tuesday", "Unknown")}
- Wednesday: {clinic.get("hours_wednesday", "Unknown")}
- Thursday: {clinic.get("hours_thursday", "Unknown")}
- Friday: {clinic.get("hours_friday", "Unknown")}
- Saturday: {clinic.get("hours_saturday", "Unknown")}
- Sunday: {clinic.get("hours_sunday", "Unknown")}

# Script (Authoritative)
The following is the clinic's approved script. Treat it as the source of truth for what this clinic does, does not do, which caller needs you handle, and the step-by-step protocols you follow. If any FAQ below conflicts with this script, the script wins.

{script_block}

{patient_flow}

## Ending the Call
Before hanging up, call `submit_ticket` exactly once with a complete summary of the call. The ticket is how clinic staff know to follow up — if you do not submit it, the call is effectively lost.

{faqs_block}

## Behaviour Guidelines
- Be warm, concise, and professional.
- Do not invent information about services, pricing, availability, or patient records. If you do not know, say so and offer to collect the caller's details for a callback.
- Do not disclose internal identifiers (clinic_id, patient_id, etc.) to the caller.
- You are an AI receptionist. If the caller asks, you may confirm you are an AI. Do not claim to be human.
- Match the caller's pace. Older callers may need you to slow down, repeat, or speak clearly — do so without being asked.
"""


def _build_tools(pms_type: str, clinic_id: str) -> list[dict]:
    """
    Select tools based on clinic.pms_type.

    - "blueprint": server-side patient match + ticket submission.
    - other: ticket submission only (no PHI source available for matching).
    """
    if pms_type == "blueprint":
        return [
            make_match_patient_tool(clinic_id),
            make_submit_ticket_tool(clinic_id),
        ]
    return [make_submit_ticket_tool(clinic_id)]


def build_agent_config(
    clinic: dict,
    faqs: list,
    script_sections: dict[str, str],
) -> dict:
    """
    Returns a complete VAPI assistant creation payload for the given clinic.

    Args:
        clinic:          Clinic row from Users.clinics (must include clinic_name,
                         clinic_id, address, hours_*, pms_type).
        faqs:            List of {question, answer} from ClinicData.faq
                         (pre-filtered to voice_assistant = TRUE).
        script_sections: {section_name: content} — latest approved rows for all
                         four sections. Missing any raises KeyError.

    Returns:
        Dict suitable for passing as kwargs to client.assistants.create().
    """
    pms_type = clinic.get("pms_type", "none")
    clinic_id = clinic["clinic_id"]

    system_prompt = build_system_prompt(clinic, faqs, script_sections, pms_type)
    tools = _build_tools(pms_type, clinic_id)

    model_config = {
        "provider": "openai",
        "model": "gpt-4o",
        "messages": [{"role": "system", "content": system_prompt}],
        "tools": tools,
    }

    config = {
        "name": clinic["clinic_name"],
        "first_message": build_first_message(clinic["clinic_name"]),
        "first_message_interruptions_enabled": True,
        "model": model_config,
        # VAPI requires voice + transcriber to be set explicitly before an
        # assistant can be published. Defaults below; override per-clinic later
        # if we want different voices.
        "voice": {"provider": "11labs", "voiceId": "burt"},
        "transcriber": {"provider": "deepgram", "model": "nova-2", "language": "en-US"},
    }

    # VAPI attaches X-Vapi-Secret to every tool call when a server config is
    # present on the assistant. cortex-hypervisor verifies the header.
    if _VAPI_SECRET:
        config["server"] = {
            "url": _CORTEX_BASE,
            "secret": _VAPI_SECRET,
        }

    return config
