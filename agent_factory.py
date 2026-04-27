"""
Builds VAPI assistant configuration for a clinic — voice agent v1.

Reads:
- Clinic metadata (name, address, hours) — from Users.clinics
- Approved FAQ rows — from ClinicData.faq WHERE voice_assistant = TRUE
- Approved script sections — from Users.agent_script_sections (latest approved
  row per section; all four required: scope_of_practice, not_offered,
  callers_needs, protocols)
- Enabled capability IDs — from Users.clinic_voice_agent_capabilities

Capabilities (see capabilities.py) bundle a VAPI tool + prompt fragment +
PMS compatibility. SubmitTicket is always-on; PatientMatch and
SearchAvailability are toggleable per-clinic via the dashboard.

See voice_agent_builder/CLAUDE.md "Agent Specification" for the full contract.

Usage:
    config = build_agent_config(clinic, faqs, script_sections, enabled_capability_ids)
    assistant = client.assistants.create(**config)
"""
from __future__ import annotations

import datetime
import json
import logging
import os

from secrets import get_secret
from capabilities import CAPABILITY_REGISTRY, Capability, SubmitTicket

log = logging.getLogger(__name__)

_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")
_VAPI_SECRET = get_secret("vapi-webhook-secret")
# VAPI `apiRequest` tools don't inherit the assistant's server secret — they
# authenticate via a VAPI `credentialId` that references a custom-credential
# configured to send `X-Vapi-Secret: <value>`. This credential is created
# once per VAPI org (see README / setup notes) and its ID is stored in SM.
_VAPI_CREDENTIAL_ID = get_secret("vapi-cortex-credential-id")

SECTION_ORDER = [
    ("scope_of_practice", "Scope of Practice"),
    ("not_offered", "Not Offered"),
    ("callers_needs", "Caller's Needs"),
    ("protocols", "Protocols"),
]

# Universal Information-Capture guidance. Not tied to any single capability —
# every call, regardless of toggles, needs this framing.
_INFORMATION_CAPTURE_FRAGMENT = """## Information Capture
Based on the caller's intent (cross-reference the Caller's Needs section of the Knowledge Base), collect at minimum:
- Caller's name (as spoken).
- Callback phone number.
- Reason for the call (in the caller's words).
- For appointment requests: preferred day(s) and time window, new-vs-existing patient status.
- Any relevant clinical context surfaced by the clinic's qualifying questions (see the Protocols section of the Knowledge Base) — but only capture what the caller volunteers; do not press for medical details beyond what's needed for triage."""


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


def _instantiate_capabilities(
    clinic: dict,
    enabled_capability_ids: list[str],
) -> list[Capability]:
    """
    Build the ordered list of Capability instances for this clinic.

    Order:
      1. Toggleable capabilities (in CAPABILITY_REGISTRY declaration order) —
         only those whose ID is in enabled_capability_ids AND whose
         supported_pms includes this clinic's pms_type.
      2. Always-on capabilities (e.g. SubmitTicket) appended last so the
         "closing & ticket submission" block is at the end of the prompt's
         booking-protocols flow.

    Capabilities incompatible with the clinic's pms_type are skipped with a
    warning (the hypervisor should have refused the toggle at PUT time, so
    hitting this is a data-drift safety net).
    """
    enabled = set(enabled_capability_ids)
    instantiated: list[Capability] = []

    # Toggleable first, in registry order
    for cap_id, cls in CAPABILITY_REGISTRY.items():
        if cls.always_on:
            continue
        if cap_id not in enabled:
            continue
        try:
            instantiated.append(cls(clinic, _VAPI_CREDENTIAL_ID))
        except ValueError as e:
            log.warning(
                "Skipping enabled capability %s for clinic_id=%s: %s",
                cap_id, clinic.get("clinic_id"), e,
            )

    # Always-on last
    for cls in CAPABILITY_REGISTRY.values():
        if not cls.always_on:
            continue
        try:
            instantiated.append(cls(clinic, _VAPI_CREDENTIAL_ID))
        except ValueError as e:
            # An always-on capability that refuses this clinic is a spec bug
            # — e.g. SubmitTicket's supported_pms doesn't cover this PMS.
            raise RuntimeError(
                f"Always-on capability {cls.__name__} refused clinic: {e}"
            ) from e

    # Defense-in-depth: every assistant must have submit_ticket.
    if not any(isinstance(c, SubmitTicket) for c in instantiated):
        raise RuntimeError(
            "No SubmitTicket capability instantiated — assistant cannot persist call outcomes."
        )

    return instantiated


def _build_booking_protocols(caps: list[Capability]) -> str:
    """
    Compose the BOOKING PROTOCOLS section of the system prompt from capability
    fragments + the universal Information Capture block.

    Layout:
      <toggleable cap fragments, in instantiation order>
      <Information Capture — universal>
      <always-on cap fragments, in instantiation order>

    The always-on fragments come last because SubmitTicket's fragment is the
    "Closing & Ticket Submission" block — it belongs at the end of the flow.
    """
    toggleable_fragments = [c.prompt_fragment for c in caps if not c.always_on]
    always_on_fragments = [c.prompt_fragment for c in caps if c.always_on]
    parts = toggleable_fragments + [_INFORMATION_CAPTURE_FRAGMENT] + always_on_fragments
    return "\n\n".join(parts)


def build_system_prompt(
    clinic: dict,
    faqs: list,
    script_sections: dict[str, str],
    capabilities: list[Capability],
) -> str:
    """
    Assemble the voice agent system prompt.

    The prompt is split into two explicitly-labeled parts:

      KNOWLEDGE BASE — clinic-specific, compiled/curated:
        clinic metadata, the four approved script sections, and approved FAQs.
        Descriptive — what the clinic does and how it talks to callers.

      BOOKING PROTOCOLS — procedural, composed from enabled capabilities:
        the agent's operational playbook — which tools are available, when to
        use them, and how to close the call.

    These are intentionally separate. The knowledge base must never contain
    booking procedure; the booking protocols must not inline clinic facts.
    """
    script_block = _format_script_sections(script_sections)
    faqs_block = json.dumps(faqs or [], indent=2)
    booking_protocols = _build_booking_protocols(capabilities)

    return f"""The date today is {datetime.datetime.now().strftime("%Y-%m-%d")}.

You are the friendly, professional receptionist at {clinic["clinic_name"]}. Your job is to identify the caller's need, triage against the clinic's scope of practice, collect the information needed for follow-up, and create a ticket that clinic staff will use to call the patient back. Be empathetic when callers express frustration or distress about hearing difficulties — you are often the first voice they reach when they are already stressed.

This prompt has TWO distinct parts:

  1. KNOWLEDGE BASE — clinic-specific facts about what this clinic does and how
     it talks to callers. Use it to answer questions, triage needs, and match
     the clinic's voice. Descriptive, NOT procedural.
  2. BOOKING PROTOCOLS — your operational playbook. Procedural. Follow it
     consistently on every call, regardless of what the Knowledge Base says.

========================================================================
# KNOWLEDGE BASE
========================================================================

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

## Approved Script (authoritative)
Treat this as the source of truth for what this clinic does, does not do, who calls, and how the clinic talks to callers. If an FAQ below conflicts with this script, the script wins.

{script_block}

## Frequently Asked Questions (reference only)
Curated by the clinic for common caller questions. Reference material — script wins on conflict.

{faqs_block}

========================================================================
# BOOKING PROTOCOLS
========================================================================
This is YOUR operational playbook — how you handle a call end-to-end. It is the same on every call, regardless of clinic. Do not improvise a different flow even if the Knowledge Base above seems to suggest one; the Knowledge Base is descriptive, this section is procedural.

{booking_protocols}

========================================================================
# BEHAVIOUR GUIDELINES
========================================================================
- Be warm, concise, and professional.
- Do not invent information about services, pricing, availability, or patient records. If you do not know, say so and offer to collect the caller's details for a callback.
- Do not disclose internal identifiers (clinic_id, patient_id, etc.) to the caller.
- You are an AI receptionist. If the caller asks, you may confirm you are an AI. Do not claim to be human.
- Match the caller's pace. Older callers may need you to slow down, repeat, or speak clearly — do so without being asked.
"""


def build_agent_config(
    clinic: dict,
    faqs: list,
    script_sections: dict[str, str],
    enabled_capability_ids: list[str],
) -> dict:
    """
    Returns a complete VAPI assistant creation payload for the given clinic.

    Args:
        clinic:                 Clinic row from Users.clinics (must include
                                clinic_name, clinic_id, address, hours_*, pms_type).
        faqs:                   List of {question, answer} from ClinicData.faq
                                (pre-filtered to voice_assistant = TRUE).
        script_sections:        {section_name: content} — latest approved rows
                                for all four sections. Missing any raises KeyError.
        enabled_capability_ids: List of capability IDs from
                                Users.clinic_voice_agent_capabilities where
                                enabled=TRUE. Always-on capabilities (e.g.
                                submit_ticket) are always attached regardless.

    Returns:
        Dict suitable for passing as kwargs to client.assistants.create().
    """
    caps = _instantiate_capabilities(clinic, enabled_capability_ids)
    system_prompt = build_system_prompt(clinic, faqs, script_sections, caps)
    tools = [cap.to_vapi_tool() for cap in caps]

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
        "voice": {"speed": 0.9, "provider": "vapi", "voiceId": "Emma"},
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
