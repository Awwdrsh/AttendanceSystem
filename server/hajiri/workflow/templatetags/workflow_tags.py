"""
Template tags for request forms in other apps.

    {% load workflow_tags %}
    {% approval_chain_live "expense_claim" employee_id=emp.id amount=total amount_field="amount" employee_field="employee_id" %}

"Who will approve this" (ApprovalChainPreview.tsx) that follows the form it sits in: it loads on render and reloads
300 ms after the form's amount or employee field changes (or on the body event hj:chain-<object_type>), reading the
current values from the form. Fields that are missing fall back to the values given to the tag. Renders nothing
when the chain cannot be worked out. The static `{% approval_chain %}` tag from hajiri.web works too, but never
reads the form.
"""
from django import template
from django.urls import reverse

register = template.Library()


@register.inclusion_tag('workflow/partials/chain_live.html')
def approval_chain_live(object_type, employee_id=None, amount=None, amount_field='amount', employee_field='employee_id',
                        target_id=None):
    return {'url': reverse('workflow:preview'), 'object_type': object_type, 'employee_id': employee_id or '',
            'amount': '' if amount is None else amount, 'amount_field': amount_field, 'employee_field': employee_field,
            'id': target_id or f'chain-{object_type}'}
