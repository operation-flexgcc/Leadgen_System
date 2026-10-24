# Sales feedback update — 23 October 2026

## New lead entry

Every writable role (sales intern, inside sales, LinkedIn outreach, CAE, manager,
system admin) uses **Add lead** at `/crm/submit/`. Old `/prospects/new/` bookmarks
redirect to the same form. Existing prospect records and bulk-import/history
tools remain available; no historical records are rewritten.

Lead source is independent of user role: Personal contact, LinkedIn, Inbound,
Referral, Outbound, Event / networking, Other. Unknown historical sources are
not inferred. First contact is an optional actual/planned date, independent of
record creation. Legacy workstream is retained internally for existing APIs
and scope checks; it is not the lead-source input.

New submissions and non-historical handoffs require manager approval unless
entered using the Manager or System admin role. Multi-role users must choose
the Manager role to exercise manager permissions. Existing/historical handoffs
remain approved; no retrospective approval events or dates are invented.
Managers approve from the lead detail page. Pending leads are visible but
cannot be updated by a CAE or converted to opportunities. Approval is recorded
with actor/time and optimistic version checks.

## Follow-up

The outreach dashboard, lead queue and opportunity list can filter and sort by
salesperson and next-step owner, ascending or descending. Salesperson means
the original outreach owner, falling back to the record creator. Next-step
owner can be explicitly selected; otherwise the CAE owns CRM follow-up and the
outreach owner owns pre-handoff follow-up. The source owner is not reassigned.

**Mark dead** on a lead detail page takes an optional reason, clears active
follow-up and preserves the lead and audit history. Converted leads must be
closed through their opportunity. Dead leads can be reopened through the
existing status editor. Underlying API status `dropped` is retained for
backward compatibility.

The missing-owner template error is fixed for both lead and opportunity pages,
including direct manager submissions with no outreach owner.

## Safe live verification

Use normal, pre-provisioned Google sign-in and existing role selection. No
authentication bypass, hard-coded credentials or testing-only login is added.
For synthetic verification, check **Synthetic test record** at creation, use an
obvious unique sample name and example.com contact details, and do not record
real outreach against it. The flag cannot be applied to an existing customer.

After testing, a system admin opens **Remove synthetic record**, types the
exact company name and confirms removal. Only explicitly tagged samples can
be removed. The action deletes their CRM activity, sample opportunity and
prospect, plus an orphaned company identity; it does not delete users or real
records. Samples linked to outreach/import history are rejected. Delete any
test record before handoff and confirm it is absent from the live search.
