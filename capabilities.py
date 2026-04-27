"""
Voice agent capabilities — per-clinic, per-instance tool + prompt bundles.

Each Capability is a class that bundles three things that must move together:
  1. A VAPI `apiRequest` tool definition (the JSON the agent can call)
  2. A prompt fragment (markdown, injected into the Booking Protocols section
     of the system prompt so the agent knows *when* to call the tool)
  3. PMS compatibility metadata (which Users.clinics.pms_type values the
     capability supports)

Capabilities are instantiated per clinic. The __init__ binds clinic context
(clinic_id, clinic_name, pms_type) and the VAPI credential id, then validates
that the clinic's PMS is one the capability supports. PMS-specific routing
happens inside `to_vapi_tool()` (e.g. a Blueprint clinic gets a URL on the
/blueprint router; an Audit Data clinic would get a different URL).

Enablement is data-driven — `Users.clinic_voice_agent_capabilities` holds
per-clinic toggles. Definitions (this file) stay in code so tool JSON and
prompt text can't drift out of sync with the backend endpoints that service
them.

SubmitTicket is always-on (not exposed as a toggle in the dashboard). Every
call must produce a ticket.
"""
from __future__ import annotations

import os
from typing import ClassVar

_CORTEX_BASE = os.environ.get("CORTEX_API_BASE_URL", "http://localhost:8000")


# ── Base class ────────────────────────────────────────────────────────────────


class Capability:
    """Base class. Subclass per capability; set the ClassVars and implement
    to_vapi_tool() + prompt_fragment."""

    id: ClassVar[str]
    display_name: ClassVar[str]
    description: ClassVar[str]
    agent_tool_name: ClassVar[str]
    # Tuple of pms_type values this capability supports, or None for PMS-agnostic.
    # Empty tuple = supports nothing (unusable).
    supported_pms: ClassVar[tuple[str, ...] | None] = None
    # True = instantiated on every sync regardless of toggle state.
    # Used for foundational capabilities (e.g. submit_ticket).
    always_on: ClassVar[bool] = False

    def __init__(self, clinic: dict, credential_id: str):
        self.clinic_id: str = clinic["clinic_id"]
        self.clinic_name: str = clinic.get("clinic_name", "")
        self.pms_type: str = clinic.get("pms_type") or "none"
        self.credential_id: str = credential_id

        if self.supported_pms is not None and self.pms_type not in self.supported_pms:
            raise ValueError(
                f"{type(self).__name__} does not support pms_type={self.pms_type!r} "
                f"(supported: {self.supported_pms})"
            )

    def to_vapi_tool(self) -> dict:
        raise NotImplementedError

    @property
    def prompt_fragment(self) -> str:
        raise NotImplementedError


# ── Capabilities ──────────────────────────────────────────────────────────────


class SubmitTicket(Capability):
    id = "submit_ticket"
    display_name = "Submit ticket"
    description = (
        "Foundational. Every call produces one ticket summarising the caller's "
        "need, collected info, and a suggested follow-up for clinic staff."
    )
    agent_tool_name = "submit_ticket"
    supported_pms = None  # PMS-agnostic — writes to our BQ, not a PMS
    always_on = True

    def to_vapi_tool(self) -> dict:
        return {
            "type": "apiRequest",
            "name": self.agent_tool_name,
            "description": (
                "Submit a ticket summarizing this call. Call this exactly once, "
                "right before you end the conversation. The ticket is what clinic "
                "staff will see to follow up. If you do not call this, the call "
                "is lost from the clinic's point of view."
            ),
            "url": f"{_CORTEX_BASE}/clinics/{self.clinic_id}/voice_agent/tickets",
            "method": "POST",
            "credentialId": self.credential_id,
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
                        "description": "1-2 sentence recap of the call for clinic staff.",
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

    @property
    def prompt_fragment(self) -> str:
        return """## Closing & Ticket Submission
Before ending the call:
1. Summarize back to the caller what you've captured and confirm it's correct.
2. Let them know a team member will follow up — you cannot confirm a specific appointment time.
3. Call `submit_ticket` EXACTLY ONCE with:
   - caller_name, caller_phone (E.164), patient_match_status, blueprint_patient_id (if matched), last4_confirmed.
   - intent_category: the label from the Knowledge Base's Caller's Needs section that best fits.
   - summary: 1-2 sentences for clinic staff.
   - details: any intent-specific fields you collected.
   - suggested_followup: concrete next action (e.g. "book hearing test, afternoon preference", "return wax-removal referral").
   - urgency: 'urgent' only if the caller reported a time-sensitive medical concern; otherwise 'normal'.
4. Warm goodbye, end the call.

If `submit_ticket` fails, apologize, tell the caller you'll have a team member call back, and log the failure in your final message."""


class PatientMatch(Capability):
    id = "patient_match"
    display_name = "Patient identity verification"
    description = (
        "Confirm an existing patient by first + last name and the last 4 digits "
        "of the phone number on file. Requires the patient record to already "
        "exist in the PMS."
    )
    agent_tool_name = "match_patient_by_name"
    supported_pms = ("blueprint",)

    def _tool_url(self) -> str:
        if self.pms_type == "blueprint":
            return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/patient/match"
        # When Audit Data lands, add its own branch here and extend supported_pms.
        raise NotImplementedError(f"patient_match not routed for pms={self.pms_type}")

    def to_vapi_tool(self) -> dict:
        return {
            "type": "apiRequest",
            "name": self.agent_tool_name,
            "description": (
                "Look up an existing patient in the clinic's records by first name, "
                "last name, and the last 4 digits of the phone number on file. "
                "Only call this after the caller confirms they are an existing patient. "
                "Returns 'matched' (patient identified uniquely), 'ambiguous' (multiple "
                "candidates — retry with the caller's date of birth), or 'unmatched' "
                "(treat the caller as new). The tool never reveals a patient's name, "
                "phone number, or DOB — only a status and an opaque patient identifier."
            ),
            "url": self._tool_url(),
            "method": "POST",
            "credentialId": self.credential_id,
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

    @property
    def prompt_fragment(self) -> str:
        return """## Patient Identification
1. Early in the call, ask whether the caller has been to the clinic before.
2. If yes:
   a. Collect their first and last name.
   b. Ask for the last 4 digits of the phone number on file.
   c. Call `match_patient_by_name` with those three fields.
   d. If the result is `matched`, note the returned `patient_id` for the ticket.
   e. If the result is `ambiguous`, ask for the caller's date of birth and retry with the `dob` field.
   f. If the result is `unmatched` after your best effort, treat the caller as new and ask for a callback phone number.
3. If no: treat as a new patient — collect full name and callback phone number directly.

You never learn the patient's full record — only a yes/no/ambiguous status and an opaque patient_id. Never pretend you know details about a patient beyond what the caller has told you directly."""


class ListAppointmentTypes(Capability):
    id = "list_appointment_types"
    display_name = "Appointment type lookup"
    description = (
        "Look up the clinic's bookable appointment types (e.g. 'Hearing test', "
        "'Fitting') and their durations. Required precondition for finding "
        "available slots — every availability search needs an appointment "
        "type ID."
    )
    agent_tool_name = "list_appointment_types"
    supported_pms = ("blueprint",)

    def _tool_url(self) -> str:
        if self.pms_type == "blueprint":
            return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/appointment-types"
        raise NotImplementedError(f"list_appointment_types not routed for pms={self.pms_type}")

    def to_vapi_tool(self) -> dict:
        return {
            "type": "apiRequest",
            "name": self.agent_tool_name,
            "description": (
                "Return the clinic's bookable appointment types as a list of "
                "{id, name, duration_minutes}. Call this once before "
                "find_available_slots so you know which event_type_id to "
                "use. Match the caller's stated need to one of the names — "
                "for hearing concerns the type is usually 'Hearing test'; "
                "for hearing-aid fitting the type is usually 'Fitting'."
            ),
            "url": self._tool_url(),
            "method": "POST",
            "credentialId": self.credential_id,
            "body": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": (
                            "A brief phrase describing why you're calling this tool "
                            "(e.g. 'caller wants a hearing test'). Optional — used "
                            "for call observability only; the server ignores it. "
                            "VAPI's schema requires the body to declare at least "
                            "one property."
                        ),
                    },
                },
            },
        }

    @property
    def prompt_fragment(self) -> str:
        return """## Appointment Types
When the caller asks about availability or you need to know what kind of visit they want, call `list_appointment_types`. The response is a list of {id, name, duration_minutes}. Match the caller's stated need to a name — usually:
- "Hearing test" / "test my hearing" / "I think I need hearing aids" → the 'Hearing test' type
- "Pick up my hearing aids" / "fitting" / "follow-up" → the 'Fitting' type
- Anything else → ask a clarifying question or use the closest match
Hold onto the matching `id` — you'll pass it to `find_available_slots` as `event_type_id`."""


class FindAvailableSlots(Capability):
    id = "find_available_slots"
    display_name = "Bookable appointment slot search"
    description = (
        "Find concrete bookable time slots in a date range for a specific "
        "appointment type. Returns dates and times the clinic actually has "
        "open — not just provider work blocks. Requires an event_type_id, "
        "obtained via list_appointment_types."
    )
    agent_tool_name = "find_available_slots"
    supported_pms = ("blueprint",)

    def _tool_url(self) -> str:
        if self.pms_type == "blueprint":
            return f"{_CORTEX_BASE}/blueprint/{self.clinic_id}/availability/find"
        raise NotImplementedError(f"find_available_slots not routed for pms={self.pms_type}")

    def to_vapi_tool(self) -> dict:
        return {
            "type": "apiRequest",
            "name": self.agent_tool_name,
            "description": (
                "Find bookable appointment slots in a date range for a specific "
                "appointment type. Returns {days: [{date, available_times: [HH:MM, ...]}]}. "
                "You MUST supply event_type_id — get it from list_appointment_types "
                "first. Use a 1-2 week window unless the caller specifies otherwise. "
                "You CANNOT book a slot yourself; capture the caller's preference "
                "in the ticket and tell them clinic staff will confirm."
            ),
            "url": self._tool_url(),
            "method": "POST",
            "credentialId": self.credential_id,
            "body": {
                "type": "object",
                "properties": {
                    "event_type_id": {
                        "type": "integer",
                        "description": (
                            "The appointment type ID from list_appointment_types. "
                            "Required — without it the search has no idea what "
                            "duration / resource constraints apply."
                        ),
                    },
                    "start_date": {
                        "type": "string",
                        "description": (
                            "Start of the search window in YYYY-MM-DD format "
                            "(clinic local time)."
                        ),
                    },
                    "end_date": {
                        "type": "string",
                        "description": (
                            "End of the search window in YYYY-MM-DD format "
                            "(clinic local time, inclusive)."
                        ),
                    },
                },
                "required": ["event_type_id", "start_date", "end_date"],
            },
        }

    @property
    def prompt_fragment(self) -> str:
        return """## Finding Available Slots
After you know which appointment type fits the caller's need (via `list_appointment_types`), call `find_available_slots` with that `event_type_id` and a 1-2 week date range. The response is `{days: [{date, available_times: [HH:MM, ...]}]}` — concrete bookable slots, not just provider work blocks. Use it to tell the caller which days/times look open ("Tuesday morning has 9am, 9:30, and 10:30 open"). You CANNOT book any specific slot yourself; capture the caller's preferred day and time in the ticket and tell them clinic staff will confirm."""


# ── Registry ──────────────────────────────────────────────────────────────────


CAPABILITY_REGISTRY: dict[str, type[Capability]] = {
    SubmitTicket.id: SubmitTicket,
    PatientMatch.id: PatientMatch,
    ListAppointmentTypes.id: ListAppointmentTypes,
    FindAvailableSlots.id: FindAvailableSlots,
}


def toggleable_capabilities() -> list[type[Capability]]:
    """Capabilities that admins can turn on/off from the dashboard.
    Excludes always-on foundational capabilities."""
    return [cls for cls in CAPABILITY_REGISTRY.values() if not cls.always_on]
