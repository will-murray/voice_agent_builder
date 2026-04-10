# voice_agent_builder — AI Voice Agent Builder

## Overview

Creates and manages VAPI AI voice receptionists for audiology clinics. Voice agents are opt-in per clinic. The agent handles inbound calls that the clinic's receptionist did not answer (forwarded on no-answer from the clinic's primary phone system) and outbound calls for appointment confirmations and patient reactivation.

## Commands

```bash
python create_agent.py    # Create/update a single clinic's agent (dev/manual use only)
```

Production activation is triggered via the cortex-hypervisor `POST /clinics/{clinic_id}/voice_agent/activate` endpoint, not by running this script directly.

## Stack

- Python 3.12
- VAPI Python SDK (`vapi-python`) — voice agent platform
- Twilio — phone number provisioning and SMS
- Google BigQuery — reads clinic data, FAQs, appointment types
- Anthropic Claude — used in transcript analysis (in big-query-ingestion, not here)

## Call Flow

```
Patient calls clinic primary number (RingCentral / Weave / Vonage / etc.)
    → Clinic receptionist does not answer (or after hours)
        → Clinic phone system forwards to Twilio number
            → VAPI agent answers
                → Looks up patient in Blueprint OMS by caller phone number
                → Collects appointment info / answers questions
                → Logs outcome
```

The Twilio number is the agent's dedicated line — not the clinic's primary number. Each clinic gets one Twilio number when voice agent is activated.

## Project Layout

```
create_agent.py         # Core agent build logic — being refactored (see Pending Work)
auth.py                 # VAPI_TOKEN from .env
bigquery_client.py      # BigQuery client + run_query()
tools/
  acuity_scheduling.py  # BROKEN — do not use (see Pending Work 2-A)
  blueprint.py          # PLANNED: Blueprint OMS VAPI tools
provisioning.py         # PLANNED: full clinic agent setup orchestrator
agent_factory.py        # PLANNED: builds agent config by clinic + PMS type
vapi_client.py          # PLANNED: single shared VAPI client (see 3-F)
twilio_client.py        # PLANNED: Twilio phone number provisioning
```

## Current `create_agent.py` Functions

| Function | Purpose |
|---|---|
| `build_first_message(clinic_name)` | Opening greeting string |
| `fetch_faqs(clinic_name)` | Pulls FAQ rows from `ClinicData.faq` |
| `fetch_appt_types(clinic_name)` | Pulls appointment types from `Users.appointment_types` |
| `build_system_prompt(clinic_name)` | Assembles full system prompt from clinic data + FAQs |
| `fetch_assistant(clinic_name)` | Finds existing VAPI assistant by name |
| `create_assistant(clinic_name)` | Creates new VAPI assistant |
| `sync_assistant(clinic_name)` | Deletes existing + creates fresh assistant |

**Current model:** GPT-4o via VAPI's OpenAI provider.

## Planned Blueprint OMS Tools (`tools/blueprint.py`)

Three VAPI tools exposed to the agent:

```python
# Tool 1: lookup_patient_by_phone
# Blueprint API: GET /rest/client/show?event=ringing&callerid={phone}
# Called at call start — personalizes the greeting with patient name

# Tool 2: check_availability
# Blueprint API: GET /rest/availability/?eventTypeId=...&startTime=...&endTime=...
# Returns available appointment slots

# Tool 3: create_appointment
# Blueprint API: POST /rest/appointments/
# Creates appointment with existing patient (patientId) or new QuickAdd patient
```

## Planned `agent_factory.py`

Selects the correct PMS tool set based on `clinic.pms_type`:

```python
def build_tools(clinic: Clinic) -> list:
    base_tools = [clinic_context_tool, form_capture_tool]
    if clinic.pms_type == "blueprint":
        return base_tools + [blueprint.lookup_patient_by_phone,
                             blueprint.check_availability,
                             blueprint.create_appointment]
    return base_tools  # no PMS — info-collection only
```

When a new PMS is added, only a new `tools/<pms>.py` file is needed — the factory selects it automatically.

## Planned `provisioning.py` — Activation Flow

Called by cortex-hypervisor when admin activates voice agent for a clinic:

```python
def provision_clinic_voice_agent(clinic_id: str, area_code: str):
    # 1. Buy Twilio number (area_code from clinic.phone)
    # 2. Import Twilio number to VAPI → get vapi_phone_number_id
    # 3. Build system prompt via build_system_prompt(clinic_name)
    # 4. Create VAPI assistant → get vapi_assistant_id
    # 5. Assign Twilio number to assistant in VAPI
    # 6. Write twilio_phone_number + vapi_assistant_id to clinic record in BQ
    # 7. Return twilio_phone_number for display in dashboard
```

Deactivation:
```python
def deprovision_clinic_voice_agent(clinic_id: str):
    # 1. Delete VAPI assistant
    # 2. Release Twilio number
    # 3. Clear voice agent fields on clinic record
```

## Twilio Integration Notes

- One Twilio number per clinic
- Outbound caller ID: verify clinic's primary phone number with Twilio so outbound calls show the clinic's familiar number (not the Twilio number)
- Caller ID passthrough: when clinic phone system forwards to VAPI, the original patient phone number must be passed through (not the clinic's number) — required for Blueprint CTI lookup. Verify this works per phone system type during onboarding.

## Pending Work

### Must fix (blockers)
- **2-A** (critical — run nothing until these are fixed):
  - `create_agent.py` last two lines — `sync_assistant("Audiology Clinic of Northern Alberta")` runs at import. Wrap in `if __name__ == "__main__":`.
  - `create_agent.py:56` — `rows[0]` with no bounds check. Add `if not rows: raise ValueError(f"Clinic not found: {clinic_name}")`.
  - `create_agent.py:99–102` — `json.dumps(fetch_faqs(...))` will produce `"null"` if FAQs are empty. Use `json.dumps(fetch_faqs(clinic_name) or [])`.
  - `create_agent.py:65` — redundant in-memory filter after SQL `WHERE` clause. Remove.
  - `tools/acuity_scheduling.py:102` — `create_fetch_appointment_types_tool()` at module level. Wrap in `if __name__ == "__main__":`.
  - `tools/acuity_scheduling.py:80` — `{minDate}` / `{maxDate}` are undefined — `NameError` on call.
  - `tools/acuity_scheduling.py:51,71` — `_create_acuity_credential()` called twice. Create once, pass `credential_id` to both tool functions.
  - `create_agent.py:149–151` — `sync_assistant()` at module level. Wrap in `if __name__ == "__main__":`.

### Structural refactoring
- **3-F** (depends on 2-A) — `create_agent.py` and `tools/acuity_scheduling.py` each define `VAPI_BASE`, `VAPI_TOKEN`, `VAPI_HEADERS`, and a `Vapi()` client independently. Create `vapi_client.py` with a single shared client. Import from both files.

### SQL injection (must fix — 1-B)
`create_agent.py:21–63` interpolates `clinic_name` directly into BigQuery SQL strings across four queries. `bigquery_client.py:34–38` already supports `run_query(sql, params)`. Replace with `@clinic_name` placeholder:

```python
rows = run_query(
    "SELECT ... FROM `project.dataset.clinics` WHERE clinic_name = @clinic_name",
    params=[bigquery.ScalarQueryParameter("clinic_name", "STRING", clinic_name)]
)
```

## Environment Variables (`.env`)

```
VAPI_TOKEN=
GCS_SERVICE_ACCOUNT=    # JSON string — Google Cloud service account for BigQuery
GCP_PROJECT=
TWILIO_ACCOUNT_SID=     # PLANNED
TWILIO_AUTH_TOKEN=      # PLANNED
BLUEPRINT_API_KEY=      # Per-clinic — stored in cortex-hypervisor, passed at runtime
```
