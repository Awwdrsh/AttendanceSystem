"""
Company scoping helpers for services and views. Every query that reads or writes company data goes through
`Model.objects.for_company(company_id)` (hajiri.db.models.base.CompanyQuerySet); these helpers cover the common
shapes around it:

    from hajiri.core.auth import company_id_of
    from hajiri.core.scoping import get_or_404, scoped

    cid = company_id_of(request.hajiri_user)                        # the ACTIVE company
    ticket = get_or_404(HelpdeskTicket, cid, 'ticket not found', pk=ticket_id)
    open_tickets = scoped(HelpdeskTicket, cid).filter(status=HelpdeskTicket.Status.OPEN)

A row of another company is never "forbidden": it is simply not found (NotFoundError with the TS message), so ids
cannot be probed across tenants. A malformed id answers like the TS API did for a bad uuid parameter in SQL
(400 'invalid value format').
"""
from __future__ import annotations

import uuid
from typing import Any, TypeVar

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models

from .errors import BadRequestError, NotFoundError

M = TypeVar('M', bound=models.Model)


def as_uuid(value: Any) -> uuid.UUID:
    """A UUID from a path or body value; anything else is 400 'invalid value format' (PostgreSQL's 22P02 mapping)."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise BadRequestError('invalid value format') from None


def is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError, AttributeError):
        return False


def scoped(model_or_qs: type[M] | models.QuerySet, company_id) -> models.QuerySet:
    """`for_company` on a model or on a queryset that is already filtered."""
    qs = model_or_qs if isinstance(model_or_qs, models.QuerySet) else model_or_qs.objects.all()
    return qs.for_company(company_id)


def get_or_404(model_or_qs: type[M] | models.QuerySet, company_id, message: str = 'not found', **lookup) -> M:
    """The one row of this company matching `lookup`, or NotFoundError(message). UUID lookups are validated first
    (pk=, id= and *_id= values)."""
    for k, v in list(lookup.items()):
        if (k in ('pk', 'id') or k.endswith('_id')) and v is not None and not hasattr(v, 'resolve_expression'):
            lookup[k] = as_uuid(v)
    try:
        return scoped(model_or_qs, company_id).get(**lookup)
    except (models.ObjectDoesNotExist, DjangoValidationError):
        raise NotFoundError(message) from None


def exists_in_company(model_or_qs, company_id, **lookup) -> bool:
    try:
        return scoped(model_or_qs, company_id).filter(**lookup).exists()
    except DjangoValidationError:
        return False
