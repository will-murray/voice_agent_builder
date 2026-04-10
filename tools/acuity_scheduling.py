import os
import base64
import requests
from dotenv import load_dotenv
from vapi import Vapi
from vapi.types import CreateApiRequestToolDto, JsonSchema

load_dotenv()

VAPI_TOKEN = os.getenv("VAPI_API_KEY")
ACUITY_USER_ID = os.getenv("ACUITY_USER_ID")
ACUITY_API_KEY = os.getenv("ACUITY_API_KEY")

VAPI_BASE = "https://api.vapi.ai"
VAPI_HEADERS = {
    "Authorization": f"Bearer {VAPI_TOKEN}",
    "Content-Type": "application/json"
}

client = Vapi(token=VAPI_TOKEN)


def _create_acuity_credential() -> str:
    """
    Creates a Vapi custom credential that injects the Acuity Basic auth header.
    Uses bearerPrefixEnabled=False so the token value is sent verbatim,
    allowing us to embed 'Basic ' in the token string itself.
    """
    encoded = base64.b64encode(f"{ACUITY_USER_ID}:{ACUITY_API_KEY}".encode()).decode()
    payload = {
        "provider": "custom-credential",
        "name": "acuity-basic-auth",
        "authenticationPlan": {
            "type": "bearer",
            "token": f"Basic {encoded}",
            "headerName": "Authorization",
            "bearerPrefixEnabled": False
        }
    }
    response = requests.post(
        f"{VAPI_BASE}/credential",
        headers=VAPI_HEADERS,
        json=payload
    )
    response.raise_for_status()
    return response.json()["id"]


def create_fetch_appointment_types_tool(credential_id: str) -> dict:
    tool = CreateApiRequestToolDto(
        name="fetch_appointments_type",
        description=(
            """
            Fetches the types of appointments which the clinic offers.
            Use this if asked what types of appointments are available.
            If a client asks to book an appointment fetch the info here and present them with options.
            """
        ),
        url="https://acuityscheduling.com/api/v1/appointment-types",
        method="GET",
        credential_id=credential_id
    )
    client.tools.create(request=tool)


def create_fetch_appointments_tool(credential_id: str) -> dict:
    # minDate and maxDate are VAPI URL template variables — filled at call time by VAPI.
    # Do not change to an f-string; {minDate} and {maxDate} are intentional templates.
    tool = CreateApiRequestToolDto(
        name="fetch_appointments",
        description=(
            "Fetches scheduled appointments from Acuity Scheduling within a date range. "
            "Use this to check what appointments are booked."
        ),
        url="https://acuityscheduling.com/api/v1/appointments?minDate={minDate}&maxDate={maxDate}",
        method="GET",
        credential_id=credential_id,
        body=JsonSchema(
            type="object",
            properties={
                "minDate": {
                    "type": "string",
                    "description": "Start of the date range in YYYY-MM-DD format (e.g. 2024-03-01)."
                },
                "maxDate": {
                    "type": "string",
                    "description": "End of the date range in YYYY-MM-DD format (e.g. 2024-03-31)."
                }
            },
            required=["minDate", "maxDate"]
        )
    )
    client.tools.create(request=tool)


if __name__ == "__main__":
    credential_id = _create_acuity_credential()
    create_fetch_appointment_types_tool(credential_id)
    create_fetch_appointments_tool(credential_id)
