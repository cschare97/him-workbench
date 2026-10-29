# Specialty Referral Tracking — Standard Operating Procedure

This SOP describes the workflow implemented by HIM Workbench's referral tracker
(`src/him_workbench/referrals.py`, the **Referrals** page in the web UI). It is a generic
procedure written to match what the tool actually enforces — not a reproduction of any
employer's internal referral SOP, and not tied to any specific EHR. Synthetic data only.

## Purpose

Track a specialty referral from the moment it is sent until the patient is seen (or the
referral is declined), and surface the ones that stall before anyone has to go looking for them.

## Roles

| Role | Responsibility |
|---|---|
| Referring provider | Decides the patient needs a specialist, enters the referral. Required field. |
| Receiving provider | The specialist or practice the patient is being referred to. Optional — not always known at the time of referral. |
| Referral coordinator | Assigned to follow up if the referral stalls. Picked from a fixed list (`Unassigned`, or a named coordinator); defaults to `Unassigned` if not set. |

## Procedure

1. **Log the referral.** Capture: patient, specialty, priority (routine/urgent), reason for
   referral, referring provider, and — when known — receiving provider, diagnosis, one or more
   ICD-10-CM codes, and any notes. The referral starts in status **Sent**.
2. **Assign a coordinator (optional).** If nobody owns follow-up yet, it stays `Unassigned`;
   assign a coordinator at logging time or later by re-logging status with a note.
3. **Monitor for a response.** A referral still **Sent** with no status change after the
   follow-up window (14 calendar days by default, `HIM_REFERRAL_FOLLOWUP_DAYS` to change it) is
   flagged **no response** at the top of the referral list. This is a tracking setting, not a
   regulatory deadline — unlike the ROI tracker's HIPAA-based one.
4. **Schedule.** When the specialist's office confirms an appointment, move the referral to
   **Scheduled** and record the appointment date. A scheduled referral is never flagged "no
   response," even if the appointment is still weeks out.
5. **Resolve.** Once the visit happens, move the referral to **Completed**. A referral cannot
   be marked Completed until it has been Scheduled — this is enforced, not just a convention.
   If the specialist's office declines the referral (not accepting new patients, out of network,
   etc.) at any open stage, move it to **Declined** with a required note explaining why.
6. **Closed means closed.** Completed and Declined referrals cannot be reopened or edited.
   Every status change is kept in a dated history on the referral's detail page, so the full
   timeline — who did what, and when — is always visible, never overwritten.

## What gets flagged, and why

The only automatic flag is **no response**: a referral sent but never scheduled, past the
follow-up window. That is the failure mode worth catching — a referral that went out and was
never followed up on, not one that is simply waiting on a far-out appointment (which is
"Scheduled," not flagged).

## Fields reference

| Field | Required | Notes |
|---|---|---|
| Patient | Yes | One of the loaded synthetic patients |
| Specialty | Yes | Free text, e.g. "Cardiology" |
| Priority | Yes | `routine` or `urgent` |
| Reason for referral | Yes | Free text, 300-character limit |
| Referring provider | Yes | Free text |
| Receiving provider | No | Free text |
| Diagnosis | No | Free text |
| ICD-10-CM codes | No | Validated for shape (e.g. `R00.2`), not checked against a loaded code file |
| Coordinator | No | Defaults to `Unassigned` |
| Notes | No | Free text, separate from Reason |
| Appointment date | No | Captured when moving to Scheduled |

## Out of scope

This tool does not read from or write to any EHR's own referral or transaction tables
(OpenEMR's included) — it is a standalone synthetic tracker. It is not legal or billing advice.
