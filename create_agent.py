"""
Manual CLI tool for creating or refreshing a clinic's VAPI voice agent.

In production, agent creation is triggered via:
    POST /clinics/{clinic_id}/voice_agent/activate  (cortex-hypervisor)

This script exists for one-off updates and development testing. It fetches
clinic metadata, approved FAQs, and approved script sections from BigQuery,
builds a VAPI assistant config via agent_factory, and syncs the assistant in
VAPI.

The 4-of-4 approved-sections gate is enforced both here (client-side guard
via fetch_script_sections) and in cortex-hypervisor (authoritative). See
voice_agent_builder/CLAUDE.md "Agent Specification".
"""
from bigquery_client import run_query
from google.cloud import bigquery
from vapi_client import client
from agent_factory import build_agent_config, SECTION_ORDER



def fetch_clinic(clinic_name: str) -> dict:
    rows = run_query(
        """
        SELECT clinic_name, address, hours_monday, hours_tuesday, hours_wednesday,
               hours_thursday, hours_friday, hours_saturday, hours_sunday,
               clinic_id, timezone, pms_type
        FROM `project-demo-2-482101.Users.clinics`
        WHERE clinic_name = @clinic_name
        """,
        params=[bigquery.ScalarQueryParameter("clinic_name", "STRING", clinic_name)],
    )
    if not rows:
        raise ValueError(f"Clinic not found: {clinic_name}")
    return rows[0]


def fetch_faqs(clinic_id: str) -> list:
    """Fetch FAQs approved for the voice agent (voice_assistant = TRUE)."""
    rows = run_query(
        """
        SELECT question, answer
        FROM `project-demo-2-482101.ClinicData.faq`
        WHERE clinic_id = @clinic_id AND voice_assistant = TRUE
        """,
        params=[bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id)],
    )
    return rows or []


def fetch_script_sections(clinic_id: str) -> dict[str, str]:
    """
    Return {section_name: content} of the latest approved row per section.

    Raises ValueError if any of the four required sections has no approved
    row — matches the provisioning hard gate enforced by cortex-hypervisor.
    """
    rows = run_query(
        """
        WITH ranked AS (
            SELECT section_name, content,
                   ROW_NUMBER() OVER (PARTITION BY section_name ORDER BY created_at DESC) AS rn
            FROM `project-demo-2-482101.Users.agent_script_sections`
            WHERE clinic_id = @clinic_id AND state = 'approved'
        )
        SELECT section_name, content FROM ranked WHERE rn = 1
        """,
        params=[bigquery.ScalarQueryParameter("clinic_id", "STRING", clinic_id)],
    )
    sections = {r["section_name"]: r["content"] for r in rows}
    required = {key for key, _ in SECTION_ORDER}
    missing = required - set(sections)
    if missing:
        raise ValueError(
            f"Clinic {clinic_id} is missing approved sections: {sorted(missing)}. "
            "Approve all four sections in the dashboard before syncing the agent."
        )
    return sections


def fetch_assistant(clinic_name: str):
    assistants = client.assistants.list()
    return next((a for a in assistants if a.name == clinic_name), None)


def sync_assistant(clinic_name: str):
    """Delete the existing assistant if present, then create a fresh one."""
    clinic = fetch_clinic(clinic_name)
    clinic_id = clinic["clinic_id"]

    faqs = fetch_faqs(clinic_id)
    script_sections = fetch_script_sections(clinic_id)

    config = build_agent_config(clinic, faqs, script_sections)

    existing = fetch_assistant(clinic_name)
    if existing is not None:
        print(f"Deleting existing assistant for '{clinic_name}' (id={existing.id})")
        client.assistants.delete(existing.id)

    print(f"Creating assistant for '{clinic_name}'")
    assistant = client.assistants.create(**config)
    print(f"Created assistant id={assistant.id}")
    return assistant


if __name__ == "__main__":
    clinic_name = "Audiology Clinic of Northern Alberta"
    sync_assistant(clinic_name)
