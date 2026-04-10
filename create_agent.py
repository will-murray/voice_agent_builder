from bigquery_client import run_query
from google.cloud import bigquery
from vapi import Vapi
from auth import VAPI_TOKEN
import json
import datetime

VAPI_BASE = "https://api.vapi.ai"
HEADERS = {"Authorization": f"Bearer {VAPI_TOKEN}", "Content-Type": "application/json"}
client = Vapi(token=VAPI_TOKEN)


def build_first_message(clinic_name: str):
    return f"you've reached {clinic_name} how can I assist you today"


def fetch_faqs(clinic_name: str):
    rows = run_query(
        "SELECT question, answer FROM `project-demo-2-482101.ClinicData.faq` WHERE clinic_name = @clinic_name",
        params=[bigquery.ScalarQueryParameter("clinic_name", "STRING", clinic_name)],
    )
    if len(rows) == 0:
        return None
    return rows


def fetch_appt_types(clinic_name: str):
    rows = run_query(
        "SELECT clinic_name, appointment_name, duration FROM `project-demo-2-482101.Users.appointment_types` WHERE clinic_name = @clinic_name",
        params=[bigquery.ScalarQueryParameter("clinic_name", "STRING", clinic_name)],
    )
    if len(rows) == 0:
        return None
    return rows


def build_system_prompt(clinic_name: str):
    """Builds the VAPI assistant system prompt for the given clinic."""
    rows = run_query(
        """
        SELECT clinic_name, address, about_us, hours_monday, hours_tuesday, hours_wednesday,
               hours_thursday, hours_friday, hours_saturday, hours_sunday, clinic_id, phone,
               parking_info, accessibility_info, timezone, booking_system
        FROM `project-demo-2-482101.Users.clinics`
        WHERE clinic_name = @clinic_name
        """,
        params=[bigquery.ScalarQueryParameter("clinic_name", "STRING", clinic_name)],
    )

    if not rows:
        raise ValueError(f"Clinic not found: {clinic_name}")

    D = rows[0]

    appt_types = run_query(
        "SELECT * FROM `project-demo-2-482101.Users.appointment_types` WHERE clinic_name = @clinic_name",
        params=[bigquery.ScalarQueryParameter("clinic_name", "STRING", clinic_name)],
    )

    system_prompt = f"""
        The date today is {datetime.datetime.now().strftime("%Y-%m-%d")}.

        You are a friendly and professional receptionist at {D["clinic_name"]}.
        Your job is to assist callers by answering questions about the clinic, providing information about services, and helping with appointment bookings.
        You cannot confirm appointment bookings. Instead your job is to collect information regarding appointment booking and send the information to the relevant party who will confirm the booking.

        ## About the Clinic
        {D["about_us"]}

        ## Contact & Location
        - Address: {D["address"]}
        - Phone: {D["phone"]}

        ## Parking & Accessibility
        - Parking: {D["parking_info"]}
        - Accessibility: {D["accessibility_info"]}

        ## Hours of Operation (Timezone: {D["timezone"]})
        - Monday: {D["hours_monday"]}
        - Tuesday: {D["hours_tuesday"]}
        - Wednesday: {D["hours_wednesday"]}
        - Thursday: {D["hours_thursday"]}
        - Friday: {D["hours_friday"]}
        - Saturday: {D["hours_saturday"]}
        - Sunday: {D["hours_sunday"]}

        ## Booking
        The clinic uses {D["booking_system"]} for appointment scheduling.
        When a caller wants to book, reschedule, or cancel an appointment, use the appropriate booking tools available to you.

        ## FAQ
        {json.dumps(fetch_faqs(clinic_name) or [])}

        ## Appointment Types
        {json.dumps(appt_types or [])}

        ## Behaviour Guidelines
        - Be warm, concise, and professional at all times.
        - If you don't know the answer to a question, offer to take a message or direct the caller to call back during business hours.
        - Never guess at appointment availability — always use the booking tools to check.
        - Do not share the clinic_id ({D["clinic_id"]}) with callers.
        """

    return system_prompt


def fetch_assistant(clinic_name: str):
    assistants = client.assistants.list()
    return next((a for a in assistants if a.name == clinic_name), None)


def create_assistant(clinic_name: str):
    """Builds and creates the VAPI assistant for the given clinic."""
    sys_prompt = build_system_prompt(clinic_name)
    model = {
        "provider": "openai",
        "model": "gpt-4o",
        "messages": [{"role": "system", "content": sys_prompt}],
    }

    return client.assistants.create(
        first_message=build_first_message(clinic_name),
        first_message_interruptions_enabled=True,
        model=model,
        name=clinic_name,
    )


def sync_assistant(clinic_name: str):
    """Deletes the existing assistant if present, then creates a fresh one."""
    existing = fetch_assistant(clinic_name)
    if existing is not None:
        print(f"Deleting existing assistant for '{clinic_name}' (id={existing.id})")
        client.assistants.delete(existing.id)

    print(f"Creating assistant for '{clinic_name}'")
    return create_assistant(clinic_name)


if __name__ == "__main__":
    clinic_name = "Audiology Clinic of Northern Alberta"
    sync_assistant(clinic_name)
