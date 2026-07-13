#!/usr/bin/env python3
"""
endpoints.py
============
Single source of truth for every Ardhisasa API endpoint path used across
this bot. Before this existed, ~18 distinct endpoint paths were each
hardcoded as an f-string in 2-6 different feature modules — if the API
ever renames a path or moves to a new service prefix, every call site had
to be found and edited individually. Now there is exactly one constant per
endpoint; every feature module imports the constant it needs instead of
rebuilding the URL itself.

Each constant below is documented with the HTTP method(s) actually used
against it in this codebase, the request query params / JSON body, and the
response fields this codebase actually reads — not the full API contract,
just what matters here. Where a single path is hit with more than one
shape (see `STAMP_DUTY_FIX_APPLICATION_URL`), both are documented rather
than picking a "correct" one, since this file only centralizes paths and
must not silently change request behavior.

`ardhisasa_auth.py`'s own `login()`/`verify_otp()` are the one exception:
they keep building `f"{AUTH_BASE_URL}/login"`/`f"{AUTH_BASE_URL}/otpverify"`
inline rather than importing from here, since `ardhisasa_auth.py` is the
lowest-level foundational module (predates this modularization and is
copied independently into other script trees) and this file already
imports `AUTH_BASE_URL` from it — importing back would be circular. If
the login/OTP path ever changes, update both `ardhisasa_auth.py` and the
two constants below.
"""

from ardhisasa_auth import AUTH_BASE_URL
from common import BASE_URL

# ──────────────────────────────────────────────────────────
# Auth (AUTH_BASE_URL = "https://ardhisasa-api.lands.go.ke/acl/api/v1/auth")
# ──────────────────────────────────────────────────────────

# POST — body: {"username", "password", "usertype", "otpcode": ""}
# Triggers OTP dispatch to the registered device. Response: {"success": bool,
# "error"/"message" on failure}. Used by fetch_tasks.py, new_assignment.py,
# receive_tasks.py, refresh_auth.py (ardhisasa_auth.py builds this inline —
# see module docstring above).
AUTH_LOGIN_URL = f"{AUTH_BASE_URL}/login"

# POST — body: {"username", "password", "otpcode"}
# Response: {"details": {"access_token", "jwt", "refresh_token"}}.
# Used by fetch_tasks.py, new_assignment.py, receive_tasks.py, refresh_auth.py
# (ardhisasa_auth.py builds this inline — see module docstring above).
AUTH_OTP_VERIFY_URL = f"{AUTH_BASE_URL}/otpverify"

# POST — headers: current (possibly-about-to-expire) Authorization/JWTAUTH.
# Body: {"refresh_token": "..."} if available, else {} (server still issues
# new tokens while the current ones remain valid). Response:
# {"details": {"access_token", "jwt", "refresh_token"}} (or top-level, some
# responses omit the "details" wrapper — token_refresh_daemon.py checks both).
# Used only by token_refresh_daemon.py, called ~10 min before expiry.
AUTH_REFRESH_TOKEN_URL = f"{AUTH_BASE_URL}/refresh-token"


# ──────────────────────────────────────────────────────────
# Accounts & Teams (acl service)
# ──────────────────────────────────────────────────────────

# GET — params: {"account_type": "STAFF", "filter_type": "ACTIVE", "page",
# "search": <name>}. Response: {"results": [{"id", "account_number",
# "staff_details": {"firstname", "middlename", "lastname", "user_id",
# "department_details", "roles", "county_units", ...}}]}. Only summary
# fields — full staff detail (department/roles/county_units) requires a
# follow-up call to one of the ACCOUNTS_*_DETAIL_URL variants below.
# Used by dlv_batch.py, new_assignment.py, receive_tasks.py (x3),
# sectional_properties.py, valuer_tasks.py.
ACCOUNTS_LIST_URL = f"{BASE_URL}/acl/api/v1/accounts/list-user-accounts"

# GET — full staff detail (department_details/roles/ardhipay_roles/
# county_units), used only by receive_tasks.py's `_fetch_staff_detail`,
# which probes several undocumented URL shapes in order since the correct
# one isn't fixed across accounts: `?user_id={id}` (query) or `/{id}`
# (path segment) against this same base, i.e.
#   f"{ACCOUNTS_GET_USER_DETAIL_URL}?user_id={user_id}"
#   f"{ACCOUNTS_GET_USER_DETAIL_URL}/{user_id}"
ACCOUNTS_GET_USER_DETAIL_URL = f"{BASE_URL}/acl/api/v1/accounts/get-user-detail"

# GET — params: {"user_id"}. Another candidate in the same staff-detail probe.
ACCOUNTS_USER_DETAILS_URL = f"{BASE_URL}/acl/api/v1/accounts/user-details"

# GET — params: {"user_id"}. Another candidate in the same staff-detail probe.
ACCOUNTS_VIEW_USER_URL = f"{BASE_URL}/acl/api/v1/accounts/view-user"

# GET — account_id appended as a path segment: f"{ACCOUNTS_BY_ID_URL}/{account_id}".
# Last-resort candidate in the same staff-detail probe (falls back to the
# original list-user-accounts entry if every candidate fails).
ACCOUNTS_BY_ID_URL = f"{BASE_URL}/acl/api/v1/accounts"

# GET — params: {"page", "search": ""}. Response: {"results": [{"id",
# "team_name", "min_amount", "max_amount"}]}. Used only by job_distribution.py.
TEAMS_LIST_URL = f"{BASE_URL}/acl/api/v1/list-teams"

# GET — params: {"team_id", "page", "search": ""}, paginated via "next".
# Response: {"results": [{"userid", "name", "account_number", "availability",
# "registry"}], "next"}. Used only by job_distribution.py.
TEAM_MEMBERS_URL = f"{BASE_URL}/acl/api/v1/staff-teams/get-team-members"


# ──────────────────────────────────────────────────────────
# Stamp Duty Valuation Service (valuationservice — the DLV/valuer-facing side)
# ──────────────────────────────────────────────────────────

# GET — params vary by caller but always include {"filter", "role": "DLV",
# "request_type": "STAMP_DUTY" or "COUNTY_STAMP_DUTY", "search", "page"};
# County variants add {"from_ardhipay": "true"}. Response: {"count",
# "results": [{"id", "reference_number", "parcel_number", "registry",
# "county", "application_status", "node", "date_created"}], "next"}.
# Used by dlv_core.py, job_distribution.py, lookup_reference.py,
# receive_tasks.py (x2), valuer_tasks.py, bulk_export.py (filter="Completed").
STAMP_DUTY_APPLICATION_LIST_URL = f"{BASE_URL}/valuationservice/api/v1/stamp-duty/application"

# GET — params: {"request_id": <id>}. Response: {"reference_number",
# "parcel_number", "registry", "county", "application_status", "node",
# "date_created", "actors": [{"role", "user_details": {"names", "id"},
# "date_assigned"}], "external_process_details": {"consideration_amount",
# "currency_code"}, "process_documents": [...], "application_documents": [...]}.
# Used by dlv_core.py, job_distribution.py, lookup_reference.py,
# receive_tasks.py, valuer_tasks.py, bulk_export.py.
STAMP_DUTY_APPLICATION_DETAIL_URL = f"{BASE_URL}/valuationservice/api/v1/stamp-duty/application/detail-view"

# Assign a valuer to a reference. Two distinct request shapes are used
# against this same URL in this codebase — both are kept as-is (not
# unified) since they were observed working independently and this file
# only centralizes the path, not the request contract:
#   - POST body: {"reference_number", "valuation_officer", "node":
#     "VALUATION_STAMP_DUTY_VALUER_REPORT"} — used by new_assignment.py,
#     receive_tasks.py, dlv_batch.py.
#   - PUT  body: {"request_id", "valuation_officer"} (no "node"), with
#     cparams=CPARAMS_VALUER_ROLE — used by auto_fetch.py's sectional
#     auto-assign path only.
STAMP_DUTY_FIX_APPLICATION_URL = f"{BASE_URL}/valuationservice/api/v1/stamp-duty/fix_application_details"

# GET — params: {"request_id": <id>}. Response: {"combined_document":
# [{"document": <url>}]} (non-critical — bulk_export.py swallows errors and
# treats a failure here as "no combined report", not a hard failure).
# Used only by bulk_export.py.
STAMP_DUTY_OFFICE_REPORT_URL = f"{BASE_URL}/valuationservice/api/v1/stamp-duty/office-reports/get-current-office-report"


# ──────────────────────────────────────────────────────────
# Assessor / HQ stage (stampdutyservice) & county transfer (registrationservice)
# ──────────────────────────────────────────────────────────
# These cover the pre-DLV stage — a task lives here before an assessor
# assigns it into DLV's queue.

# GET — params: {"filter": "Ongoing", "page", "search"}; county variant
# adds {"from_ardhipay": "true"}. Response: {"results": [{"id",
# "reference_number", "parcel_number", "registry", "county", "date_created"}]}.
# Used by dlv_core.py (assessor-stage fallback search) and fetch_tasks.py
# (HQ candidate list, both digitised and from_ardhipay variants).
ASSESSOR_STAGE_LIST_URL = f"{BASE_URL}/stampdutyservice/api/v1/stamp-duty/hod-or-clr"

# GET — params: {"request_id": <id>}. Response: {"stamp_duty_status",
# "application_status", "invoices": [...], "officers": [{"names", "role"}],
# "county", "registry"}. Used by dlv_core.py and fetch_tasks.py (both the
# HQ 2b detail call and the county detail call use this same path).
ASSESSOR_STAGE_DETAIL_URL = f"{BASE_URL}/stampdutyservice/api/v1/stamp-duty/detail-view"

# GET — params: {"request_id": <id>}. Response: registration-side detail
# (used to cross-reference an HQ task's registration record before it has
# a stamp-duty invoice). Used only by fetch_tasks.py's HQ 2a detail call.
COUNTY_TRANSFER_DETAIL_URL = f"{BASE_URL}/registrationservice/api/v1/transfer/transfer-request-staff-detailed-view"
