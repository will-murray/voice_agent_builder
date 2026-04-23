# Voice Agent v1 — Implementation Plan

Companion to the Agent Specification in `CLAUDE.md`. The spec defines *what* v1 is; this plan defines *how and in what order* it gets built across the three repos (`voice_agent_builder`, `cortex-hypervisor`, `big-query-ingestion`).

Phases are sequenced by dependency. Tasks within a phase can usually run in parallel.

---

## Phase 0 — Schema & verification ✅ DONE

- **0.1** ✅ `Users.agent_script_sections` created (migration `004_voice_agent_v1_tables.sql`).
- **0.2** ✅ `Users.voice_agent_tickets` created (same migration).
- **0.3** ✅ `Blueprint_PHI.ClientDemographics` carries `_clinic_id` (underscore prefix — ETL tag).
- **0.4** ✅ `ClinicData.faq.voice_assistant` populated (0 NULLs, 58 TRUE / 2123 FALSE out of 2181 rows).

---

## Phase 1 — Compiler migration (big-query-ingestion) ✅ IMPLEMENTED, NOT YET RUN

- **1.1** ✅ Rewrote `app/transcript_analysis/agent_script.py`:
  - Claude now returns JSON with four keys (`scope_of_practice`, `not_offered`, `callers_needs`, `protocols`).
  - `_load_current_sections` reads the latest row per section, any state (draft or approved).
  - `_append_sections` writes four `draft` rows per run, `source = "compiler"`.
  - No longer writes to `Users.agent_script`. Legacy table preserved as archive.
- **1.2** ✅ Added `app/transcript_analysis/backfill_agent_script_sections.py`:
  - One-shot CLI: reads latest blob per clinic from `Users.agent_script`, asks Claude to split into four sections, writes four `draft` rows with `source = "backfill"`.
  - Idempotent (skips clinics that already have rows in the new table; `--force` to override).
  - `--dry-run` and `--clinic-id <uuid>` flags supported.

**Not yet done:** run the backfill (requires Anthropic API call + BQ writes) and run the new compiler end-to-end against a real clinic. Both should happen before Phase 3 activates in production.

---

## Phase 2 — Hypervisor endpoints (cortex-hypervisor) ✅ IMPLEMENTED

- **2.1** ✅ `POST /blueprint/{clinic_id}/patient/match` (in `api/routers/blueprint.py`) — mandatory `WHERE _clinic_id = @clinic_id` filter, case-insensitive name match, last4-phone match across mobile/home/work, optional DOB tie-breaker. Returns `{status, patient_id?, candidates_count}`. No PHI leaks back to caller beyond opaque `patient_id`.
- **2.2** ✅ `POST /clinics/{clinic_id}/voice_agent/tickets` (in `api/routers/voice_agent.py`) — VAPI-authed, appends one row to `Users.voice_agent_tickets` with status='open'. Details dict serialized to JSON STRING.
- **2.3** ✅ `api/routers/agent_script.py`:
  - `GET /clinics/{clinic_id}/agent_script/sections` — returns latest draft + latest approved per section, plus `all_approved` bool.
  - `POST /clinics/{clinic_id}/agent_script/sections/{section_name}/approve` — takes a `section_id`, appends a new `approved` row with content copied verbatim, `approved_by` = caller's Firebase email, source='manual'.
  - Gated on Firebase `require_read_access` / `require_write_access`.
- **2.4** ✅ Activate endpoint updated with hard gate via `services/script_approval.require_full_approval(clinic_id)` — raises 409 listing missing sections when not all 4 approved.
- **2.5** ✅ Old `POST /blueprint/{clinic_id}/patient/lookup` endpoint left in place (retires in Phase 4.1).

**Not yet done:**
- ✅ **PHI-critical test landed** — `test_patient_match_phi_isolation.py` (10 passing tests) enforces: `_clinic_id = @clinic_id` filter is always in the SQL; clinic_id is sourced from path, not body; cross-clinic lookups return `unmatched`; ambiguous matches never leak a `patient_id`; last4 validation runs before any BQ query.
- Manual smoke tests of the approval flow + ticket submission (requires backfilled clinics + sign-in).
- Minor code smell: `voice_agent.py` imports `verify_vapi_secret` from `blueprint.py` — consider moving to `api/deps.py` or a shared module to avoid cross-router coupling.

---

## Phase 3 — Voice agent rewrite (voice_agent_builder) ✅ IMPLEMENTED

- **3.1** ✅ `tools/blueprint.py` — full rewrite. Now exposes `make_match_patient_tool(clinic_id)` → `/blueprint/{clinic_id}/patient/match` and `make_submit_ticket_tool(clinic_id)` → `/clinics/{clinic_id}/voice_agent/tickets`. `make_voice_agent_v1_tools()` returns both. The old three tools (`lookup`, `availability`, `appointment`) are gone.
- **3.2** ✅ `agent_factory.py` — full rewrite:
  - `build_system_prompt(clinic, faqs, script_sections, pms_type)` assembles: clinic name/address/hours → four script sections (as authoritative "Script" block) → patient-identification flow (branches on `pms_type == "blueprint"`) → "submit_ticket before hanging up" instruction → FAQs as reference-only appendix → behaviour guidelines. The precedence rule ("FAQ conflicts → script wins") is stated in the prompt.
  - `_build_tools(pms_type, clinic_id)` returns `[match, submit_ticket]` for Blueprint clinics and `[submit_ticket]` otherwise (non-Blueprint clinics still get info-collection + ticket).
  - `build_agent_config(clinic, faqs, script_sections)` — `appt_types` parameter dropped entirely.
- **3.3** ✅ `create_agent.py`:
  - `fetch_appt_types` removed.
  - `fetch_faqs(clinic_id)` now filters on `voice_assistant = TRUE`.
  - `fetch_script_sections(clinic_id)` added — returns latest approved row per section; raises `ValueError` if any of the four is missing (client-side guard mirroring the hypervisor's authoritative gate).
  - `sync_assistant(clinic_name)` fetches clinic → script sections (raises on gap) → FAQs → builds config → syncs VAPI assistant.
- **3.4** ✅ Deleted `tools/acuity_scheduling.py` and `auth.py`.
- **3.5** ✅ Superseded — provisioning orchestration already lives in cortex-hypervisor (`services/provisioning.py`, `services/vapi_provisioner.py`, `services/twilio_client.py`). No duplicate modules in voice_agent_builder.

**Not yet done:**
- End-to-end smoke test: pick a clinic with all four approved sections, run `python create_agent.py` (with clinic_name edited to the target), and verify the VAPI assistant gets created with the new prompt + two tools.
- Update the `CORTEX_API_BASE_URL` env var when agents are created for prod (defaults to `http://localhost:8000`).

---

## Phase 4 — Cleanup

Depends on Phase 3 being live across all production clinics.

- **4.1** Drop the old `POST /blueprint/{clinic_id}/patient/lookup` endpoint.
- **4.2** Retire the legacy `Users.agent_script` table (drop, or convert to a view over `Users.agent_script_sections`).

---

## Phase 5 — Frontend dashboard (partial — Scripts tab landed)

Section editing + approval UI via `/dashboard/manage/[instanceId]` → **Scripts** tab. Surface is live; other frontend work (voice agent status, clinic-side activation, etc.) is still part of the broader dashboard rework.

- ✅ `src/app/dashboard/manage/[instanceId]/tabs/ScriptsTab.tsx` — per-clinic expandable cards, four textarea editors per clinic (one per section), two action buttons per editor (**Save as draft** / **Approve**). Status badge + metadata captions per section.
- ✅ `GET /api/admin/instances/[id]/agent_script_sections` — returns latest draft + latest approved row per section for every clinic in the instance, plus `all_approved` bool.
- ✅ `POST /api/admin/clinics/[id]/agent_script/sections/[sectionName]` — writes a new row with `{content, state}`; `state='approved'` sets `approved_by` (Firebase email) + `approved_at`; `source='manual'` on all rows written from the dashboard.
- ✅ Access-controlled via existing `requireInstanceAccess` helper (admin of the clinic's instance, or super_admin).
- Type-checks clean; pre-existing `useSearchParams`/Suspense issue on `/dashboard/admin` is unrelated.

---

## Cross-cutting notes

- **Rollout**: the 4-of-4 gate means every clinic with an active agent today must have its compiled script backfilled and super-admin-approved *before* Phase 3 deploy — otherwise `sync_assistant` refuses and the agent can't be updated. Complete 1.2 and mass-approve before rolling Phase 3 out.
- **PHI tests**: the clinic_id filter on `/blueprint/{clinic_id}/patient/match` is PHI-critical. At least one test must prove cross-clinic leakage is impossible.
- **Risk ranking**: Phase 2 is highest-risk (new PHI-touching endpoints). Phase 3 is most disruptive (live agent behavior change). Phases 0, 1, 4 are low-risk.
- **Deferred from v1** (see spec's "Out of Scope" section): emergency call handling, recording-consent disclosure, after-hours vs. overflow differentiation, multi-language, ticket notification surface, instance-level shared data.