# External REST approval integration

Use this when another system (e.g. an iFlow transport pipeline) wants to *create*
an approval here, have approvers review all the relevant fields, and get a
*callback* with the result when it's decided — the same role Power Automate played.

```
your system ──POST payload──▶  /api/v1/external/approvals/   (creates the approval)
                                       │
                          approvers review & decide
                          (token email link / app / Teams)
                                       │
   your result API  ◀──POST result───  on completion (Result_Callback_URL)
```

## 1. Create an approval (inbound)

```
POST /api/v1/external/approvals/
Header: X-Approval-Secret: <EXTERNAL_APPROVAL_SECRET>
Body (your payload — "body" wrapper optional):
{
  "approvers": [
    {"email": "validator@acme.com", "title": "Validation Approval for Iflow: BT120P0"},
    {"email": "customer@acme.com",  "title": "Customer Approval for Iflow: BT120P0"}
  ],
  "Workflow_Name": "CF_BT120Q0_to_CF_BT120P0_20260518_155414",
  "Source_Tenant": "BT120Q0",
  "Destination_Tenant": "BT120P0",
  "Package_Id": "rb.example.Boost.SalesProjectMgmt",
  "iFlow_Id":  "rb.example.Boost.SalesProjectMgmt.OneQ_SPJ_Project_Req.SalesForce",
  "Version": "1.0.0",
  "Validation_Status": "true",
  "Details": "From: BT120Q0\nTo: BT120P0\n...",
  "title": "Request for iFlow transport BT120P0",
  "priority": "High",
  "Result_Callback_URL": "https://your-system/iflow/approval-result"
}
```

What happens:
- Each approver becomes a **stage in order** (named by its `title`) — e.g. *Validation
  Approval* must clear before *Customer Approval*.
- All the metadata (`Source_Tenant`, `Destination_Tenant`, `Package_Id`, `iFlow_Id`,
  `Version`, `Validation_Status`, `Workflow_Name`, …) is shown on the **console** and in
  each approver's **email**, and `Details` becomes the description.
- Each approver email contains a one-click **token link** to approve/reject (no login).
  They can equally act in the app or in Teams (if `route_to_teams` is on).
- Response: `{"external_id": .., "request_id": .., "status": "under_review"}`.

## 2. Result callback (outbound)

When the last stage is decided, we **POST your `Result_Callback_URL`** with:

```json
{
  "responses": [
    {"responder": {"id": "..", "displayName": "..", "email": "..", "userPrincipalName": ".."},
     "stage": "Validation Approval for Iflow: BT120P0",
     "requestDate": "...", "responseDate": "...", "approverResponse": "Approve"}
  ],
  "responseSummary": "All approvers approved the request.",
  "completionDate": "...",
  "outcome": "Approved",
  "name": "CF_BT120Q0_to_CF_BT120P0_20260518_155414",
  "title": "Request for iFlow transport BT120P0",
  "requestDate": "...",
  "expirationDate": "...",
  "priority": "High",
  "Workflow_Name": "...", "iFlow_Id": "...", "Version": "1.0.0"
}
```

That matches the response shape your API already consumes. The POST is best-effort
and logged; it does not block the decision.

## Where this replaces / complements Power Automate

- **Replace it:** point your pipeline at `/api/v1/external/approvals/` instead of the
  Flow. Approvers act via the token email link / app, and you get the result callback.
- **Keep Teams:** set `route_to_teams=True` (see TEAMS.md) and the same approval also
  appears in the Teams Approvals app; whoever acts first decides.

## Notes / caveats

- Approver emails are resolved to users; unknown emails are **auto-provisioned** as
  login-disabled accounts so a task can be assigned and the email/token link sent.
  Apply the same guardrails as the public form for untrusted input.
- The inbound endpoint is protected by the shared `X-Approval-Secret`
  (`TEAMS_APPROVAL_CALLBACK_SECRET`). Serve over HTTPS.
- Stages are **sequential** (one approver per stage, in array order). If you want all
  approvers in parallel instead, that's a one-line change in the adapter.
- The result callback currently POSTs synchronously from the decision's commit hook;
  for high volume, move it onto a Celery task (same pattern as the Teams push).

## Gate the decision on your callback (optional)

Set `"require_callback_ack": true` in the inbound payload. Then the decision is only
**confirmed** once your `Result_Callback_URL` returns **HTTP 2xx**:

- `ExternalApproval.callback_acked` = did the callback return 2xx
- `ExternalApproval.is_confirmed` = `(not require_callback_ack) or callback_acked`
- The posted document includes `"confirmationRequired": true` so your side knows its
  2xx is what confirms it.

If the callback does **not** return 2xx, the approvers' `outcome` is still recorded
(Approved/Rejected) but `is_confirmed` stays false and a WARNING is logged
(`decision NOT CONFIRMED — callback did not return 2xx`). Re-post / retry until it
acks. When `require_callback_ack` is false (default), the callback is fire-and-forget
and the decision is confirmed immediately.

## Any fields, any flow (dynamic metadata)

You are NOT limited to the iFlow fields. The approver view, console, and the result
callback show **whatever fields you send** in the payload — so a different external
flow with the same `approvers` shape but different metadata just works.

- **Display order:** known iFlow fields (`Workflow_Name`, `Source_Tenant`, …) appear
  first when present; any other fields you send appear after them, in payload order.
- **Hidden control keys** (never shown as data): `approvers`, `Result_Callback_URL`,
  `Details`, `title`, `name`, `priority`, `require_callback_ack` (+ casings),
  `Requester_Email`, `notify_email`, `body`. `Details` is shown as the description.
- **Callback echo:** the result document echoes back every metadata field with its
  ORIGINAL key (e.g. `"Project_Name": "Apollo"`), alongside the fixed result fields
  (`responses`, `outcome`, `responseSummary`, `completionDate`, `title`, `priority`, …).

Example — a non-iFlow flow:

```json
{
  "approvers": [{"email": "lead@acme.com", "title": "Lead Approval"}],
  "Project_Name": "Apollo",
  "Cost_Center": "CC-4471",
  "Environment": "Production",
  "Details": "Access request for Apollo",
  "title": "Apollo access",
  "Result_Callback_URL": "https://your-system/result"
}
```

…shows Project Name / Cost Center / Environment to the approver, and the callback
includes `"Project_Name"`, `"Cost_Center"`, `"Environment"` back to you.
