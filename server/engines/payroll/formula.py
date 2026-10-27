"""Formula DSL (plan §11.2): a small expression language, parsed to an AST and evaluated with Decimal arithmetic.

Port of src/formula.ts. Grammar (lowest to highest precedence):
  or -> and -> not -> comparison (< <= > >= == !=) -> additive (+ -) -> multiplicative (* /) -> unary minus -> primary
  primary := number | "string" | identifier | identifier(args) | (expr)
No property access, no loops, no assignment, no arbitrary calls: only the functions listed in FUNCTIONS.

The AST is plain dicts shaped exactly like the TS objects ({'t': 'bin', 'op': '+', 'l': ..., 'r': ...}). Positions in
error messages count UTF-16 code units, as in JS. Left-deep operator chains (1 + 1 + ... + 1, up to the 2000-character
limit) are walked iteratively, because Python's recursion limit is far below V8's stack.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Union

from ._js import OBJECT_PROTO_NAMES, from_utf16, to_utf16, typeof
from .numeric import (
    UNDEFINED, D, Decimal, ceil, dmax, dmin, engine_context, finalise, floor, is_integer, to_dp,
)

__all__ = [
    'Value', 'Ast', 'FormulaError', 'EvalEnv', 'MAX_DEPTH', 'MAX_FORMULA_LENGTH', 'FUNCTIONS', 'BUILTIN_VARS',
    'tokenize', 'parse_formula', 'evaluate_ast', 'evaluate_formula', 'formula_references', 'validate_formula',
]

Value = Union[Decimal, bool, str]
Ast = dict[str, Any]
Tok = dict[str, Any]


class FormulaError(Exception):
    pass


OPS = ['<=', '>=', '==', '!=', '<', '>', '+', '-', '*', '/']
# JS /\s/ (ECMAScript WhiteSpace and LineTerminator), which is not the same set as str.isspace().
_JS_SPACE = frozenset('\t\n\v\f\r              '
                      '    　﻿')
_NUMBER = re.compile(r'[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?|[0-9]+\.?')
_IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def tokenize(src: str) -> list[Tok]:
    """src must already be in UTF-16 code units (see parse_formula)."""
    out: list[Tok] = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c in _JS_SPACE:
            i += 1
            continue
        if '0' <= c <= '9' or c == '.':
            m = _NUMBER.match(src, i)
            if not m or m[0] == '.':
                raise FormulaError(f'bad number at {i}')
            out.append({'k': 'num', 'v': m[0]})
            i += len(m[0])
            continue
        if c == '_' or 'A' <= c <= 'Z' or 'a' <= c <= 'z':
            m = _IDENT.match(src, i)
            assert m
            out.append({'k': 'id', 'v': m[0]})
            i += len(m[0])
            continue
        if c in ('"', "'"):
            end = src.find(c, i + 1)
            if end < 0:
                raise FormulaError('unterminated string')
            out.append({'k': 'str', 'v': from_utf16(src[i + 1:end])})
            i = end + 1
            continue
        if c == '(':
            out.append({'k': 'lp'})
            i += 1
            continue
        if c == ')':
            out.append({'k': 'rp'})
            i += 1
            continue
        if c == ',':
            out.append({'k': 'comma'})
            i += 1
            continue
        op = next((o for o in OPS if src.startswith(o, i)), None)
        if op:
            out.append({'k': 'op', 'v': op})
            i += len(op)
            continue
        raise FormulaError(f"unexpected character '{c}' at {i}")
    out.append({'k': 'eof'})
    return out


# Nesting limit so hostile formulas cannot exhaust the stack.
MAX_DEPTH = 64
MAX_FORMULA_LENGTH = 2000

_CMP = ('<', '<=', '>', '>=', '==', '!=')


class _Parser:
    def __init__(self, toks: list[Tok]) -> None:
        self.toks = toks
        self.p = 0
        self.depth = 0

    def enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise FormulaError('expression nested too deeply')

    def leave(self) -> None:
        self.depth -= 1

    def peek(self) -> Tok:
        return self.toks[self.p]

    def next(self) -> Tok:
        t = self.toks[self.p]
        self.p += 1
        return t

    def is_op(self, v: str) -> bool:
        t = self.peek()
        return t['k'] == 'op' and t['v'] == v

    def is_id(self, v: str) -> bool:
        t = self.peek()
        return t['k'] == 'id' and t['v'] == v

    def expect(self, k: str) -> Tok:
        t = self.next()
        if t['k'] != k:
            raise FormulaError(f'expected {k} at token {self.p - 1}')
        return t

    def or_expr(self) -> Ast:
        left = self.and_expr()
        while self.is_id('or'):
            self.next()
            left = {'t': 'bin', 'op': 'or', 'l': left, 'r': self.and_expr()}
        return left

    def and_expr(self) -> Ast:
        left = self.not_expr()
        while self.is_id('and'):
            self.next()
            left = {'t': 'bin', 'op': 'and', 'l': left, 'r': self.not_expr()}
        return left

    def not_expr(self) -> Ast:
        if self.is_id('not'):
            self.next()
            self.enter()
            e = self.not_expr()
            self.leave()
            return {'t': 'un', 'op': 'not', 'e': e}
        return self.cmp_expr()

    def cmp_expr(self) -> Ast:
        left = self.add_expr()
        while any(self.is_op(o) for o in _CMP):
            op = self.next()['v']
            left = {'t': 'bin', 'op': op, 'l': left, 'r': self.add_expr()}
        return left

    def add_expr(self) -> Ast:
        left = self.mul_expr()
        while self.is_op('+') or self.is_op('-'):
            op = self.next()['v']
            left = {'t': 'bin', 'op': op, 'l': left, 'r': self.mul_expr()}
        return left

    def mul_expr(self) -> Ast:
        left = self.unary()
        while self.is_op('*') or self.is_op('/'):
            op = self.next()['v']
            left = {'t': 'bin', 'op': op, 'l': left, 'r': self.unary()}
        return left

    def unary(self) -> Ast:
        if self.is_op('-'):
            self.next()
            self.enter()
            e = self.unary()
            self.leave()
            return {'t': 'un', 'op': '-', 'e': e}
        return self.primary()

    def primary(self) -> Ast:
        t = self.next()
        k = t['k']
        if k == 'num':
            return {'t': 'num', 'v': t['v']}
        if k == 'str':
            return {'t': 'str', 'v': t['v']}
        if k == 'lp':
            self.enter()
            e = self.or_expr()
            self.leave()
            self.expect('rp')
            return e
        if k == 'id':
            if t['v'] in ('true', 'false'):
                return {'t': 'id', 'name': t['v']}
            if self.peek()['k'] == 'lp':
                self.next()
                self.enter()
                args: list[Ast] = []
                if self.peek()['k'] != 'rp':
                    args.append(self.or_expr())
                    while self.peek()['k'] == 'comma':
                        self.next()
                        args.append(self.or_expr())
                self.leave()
                self.expect('rp')
                return {'t': 'call', 'name': t['v'], 'args': args}
            return {'t': 'id', 'name': t['v']}
        raise FormulaError(f'unexpected token {k} at {self.p - 1}')


def parse_formula(src: str) -> Ast:
    if not isinstance(src, str):
        raise FormulaError('formula must be a string')
    units = to_utf16(src)
    if len(units) > MAX_FORMULA_LENGTH:
        raise FormulaError(f'formula longer than {MAX_FORMULA_LENGTH} characters')
    p = _Parser(tokenize(units))
    ast = p.or_expr()
    if p.peek()['k'] != 'eof':
        raise FormulaError(f'unexpected trailing input at token {p.p}')
    return ast


@dataclass
class EvalEnv:
    # Component amounts and variables (Decimal), flags (bool) and strings (e.g. regime).
    vars: dict[str, Any]
    # rule("key") / rule("key", "path.to.value") resolver.
    rule: Callable[[str, 'str | None'], Any]


# Arity of each allowed function.
FUNCTIONS: dict[str, dict[str, int]] = {
    'min': {'min': 1, 'max': 32}, 'max': {'min': 1, 'max': 32}, 'round': {'min': 1, 'max': 2},
    'floor': {'min': 1, 'max': 1}, 'ceil': {'min': 1, 'max': 1}, 'abs': {'min': 1, 'max': 1},
    'if': {'min': 3, 'max': 3}, 'rule': {'min': 1, 'max': 2},
}


def _spec(name: str) -> dict[str, int] | None | bool:
    """FUNCTIONS[name] on the JS object: a prototype member is truthy but has no arity (every count passes)."""
    if name in FUNCTIONS:
        return FUNCTIONS[name]
    return True if name in OBJECT_PROTO_NAMES else None


def _arity_ok(spec: dict[str, int] | bool, n: int) -> bool:
    return spec is True or not (n < spec['min'] or n > spec['max'])  # type: ignore[index]


def _num(v: Any, where: str) -> Decimal:
    if isinstance(v, Decimal):
        return v
    raise FormulaError(f'{where}: expected a number, got {typeof(v)}')


def _bool(v: Any, where: str) -> bool:
    if isinstance(v, bool):
        return v
    raise FormulaError(f"{where}: expected a condition, got {'number' if isinstance(v, Decimal) else typeof(v)}")


def _bin(op: str, left: Any, right_ast: Ast, env: EvalEnv) -> Any:
    if op == 'and':
        return _bool(left, 'and') and _bool(_eval(right_ast, env), 'and')
    if op == 'or':
        return _bool(left, 'or') or _bool(_eval(right_ast, env), 'or')
    right = _eval(right_ast, env)
    if op in ('==', '!='):
        if isinstance(left, Decimal) and isinstance(right, Decimal):
            eq = left == right
        else:
            # JS ===: same type and value (Python would call True == Decimal(1) equal).
            eq = type(left) is type(right) and not isinstance(left, Decimal) and left == right
        return eq if op == '==' else not eq
    a = _num(left, op)
    b = _num(right, op)
    if op == '+':
        return finalise(a + b)
    if op == '-':
        return finalise(a - b)
    if op == '*':
        return finalise(a * b)
    if op == '/':
        if b.is_zero():
            raise FormulaError('division by zero')
        return finalise(a / b)
    if op == '<':
        return a < b
    if op == '<=':
        return a <= b
    if op == '>':
        return a > b
    if op == '>=':
        return a >= b
    raise FormulaError(f'unknown operator {op}')


def _call(ast: Ast, env: EvalEnv) -> Any:
    name = ast['name']
    args: list[Ast] = ast['args']
    spec = _spec(name)
    if not spec:
        raise FormulaError(f'unknown function {name}')
    if not _arity_ok(spec, len(args)):
        raise FormulaError(f"{name} expects {spec['min']}-{spec['max']} arguments")  # type: ignore[index]
    if name == 'if':
        return _eval(args[1], env) if _bool(_eval(args[0], env), 'if') else _eval(args[2], env)
    if name == 'rule':
        key = _eval(args[0], env)
        path = _eval(args[1], env) if len(args) > 1 else UNDEFINED
        if not isinstance(key, str) or (path is not UNDEFINED and not isinstance(path, str)):
            raise FormulaError('rule() takes string arguments')
        return env.rule(key, None if path is UNDEFINED else path)
    vals = [_num(_eval(a, env), f'{name} argument {i + 1}') for i, a in enumerate(args)]
    if name == 'min':
        return dmin(*vals)
    if name == 'max':
        return dmax(*vals)
    if name == 'round':
        dp = vals[1] if len(vals) > 1 else D(0)
        if not is_integer(dp) or dp < 0 or dp > 10:
            raise FormulaError('round() decimal places must be an integer between 0 and 10')
        return to_dp(vals[0], int(dp))
    if name == 'floor':
        return floor(vals[0])
    if name == 'ceil':
        return ceil(vals[0])
    if name == 'abs':
        return vals[0].copy_abs()
    raise FormulaError(f'unknown function {name}')


@engine_context
def evaluate_ast(ast: Ast, env: EvalEnv) -> Any:
    return _eval(ast, env)


def _eval(ast: Ast, env: EvalEnv) -> Any:
    t = ast['t']
    if t == 'num':
        return D(ast['v'])
    if t == 'str':
        return ast['v']
    if t == 'id':
        name = ast['name']
        if name == 'true':
            return True
        if name == 'false':
            return False
        # Own keys only: 'constructor', '__proto__' and friends are not variables.
        if name not in env.vars:
            raise FormulaError(f'unknown identifier {name}')
        v = env.vars[name]
        if not isinstance(v, (Decimal, bool, str)):
            raise FormulaError(f'identifier {name} is not a value')
        return v
    if t == 'un':
        v = _eval(ast['e'], env)
        return _num(v, 'negation').copy_negate() if ast['op'] == '-' else not _bool(v, 'not')
    if t == 'bin':
        # Walk the left spine iteratively; evaluation order (left operand first) is unchanged.
        spine = []
        node = ast
        while node['t'] == 'bin':
            spine.append(node)
            node = node['l']
        acc = _eval(node, env)
        for b in reversed(spine):
            acc = _bin(b['op'], acc, b['r'], env)
        return acc
    if t == 'call':
        return _call(ast, env)
    raise FormulaError(f'unknown node {t}')


@engine_context
def evaluate_formula(src: str, env: EvalEnv) -> Any:
    """Any failure (parse, type, arithmetic, Decimal range) surfaces as a FormulaError; nothing else escapes."""
    try:
        v = _eval(parse_formula(src), env)
        if isinstance(v, Decimal) and not v.is_finite():
            raise FormulaError('result is not a finite number')
        return v
    except FormulaError:
        raise
    except Exception as e:  # noqa: BLE001 - the contract is "only FormulaError escapes"
        raise FormulaError(f'formula error: {e}') from e


def _preorder(ast: Ast) -> Iterable[Ast]:
    """Nodes in the order the TS recursive walks visit them, without recursion."""
    stack = [ast]
    while stack:
        a = stack.pop()
        yield a
        t = a['t']
        if t == 'bin':
            stack.append(a['r'])
            stack.append(a['l'])
        elif t == 'un':
            stack.append(a['e'])
        elif t == 'call':
            stack.extend(reversed(a['args']))


def formula_references(src: str) -> dict[str, None]:
    """Identifiers referenced by a formula (component codes and variables), excluding keywords.

    Returned as an insertion-ordered set (dict keys), like the TS Set.
    """
    try:
        ast = parse_formula(src)
    except FormulaError:
        raise
    except Exception as e:  # noqa: BLE001
        raise FormulaError(f'formula error: {e}') from e
    refs: dict[str, None] = {}
    for a in _preorder(ast):
        if a['t'] == 'id' and a['name'] not in ('true', 'false'):
            refs[a['name']] = None
    return refs


# Built-in variables every formula may use (plan §11.2).
BUILTIN_VARS = (
    'days_in_bs_month', 'paid_days', 'lop_days', 'hourly_divisor', 'ot_hours', 'months_in_service', 'is_female',
    'regime', 'bs_month', 'fiscal_year',
)


def validate_formula(src: str, known_names: Iterable[str]) -> str | None:
    """Returns an error message, or None when the formula parses and only references known names."""
    try:
        known = {*known_names, *BUILTIN_VARS}
        ast = parse_formula(src)
        for a in _preorder(ast):
            if a['t'] == 'id' and a['name'] not in ('true', 'false') and a['name'] not in known:
                raise FormulaError(f"unknown identifier {a['name']}")
            if a['t'] == 'call':
                spec = _spec(a['name'])
                if not spec:
                    raise FormulaError(f"unknown function {a['name']}")
                if not _arity_ok(spec, len(a['args'])):
                    raise FormulaError(f"{a['name']} expects {spec['min']}-{spec['max']} arguments")  # type: ignore[index]
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)
