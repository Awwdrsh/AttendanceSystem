"""Typed errors carrying an HTTP status; views and the error middleware turn them into responses."""


class HttpError(Exception):
    status = 500

    def __init__(self, message: str | None = None, details: object = None):
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        self.details = details

    default_message = 'error'


class BadRequestError(HttpError):
    status, default_message = 400, 'bad request'


class UnauthorizedError(HttpError):
    status, default_message = 401, 'unauthorized'


class ForbiddenError(HttpError):
    status, default_message = 403, 'forbidden'


class NotFoundError(HttpError):
    status, default_message = 404, 'not found'


class ConflictError(HttpError):
    status, default_message = 409, 'conflict'


class TooManyRequestsError(HttpError):
    status, default_message = 429, 'too many requests'


class UnprocessableError(HttpError):
    """Bad configuration or input found while calculating (a broken payroll formula, an amount out of range)."""
    status, default_message = 422, 'unprocessable'


class ValidationError(BadRequestError):
    """Request data failed its schema (zod in the TS API): {'error': 'validation', 'issues': [{path, message}]}."""
    default_message = 'validation'

    def __init__(self, issues: list[dict] | None = None, message: str = 'validation'):
        super().__init__(message, issues or [])
        self.issues = issues or []


class FeatureOffError(NotFoundError):
    """A module the company switched off (Settings, Features) answers 404 "feature off"."""

    def __init__(self, feature: str):
        super().__init__('feature off', {'feature': feature})
        self.feature = feature


# PostgreSQL errors the TS error handler turned into client errors (apps/api/src/app.ts).
_DB_ERRORS = {
    '23505': (409, 'already exists'),
    '23P01': (409, 'overlaps an existing period'),
    '23503': (400, 'referenced record does not exist'),
    '23514': (400, 'value not allowed'),
    '22P02': (400, 'invalid value format'),
    '22007': (400, 'invalid value format'),
    '22008': (400, 'invalid value format'),
    '22021': (400, 'text contains a character that is not allowed'),
    '22P05': (400, 'text contains a character that is not allowed'),
}


def from_db_error(exc: BaseException) -> HttpError | None:
    """Map a database error (Django's wrapper or psycopg's) to the same status and message as the TS API, or None."""
    cause = exc
    for _ in range(3):
        if getattr(cause, 'sqlstate', None):
            break
        cause = getattr(cause, '__cause__', None) or getattr(cause, '__context__', None)
        if cause is None:
            return None
    code = getattr(cause, 'sqlstate', None)
    diag = getattr(cause, 'diag', None)
    constraint = getattr(diag, 'constraint_name', None) if diag else None
    if code == 'P0001':
        msg = (getattr(diag, 'message_primary', None) if diag else None) or str(cause).split('\n')[0]
        err = ConflictError(msg.removeprefix('error: ') or 'operation not allowed')
        return err
    if code in _DB_ERRORS:
        status, message = _DB_ERRORS[code]
        err = (ConflictError if status == 409 else BadRequestError)(message, {'constraint': constraint} if constraint else None)
        return err
    return None


def error_body(exc: HttpError) -> dict:
    """The JSON body the TS API sent for an error: {'error': message, ...details}."""
    if isinstance(exc, ValidationError):
        return {'error': 'validation', 'issues': exc.issues}
    body: dict = {'error': exc.message}
    if isinstance(exc.details, dict) and (isinstance(exc, FeatureOffError) or 'constraint' in exc.details):
        body.update(exc.details)
    elif exc.details is not None:
        body['details'] = exc.details
    return body
