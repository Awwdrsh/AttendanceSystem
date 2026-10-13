/** Typed errors mapped to HTTP statuses by the central error handler in app.ts. */
export class HttpError extends Error {
  constructor(readonly status: number, message: string, readonly details?: unknown) { super(message); }
}
export class NotFoundError extends HttpError { constructor(msg = 'not found') { super(404, msg); } }
export class ForbiddenError extends HttpError { constructor(msg = 'forbidden') { super(403, msg); } }
export class UnauthorizedError extends HttpError { constructor(msg = 'unauthorized') { super(401, msg); } }
export class ConflictError extends HttpError { constructor(msg = 'conflict') { super(409, msg); } }
export class BadRequestError extends HttpError { constructor(msg = 'bad request', details?: unknown) { super(400, msg, details); } }
