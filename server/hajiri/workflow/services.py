"""
Public interface of the workflow app (approvals). Request modules import from here:

    from hajiri.workflow.services import (DecisionContext, register_handler, register_summariser, start_workflow,
                                          cancel_workflow, instance_for_object)

Engine (hajiri.workflow.service):
- register_handler(object_type, fn(object_id: str, state: 'approved'|'rejected', ctx: DecisionContext)) -> None.
  The handler runs inside the decision's transaction after the last approval or a rejection; ctx has actor_id,
  reason and payload (e.g. {'approvedMin': 90} from an OT approval). Raise an HttpError to refuse the decision.
- register_summariser(object_type, fn(instance: dict) -> dict) (alias register_summary): context for the inbox card
  of types this app does not know (leave, corrections, OT, payroll runs, letters and training are built in).
- register_card(object_type, fn(item) -> {'main': str|SafeString, 'extra': [str|SafeString]}) (hajiri.workflow.cards):
  how the Approvals page writes that context; expenses, travel, profile changes, timesheets, ... are built in.
- start_workflow(*, company_id, object_type, object_id, employee_id, title, body, amount=None) -> instance dict.
  Call inside your transaction; it picks the flow, notifies the first approvers and records routing notes.
- cancel_workflow(object_type, object_id): a pending approval ends as 'cancelled' (the request was withdrawn).
- get_instance(id), instance_for_object(object_type, object_id) -> dict | None (keys: id, company_id, object_type,
  object_id, employee_id, steps, current_step, state, created_at, completed_at, routing, amount, flow_id);
  instance_in(company_id, id) -> the same, only when it belongs to that company (another company's id is None).
  The rows are WorkflowInstance (hajiri.db.models, State choices); `instances()` is the base queryset.
- act(*, instance_id, user, decision, reason=None, on_behalf_of=None, payload=None) -> state (inside a transaction);
  decide(user, instance_id, 'approve'|'reject', reason=, on_behalf_of=, approved_min=) -> state (the route's logic:
  company scoping, own transaction, attendance recompute); bulk_approve(user, ids) -> [{'id','state'|'error'}].
- can_act(user, inst), can_view(user, inst), approvers_for_step(company_id, employee_id, step) -> [user_id],
  inbox_for(user), inbox_count(user), summarise(inst), summaries(insts) (many cards of one company in a few queries),
  timeline(inst), detail(user, id), pending_older_than(hours),
  signature_of(...), assert_manages(user, employee_id, area=None) (TS workflow message), npt_today() -> 'YYYY-MM-DD'.

Flows (hajiri.workflow.flows): REQUEST_TYPES, REQUEST_TYPE_SET, APPROVER_KINDS, STEP_ROLES, DEFAULT_STEPS,
choose_flow(company_id, object_type, employee_id) -> ChosenFlow(steps, flow_id, flow_name) (FlowChooser(company_id)
.choose(object_type, employee_id) for many at once), steps_for_amount,
resolve_step(company_id, employee_id, step, on_date) -> Resolution(users, notes), step_label, kind_of,
normalise_step, parse_steps, match_score, fell_back_to_hr, visible_notes, employee_users(employee_ids) (the accounts
acting as those employee records: home accounts and members of the group).

Group of companies: inbox_elsewhere(user) -> [{'companyId', 'name', 'count'}] counts what waits for the person in each
of their other companies (with their roles there; the Approvals page offers to switch), and as_member_of(user,
company_id) is the person as they are in another of their companies, for such read-only counts.

Approvers are people of the request's company: home accounts, members with access and group-role holders
(hajiri.core.users), so a group HR person decides HR steps in every company of the group. Delegations apply in the
company they were set in.

Set-up (hajiri.workflow.flow_admin, HR): flows_config, set_default, create_flow, update_flow, delete_flow,
create_group, update_group, delete_group, get_group, list_delegates, delegate_candidates, add_delegate, cancel_delegate,
active_delegation, preview(user, {objectType, employeeId?, amount?}), verify(user, {objectType?}).

Template component: {% approval_chain "leave_request" employee_id=emp.id amount=claim.total %} on any request form
(it loads `workflow:preview`). To refresh it after a field changes, trigger `hj:chain-<object_type>` on the body, or
put hx-get/hx-include on your inputs yourself: GET /_/workflow/preview?object_type=&employee_id=&amount=.
Inside a form, prefer the live version, which reads the form's amount and employee fields and reloads 300 ms after
they change (ApprovalChainPreview's debounce):
    {% load workflow_tags %}
    {% approval_chain_live "expense_claim" employee_id=emp.id amount=total amount_field="amount" employee_field="employee_id" %}

Legacy addresses /manager/approvals and /hr/requests redirect to /approvals and /leave/history.
"""
from .flow_admin import (active_delegation, add_delegate, cancel_delegate, create_flow, create_group, delegate_candidates,  # noqa: F401
                         delete_flow, delete_group, flows_config, get_flow, get_group, list_delegates, preview, set_default,
                         update_flow, update_group, verify)
from .flows import (APPROVER_KINDS, DEFAULT_STEPS, REQUEST_TYPE_BY_KEY, REQUEST_TYPE_SET, REQUEST_TYPES, STEP_ROLES,  # noqa: F401
                    ChosenFlow, FlowChooser, Resolution, choose_flow, employee_users, fell_back_to_hr, kind_of, match_score, normalise_step,
                    parse_steps, resolve_step, step_label, steps_for_amount, visible_notes)
from .service import (DecisionContext, DecisionHandler, Summariser, act, approvers_for, approvers_for_step,  # noqa: F401
                      as_member_of, assert_manages, bulk_approve, can_act, can_view, cancel_workflow, decide, detail,
                      get_instance, handler_for, inbox_count, inbox_elsewhere, inbox_for, instance_for_object, instance_in,
                      instances, npt_today, pending_older_than, register_handler, register_summariser, register_summary,
                      signature_of, start_workflow, summarise, summaries, timeline, unregister_handler)


def register_card(object_type, fn):
    """See hajiri.workflow.cards.register_card."""
    from .cards import register_card as _rc
    _rc(object_type, fn)
