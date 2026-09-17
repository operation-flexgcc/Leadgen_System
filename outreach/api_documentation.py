import json

from .models import Outreach, Prospect


def _choice_values(choices):
    return [
        {"value": value, "label": label}
        for value, label in choices
    ]


def _json_example(payload):
    return json.dumps(payload, indent=2)


API_DOCUMENTATION_ORDER = (
    "prospect-sent",
    "founder-linkedin",
    "interest-handoff",
    "follow-up",
    "outreach-history-list-create",
    "outreach-history-detail",
)


API_DOCUMENTATION = {
    "prospect-sent": {
        "slug": "prospect-sent",
        "nav_label": "Prospect sent",
        "title": "Prospect sent API",
        "summary": "Mark whether a prospect has been sent for the next stage of processing.",
        "endpoint": "/api/v1/prospects/<prospect_id>/prospect-sent/",
        "request_example": _json_example({"prospect_sent": True}),
        "curl_example": """curl --request PATCH \\
  --url 'https://leadgen.flexgcc.com/api/v1/prospects/123/prospect-sent/' \\
  --header 'Authorization: Bearer <access_token>' \\
  --header 'Content-Type: application/json' \\
  --data '{"prospect_sent":true}'""",
        "fields": [
            {
                "name": "prospect_sent",
                "type": "boolean",
                "required": "Optional field; at least one field is required per PATCH.",
                "description": "Use true when the prospect has been sent. Use false only to correct an earlier mark.",
                "example": "true",
                "choices": [],
            },
        ],
        "rules": [
            "Use a JSON boolean, not the strings \"true\" or \"false\".",
            "GET returns the current prospect_sent value without changing it.",
        ],
    },
    "founder-linkedin": {
        "slug": "founder-linkedin",
        "nav_label": "Founder LinkedIn",
        "title": "Founder LinkedIn account API",
        "summary": "Update the founder account, invitation state, personalization, and escalation requirement for a LinkedIn-outreach prospect.",
        "endpoint": "/api/v1/prospects/<prospect_id>/founder-linkedin/",
        "request_example": _json_example(
            {
                "linkedin_connection_status": "accepted",
                "founder_escalation_required": True,
                "founder_escalation_notes": "Prospect asked for founder input on delivery model.",
            }
        ),
        "curl_example": """curl --request PATCH \\
  --url 'https://leadgen.flexgcc.com/api/v1/prospects/123/founder-linkedin/' \\
  --header 'Authorization: Bearer <access_token>' \\
  --header 'Content-Type: application/json' \\
  --data '{"linkedin_connection_status":"accepted","founder_escalation_required":true,"founder_escalation_notes":"Prospect asked for founder input on delivery model."}'""",
        "fields": [
            {
                "name": "founder_account",
                "type": "string enum",
                "required": "Optional in PATCH; required by the completed LinkedIn prospect workflow.",
                "description": "The single founder LinkedIn account assigned to this prospect. Send an empty string to clear it only while correcting an incomplete record.",
                "example": '"kandarp_soni"',
                "choices": _choice_values(Prospect.FounderAccount.choices),
            },
            {
                "name": "linkedin_connection_status",
                "type": "string enum",
                "required": "Optional in PATCH; required by the completed LinkedIn prospect workflow.",
                "description": "The current invitation or connection state.",
                "example": '"accepted"',
                "choices": _choice_values(Prospect.LinkedInConnectionStatus.choices),
            },
            {
                "name": "personalization_note",
                "type": "string, maximum 500 characters",
                "required": "Optional in PATCH; required by the completed LinkedIn prospect workflow.",
                "description": "A factual personalization reference used for the invitation or message.",
                "example": '"Chicago healthcare operations practice."',
                "choices": [],
            },
            {
                "name": "founder_escalation_required",
                "type": "boolean",
                "required": "Optional field; use true only when founder input is required.",
                "description": "Marks the prospect for founder attention after the invitation is accepted and a response or interest signal is recorded.",
                "example": "true",
                "choices": [],
            },
            {
                "name": "founder_escalation_notes",
                "type": "string, maximum 5,000 characters",
                "required": "Optional, but recommended when founder_escalation_required is true.",
                "description": "The exact question, objection, or response the founder should address. Send an empty string to clear it.",
                "example": '"Prospect asked whether FlexGCC can support a phased transition."',
                "choices": [],
            },
        ],
        "rules": [
            "This API is available only for LinkedIn outreach prospects.",
            "Setting founder_escalation_required to true requires linkedin_connection_status=accepted.",
            "A response must already be evidenced by an outreach response, a recorded interest signal, or a response-or-later playbook stage.",
            "GET returns only the Founder LinkedIn fields and prospect identity.",
        ],
    },
    "interest-handoff": {
        "slug": "interest-handoff",
        "nav_label": "Interest and handoff",
        "title": "Interest and handoff API",
        "summary": "Record material shared, the prospect's explicit interest signal, and questions that need founder attention.",
        "endpoint": "/api/v1/prospects/<prospect_id>/interest-handoff/",
        "request_example": _json_example(
            {
                "material_shared": "one_page_and_deck",
                "interest_signal": "Asked for a discussion about building a GCC delivery team.",
                "questions_for_founders": "Can FlexGCC start with a five-person pilot?",
            }
        ),
        "curl_example": """curl --request PATCH \\
  --url 'https://leadgen.flexgcc.com/api/v1/prospects/123/interest-handoff/' \\
  --header 'Authorization: Bearer <access_token>' \\
  --header 'Content-Type: application/json' \\
  --data '{"material_shared":"one_page_and_deck","interest_signal":"Asked for a GCC delivery-team discussion.","questions_for_founders":"Can FlexGCC start with a five-person pilot?"}'""",
        "fields": [
            {
                "name": "material_shared",
                "type": "string enum",
                "required": "Optional field; at least one field is required per PATCH.",
                "description": "The approved material already shared with the prospect. Send an empty string when nothing has been shared.",
                "example": '"one_page_and_deck"',
                "choices": _choice_values(Prospect.MaterialShared.choices),
            },
            {
                "name": "interest_signal",
                "type": "string, maximum 5,000 characters",
                "required": "Optional field; at least one field is required per PATCH.",
                "description": "The exact reply or a concise factual summary showing the prospect's interest. Send an empty string to clear it.",
                "example": '"Asked for a call about a GCC delivery team."',
                "choices": [],
            },
            {
                "name": "questions_for_founders",
                "type": "string, maximum 5,000 characters",
                "required": "Optional field; at least one field is required per PATCH.",
                "description": "Only questions explicitly raised by the prospect. Send an empty string when there are none.",
                "example": '"Can FlexGCC support a five-person pilot?"',
                "choices": [],
            },
        ],
        "rules": [
            "Send only fields that should change; omitted fields retain their current values.",
            "GET returns only Interest and handoff fields and prospect identity.",
        ],
    },
    "follow-up": {
        "slug": "follow-up",
        "nav_label": "Follow-up",
        "title": "Follow-up API",
        "summary": "Update outcome status, next action, timing, comments, and conditional meeting details.",
        "endpoint": "/api/v1/prospects/<prospect_id>/follow-up/",
        "request_example": _json_example(
            {
                "status": "meeting_to_be_scheduled",
                "next_action": "Send three meeting slots",
                "next_action_date": "2026-09-15",
                "comments": "Prospect prefers morning US Central time.",
            }
        ),
        "curl_example": """curl --request PATCH \\
  --url 'https://leadgen.flexgcc.com/api/v1/prospects/123/follow-up/' \\
  --header 'Authorization: Bearer <access_token>' \\
  --header 'Content-Type: application/json' \\
  --data '{"status":"meeting_to_be_scheduled","next_action":"Send three meeting slots","next_action_date":"2026-09-15","comments":"Prospect prefers morning US Central time."}'""",
        "fields": [
            {
                "name": "status",
                "type": "string enum",
                "required": "Optional field; at least one field is required per PATCH.",
                "description": "The current outcome status. Status changes also advance the mapped playbook stage where applicable.",
                "example": '"meeting_to_be_scheduled"',
                "choices": _choice_values(Prospect.Status.choices),
            },
            {
                "name": "next_action",
                "type": "string, maximum 250 characters",
                "required": "Required whenever next_action_date has a value.",
                "description": "One concrete next action. Clear it together with next_action_date by sending an empty string and null.",
                "example": '"Send three meeting slots"',
                "choices": [],
            },
            {
                "name": "next_action_date",
                "type": "ISO date string YYYY-MM-DD or null",
                "required": "Required whenever next_action has a value.",
                "description": "The calendar date for the next action. Use null to clear it.",
                "example": '"2026-09-15"',
                "choices": [],
            },
            {
                "name": "comments",
                "type": "string, maximum 5,000 characters",
                "required": "Optional field; at least one field is required per PATCH.",
                "description": "Internal factual context for the follow-up. Send an empty string to clear it.",
                "example": '"Prospect prefers morning US Central time."',
                "choices": [],
            },
            {
                "name": "meeting_scheduled_at",
                "type": "ISO 8601 date-time with UTC offset or null",
                "required": "Required when status is meeting_scheduled.",
                "description": "The confirmed meeting date and time. Include an offset, for example -04:00 or Z. Use null to clear it.",
                "example": '"2026-09-18T10:30:00-04:00"',
                "choices": [],
            },
            {
                "name": "meeting_timezone",
                "type": "string, maximum 80 characters",
                "required": "Required when status is meeting_scheduled.",
                "description": "A clear IANA zone or business-zone label for the confirmed meeting.",
                "example": '"America/Chicago"',
                "choices": [],
            },
            {
                "name": "meeting_participants",
                "type": "string, maximum 500 characters",
                "required": "Required when status is meeting_scheduled.",
                "description": "Names of the prospect, founder, and other confirmed attendees.",
                "example": '"Asha Rao, Kandarp Soni, Meera Shah"',
                "choices": [],
            },
        ],
        "rules": [
            "next_action and next_action_date must either both have values or both be cleared.",
            "meeting_scheduled_at, meeting_timezone, and meeting_participants are mandatory when status is meeting_scheduled.",
            "A meeting date-time without a UTC offset is rejected as ambiguous.",
            "GET returns only Follow-up fields and prospect identity.",
        ],
    },
    "outreach-history-list-create": {
        "slug": "outreach-history-list-create",
        "nav_label": "Outreach list/create",
        "title": "Outreach history list and create API",
        "summary": "List a prospect's chronological outreach history or record the next outreach entry.",
        "method_label": "GET · POST",
        "methods": "GET, POST",
        "contract_summary": "GET lists all outreach records for one prospect. POST validates and creates the next numbered record.",
        "authorization": "Managers and system administrators can access every prospect. A frontline outreach user must own the exact prospect; an unassigned or another user's prospect returns HTTP 403.",
        "behavior": "POST uses the same workstream, contact-method, LinkedIn sequence, date, and five-record limit rules as the browser form. The server assigns sequence_number and recorded_by.",
        "endpoint": "/api/v1/prospects/<prospect_id>/outreaches/",
        "request_example": _json_example(
            {
                "activity_type": "follow_up",
                "medium": "email",
                "outreach_date": "2026-08-28",
                "response": "Asked for the one-page overview.",
            }
        ),
        "curl_example": """curl --request POST \\
  --url 'https://leadgen.flexgcc.com/api/v1/prospects/123/outreaches/' \\
  --header 'Authorization: Bearer <access_token>' \\
  --header 'Content-Type: application/json' \\
  --data '{"activity_type":"follow_up","medium":"email","outreach_date":"2026-08-28","response":"Asked for the one-page overview."}'""",
        "read_curl_example": """curl --request GET \\
  --url 'https://leadgen.flexgcc.com/api/v1/prospects/123/outreaches/' \\
  --header 'Authorization: Bearer <access_token>'""",
        "replacement_hint": "Replace the prospect ID and access token before running.",
        "fields": [
            {
                "name": "activity_type",
                "type": "string enum",
                "required": "Required for POST.",
                "description": "The outreach event. Standard and LinkedIn workstreams expose different subsets; unsupported values for the prospect's workstream are rejected.",
                "example": '"follow_up"',
                "choices": _choice_values(Outreach.ActivityType.choices),
            },
            {
                "name": "medium",
                "type": "string enum",
                "required": "Required for POST.",
                "description": "The channel used. The corresponding email, phone number, or LinkedIn profile must exist on the prospect. LinkedIn-outreach prospects must use linkedin.",
                "example": '"email"',
                "choices": _choice_values(Outreach.Medium.choices),
            },
            {
                "name": "outreach_date",
                "type": "ISO date string YYYY-MM-DD",
                "required": "Required for POST.",
                "description": "The calendar date when the outreach occurred. Future dates are rejected.",
                "example": '"2026-08-28"',
                "choices": [],
            },
            {
                "name": "response",
                "type": "string or null",
                "required": "Optional for POST.",
                "description": "The prospect's response or concise factual notes. Send an empty string or null when no response was received.",
                "example": '"Asked for the one-page overview."',
                "choices": [],
            },
        ],
        "rules": [
            "A prospect can have no more than five outreach records; the API returns HTTP 409 at the limit.",
            "sequence_number is read-only and automatically fills the first available number from 1 through 5.",
            "recorded_by is read-only and is set from the bearer token user.",
            "Phone, email, and LinkedIn outreach require the matching contact detail on the prospect.",
            "LinkedIn connection and post-acceptance activities must follow the prospect's current connection state.",
            "GET returns records ordered by sequence_number and includes display labels, recorder identity, and timestamps.",
            "Outreach history cannot be deleted through the API.",
        ],
        "responses": [
            {"status": "200", "meaning": "GET succeeded and returned the outreach list."},
            {"status": "201", "meaning": "POST validated and created the next outreach record."},
            {"status": "400", "meaning": "Malformed JSON, invalid input, or a workflow rule failed."},
            {"status": "401", "meaning": "The bearer token is missing, invalid, revoked, or belongs to an inactive user."},
            {"status": "403", "meaning": "The token user does not own this prospect and is not a manager or system administrator."},
            {"status": "404", "meaning": "The numeric prospect ID does not exist."},
            {"status": "409", "meaning": "Five records already exist or another create operation won the sequence-number race."},
            {"status": "405", "meaning": "The endpoint received a method other than GET or POST."},
        ],
    },
    "outreach-history-detail": {
        "slug": "outreach-history-detail",
        "nav_label": "Outreach detail",
        "title": "Outreach history detail API",
        "summary": "Retrieve one outreach record or correct its editable history fields without changing its identity or sequence.",
        "method_label": "GET · PATCH",
        "methods": "GET, PATCH",
        "contract_summary": "GET retrieves one outreach record. PATCH updates only supplied editable fields and preserves omitted values.",
        "authorization": "Managers and system administrators can access every outreach record. A frontline outreach user must own the exact parent prospect; otherwise the API returns HTTP 403.",
        "behavior": "PATCH preserves outreach ID, prospect, sequence_number, and recorded_by. Every successful update records the authenticated modifier, previous values, changed values, and timestamp in an immutable API audit.",
        "endpoint": "/api/v1/outreaches/<outreach_id>/",
        "request_example": _json_example(
            {
                "outreach_date": "2026-08-27",
                "response": "Corrected note: requested a short deck.",
            }
        ),
        "curl_example": """curl --request PATCH \\
  --url 'https://leadgen.flexgcc.com/api/v1/outreaches/456/' \\
  --header 'Authorization: Bearer <access_token>' \\
  --header 'Content-Type: application/json' \\
  --data '{"outreach_date":"2026-08-27","response":"Corrected note: requested a short deck."}'""",
        "read_curl_example": """curl --request GET \\
  --url 'https://leadgen.flexgcc.com/api/v1/outreaches/456/' \\
  --header 'Authorization: Bearer <access_token>'""",
        "replacement_hint": "Replace the outreach ID and access token before running.",
        "fields": [
            {
                "name": "activity_type",
                "type": "string enum",
                "required": "Optional in PATCH; at least one field is required.",
                "description": "Correct the recorded activity type. The value must be available to the parent prospect's workstream.",
                "example": '"follow_up"',
                "choices": _choice_values(Outreach.ActivityType.choices),
            },
            {
                "name": "medium",
                "type": "string enum",
                "required": "Optional in PATCH; at least one field is required.",
                "description": "Correct the channel. The parent prospect must contain the matching contact detail; LinkedIn-outreach prospects must remain linkedin.",
                "example": '"email"',
                "choices": _choice_values(Outreach.Medium.choices),
            },
            {
                "name": "outreach_date",
                "type": "ISO date string YYYY-MM-DD",
                "required": "Optional in PATCH; at least one field is required.",
                "description": "Correct the historical outreach date. Future dates are rejected.",
                "example": '"2026-08-27"',
                "choices": [],
            },
            {
                "name": "response",
                "type": "string or null",
                "required": "Optional in PATCH; at least one field is required.",
                "description": "Correct the response or factual note. Send an empty string or null to clear it.",
                "example": '"Corrected note: requested a short deck."',
                "choices": [],
            },
        ],
        "rules": [
            "Send only fields that should change; omitted fields retain their current values.",
            "id, prospect_id, sequence_number, recorded_by, created_at, and updated_at are response fields and cannot be supplied in PATCH.",
            "Changing medium still requires the matching contact detail on the parent prospect.",
            "LinkedIn-outreach records must keep medium=linkedin.",
            "GET includes executable enum values and human-readable display labels in the response.",
            "Outreach history cannot be deleted through the API.",
        ],
        "responses": [
            {"status": "200", "meaning": "GET succeeded or PATCH was validated, saved, and audited."},
            {"status": "400", "meaning": "Malformed JSON, unsupported field, invalid type/value, or a workflow validation rule failed."},
            {"status": "401", "meaning": "The bearer token is missing, invalid, revoked, or belongs to an inactive user."},
            {"status": "403", "meaning": "The token user does not own the parent prospect and is not a manager or system administrator."},
            {"status": "404", "meaning": "The numeric outreach ID does not exist."},
            {"status": "405", "meaning": "The endpoint received a method other than GET or PATCH."},
        ],
    },
}
