# Executive CRM rollout and operating guide

This update adds executive discovery and sales tracking while preserving existing LinkedIn workflows, bearer tokens, outreach history and company IDs. Earlier Word handover manuals describe the baseline; this guide describes the new workflow.

## Daily workflow

1. A system administrator provisions each client account executive in Users, using their Google email and the new role. Existing Google sign-in is unchanged.
2. Sales interns and inside-sales users do their pre-escalation work outside the system, then choose Submit lead. Company, named contact, one contact method, context and an executive are required. Website and research fields are not required. The source channel is enforced from the user's role. Managers choose intern or inside sales when submitting.
3. LinkedIn operators use their existing prospect/outreach screens. Escalate to executive explicitly confirms a client reply, records context, and selects an executive. A factual outreach note or old founder-escalation flag alone never creates an executive handoff.
4. All executives see all escalated leads. Only the assigned executive, manager or system administrator may edit or comment. Original operators can read their own handoff, but cannot edit discovery or access the sales pipeline. Managers can reassign; previous executive edit access ends immediately. Inactive assignments remain visible and can be reassigned.
5. The executive tracks follow-up, next action/date and discovery meeting/outcome. Meetings use an explicit IANA time zone; ambiguous or nonexistent daylight-saving wall times are rejected. Drop requires a reason. No deadline automatically drops or converts a lead. A dropped lead may be reopened explicitly.
6. Move to sales pipeline creates one linked opportunity with a decision note. Stages are Qualified, Solution discussion, Proposal, Negotiation, Won and Lost. Requirements, probability and stage are required; value/currency and close date can be filled when known. Closing requires a reason; won/lost sets probability to 100/0. Reopening clears the close timestamp. Queue ownership remains the single source of executive ownership after conversion.

Both lists provide company/contact search, executive and channel filters, escalation date ranges, status/stage and action-date filters. Pipeline also filters expected close dates. Active deal values are totaled separately by currency; no exchange-rate conversion or combined currency total is implied. Summary counts reflect the current filters. Activity is paginated without truncating history; comments and before/after changes are append-only in the application.

## Multiple roles and sign-in

System administrators open **Users → Add user / Edit → User roles** and check every role the person needs. Use one account and one Google email per person, not duplicate accounts. Existing users retain their original role; migration `0011` adds optional extra role grants without changing prospect ownership or historical records.

After each login, a person with multiple assigned roles must choose one at `/choose-role/` before opening any application page or submitting a form. Single-role users go directly to their usual workspace. The selected role is shown beside their name; **Switch role** opens the chooser again. Selection is per browser session, not a global account setting. A safe internal destination from the sign-in flow is retained; that page still enforces the selected role's permissions.

Only the selected role's permissions apply; permissions are not combined. For example, an Inside sales + Client account executive user submits as Inside sales, then switches to Client account executive to work assigned discovery records. A Manager + Inside sales user cannot use manager access while Inside sales is selected. Source channels and personal attribution are recorded separately. Outreach records and handoffs are scoped to the selected source role. Executive assignment lists include any active person granted the executive role, regardless of what role that person is currently using.

Role changes are checked on every request. A removed active role cannot continue being used; if multiple roles remain, the chooser appears again. An administrator cannot remove their own system-admin grant or remove the last active administrator. Executive records must be reassigned before removing an executive grant. Counts on Users include each applicable role, so role totals may exceed the number of people.

New persistent API tokens are bound to the role selected when generated. Browser role switching does not change a token's permissions. Migration `0011` binds existing persistent tokens to their user's pre-migration role; removing a role revokes its tokens. Legacy short-lived JWTs and refresh-token exchange remain supported for single-role users; multi-role users must sign in, choose a role and generate a role-specific token. No API caller can select an unassigned role through a request header or payload.

Deploy migration `0011` before serving the new code, and collect static files. Do not reverse it after assigning multiple roles: that would discard extra grants and token-role bindings. Existing deployment workflows are unchanged.

## Historical records and identity

Migration `0010` is additive: it creates Escalation, Opportunity and CRMActivity, adds the executive role and permits blank website on simplified submissions. It neither deletes nor converts existing prospects/outreach. Existing company UUIDs are unchanged. A new lead with no website receives a unique system company identity; the app does not infer a website or merge unrelated companies. New submissions reject an existing same-channel domain (or same company name when no domain); use the existing prospect's handoff instead. Original source/contact fields remain on the linked prospect, so authorized corrections remain available in its original workflow or to managers.

After release, a system administrator opens Escalated leads → Review historical leads. Candidates include un-escalated non-LinkedIn records and LinkedIn records with a response/interest/meeting stage, interest context or escalation flag. Being listed is not qualification. Review the original record, correct missing contact details if needed, choose an executive and approve individually. Keep the escalation date blank when unknown. `created_at` records the migration approval time separately; it is not passed off as the historical escalation time. There is no mass conversion or automatic conversion of historical meetings into opportunities.

Records with CRM references cannot be deleted by import rollback. User deletion retains referenced accounts as disabled audit identities, as before. Reassign all CRM records before changing an executive's role. Deactivated accounts cannot sign in or use bearer tokens; administrators may still reassign their records.

## API contract

Use existing bearer tokens, JSON request bodies and `/api/v1/crm/` endpoints. Complete field lists, types, limits, status values and examples are in the authenticated `/api-access/crm/` guide. Legacy `/api/v1/prospects/`, company and outreach endpoints are unchanged. They do not implicitly create the new CRM entities.

| Method | Endpoint under `/api/v1/crm/` | Operation |
|---|---|---|
| GET / POST | `leads/` | List scoped leads / submit a new lead |
| POST | `prospects/<id>/escalate/` | Explicit handoff of an owned prospect |
| GET / PATCH | `leads/<id>/` | Read / update executive follow-up |
| POST | `leads/<id>/convert/` | Create the linked sales opportunity |
| POST | `leads/<id>/comments/` | Append a comment |
| GET | `leads/<id>/activity/` | Paginated activity and changes |
| GET | `opportunities/` | Shared sales pipeline |
| GET / PATCH | `opportunities/<id>/` | Read / update an opportunity |

PATCH requires the integer version from the latest GET, preserves omitted fields and returns 409 for a stale version. Repeat handoff/conversion returns the existing record, without duplication or reassignment. Comments are intentionally independent append operations. Lists return count/page/pages/results with 25 records per page. Dates are ISO dates; API meeting timestamps require a UTC offset plus an IANA discovery_timezone. Decimal values serialize as strings. Source fields and audit timestamps cannot be overwritten through CRM APIs. There are no delete APIs or message-sending side effects.

## Release checklist

1. Treat `operation-flexgcc/Leadgen_System:main` as the source of truth and commit subsequent changes directly to main. Use the existing Actions pipeline; creating feature branches or adding/changing workflows requires explicit approval. Do not deploy from old repository names or the obsolete local production remote.
2. Take and verify a restorable database backup before applying migrations. Confirm the production hosting target and its current release SHA through the normal release process.
3. Run tests, `manage.py check`, `manage.py makemigrations --check --dry-run`, production configuration checks, and PostgreSQL transactional tests in an approved test environment. SQLite unit tests alone do not exercise PostgreSQL row locking. This release's PostgreSQL tests passed before integration; the additional feature-branch CI job is intentionally not carried into main.
4. Deploy the reviewed commit through the configured production workflow; run `manage.py migrate --noinput` and `manage.py collectstatic --noinput`. Existing deployment scripts already perform these steps. No new secret, external integration, scheduled job or credential is required by this feature.
5. Verify `/health/` (now includes the new tables), Google sign-in, executive provisioning, source-role landing pages, and a controlled handoff/discovery/conversion. Check second-executive read-only access and manager reassignment before real use.
6. Review historical records individually only after deployment verification. The rollout does not alter historical company data automatically.

Do not reverse migration 0010 after CRM records have been entered: reversal drops the new tables and their audit history. For an application rollback, retain the additive schema and restore the previous code release while preserving a database backup. Do not treat deployment as verified until the live application and deployed SHA have been checked.
