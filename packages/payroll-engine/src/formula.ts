import { Decimal, D } from './decimal.js';

/**
 * Formula DSL (plan §11.2): a small expression language, parsed to an AST and evaluated with Decimal arithmetic.
 * Grammar (lowest to highest precedence):
 *   or → and → not → comparison (< <= > >= == !=) → additive (+ -) → multiplicative (* /) → unary minus → primary
 *   primary := number | "string" | identifier | identifier(args) | (expr)
 * No property access, no loops, no assignment, no arbitrary calls: only the functions listed in FUNCTIONS.
 */

export type Value = Decimal | boolean | string;

type Tok =
  | { k: 'num'; v: string } | { k: 'str'; v: string } | { k: 'id'; v: string }
  | { k: 'op'; v: string } | { k: 'lp' } | { k: 'rp' } | { k: 'comma' } | { k: 'eof' };

export type Ast =
  | { t: 'num'; v: string } | { t: 'str'; v: string } | { t: 'id'; name: string }
  | { t: 'bin'; op: string; l: Ast; r: Ast } | { t: 'un'; op: '-' | 'not'; e: Ast }
  | { t: 'call'; name: string; args: Ast[] };

export class FormulaError extends Error {}

const OPS = ['<=', '>=', '==', '!=', '<', '>', '+', '-', '*', '/'];

export function tokenize(src: string): Tok[] {
  const out: Tok[] = [];
  let i = 0;
  while (i < src.length) {
    const c = src[i]!;
    if (/\s/.test(c)) { i++; continue; }
    if (/[0-9.]/.test(c)) {
      const m = /^\d*\.?\d+(?:[eE][-+]?\d+)?|^\d+\.?/.exec(src.slice(i));
      if (!m || m[0] === '.') throw new FormulaError(`bad number at ${i}`);
      out.push({ k: 'num', v: m[0] }); i += m[0].length; continue;
    }
    if (/[A-Za-z_]/.test(c)) {
      const m = /^[A-Za-z_][A-Za-z0-9_]*/.exec(src.slice(i))!;
      out.push({ k: 'id', v: m[0] }); i += m[0].length; continue;
    }
    if (c === '"' || c === "'") {
      const end = src.indexOf(c, i + 1);
      if (end < 0) throw new FormulaError('unterminated string');
      out.push({ k: 'str', v: src.slice(i + 1, end) }); i = end + 1; continue;
    }
    if (c === '(') { out.push({ k: 'lp' }); i++; continue; }
    if (c === ')') { out.push({ k: 'rp' }); i++; continue; }
    if (c === ',') { out.push({ k: 'comma' }); i++; continue; }
    const op = OPS.find((o) => src.startsWith(o, i));
    if (op) { out.push({ k: 'op', v: op }); i += op.length; continue; }
    throw new FormulaError(`unexpected character '${c}' at ${i}`);
  }
  out.push({ k: 'eof' });
  return out;
}

/** Nesting limit so hostile formulas cannot exhaust the stack. */
export const MAX_DEPTH = 64;
export const MAX_FORMULA_LENGTH = 2000;

export function parseFormula(src: string): Ast {
  if (typeof src !== 'string') throw new FormulaError('formula must be a string');
  if (src.length > MAX_FORMULA_LENGTH) throw new FormulaError(`formula longer than ${MAX_FORMULA_LENGTH} characters`);
  const toks = tokenize(src);
  let p = 0;
  let depth = 0;
  const enter = (): void => { if (++depth > MAX_DEPTH) throw new FormulaError('expression nested too deeply'); };
  const leave = (): void => { depth--; };
  const peek = () => toks[p]!;
  const next = () => toks[p++]!;
  const isOp = (v: string) => { const t = peek(); return t.k === 'op' && t.v === v; };
  const isId = (v: string) => { const t = peek(); return t.k === 'id' && t.v === v; };
  const expect = (k: Tok['k']) => { const t = next(); if (t.k !== k) throw new FormulaError(`expected ${k} at token ${p - 1}`); return t; };

  const orExpr = (): Ast => { let l = andExpr(); while (isId('or')) { next(); l = { t: 'bin', op: 'or', l, r: andExpr() }; } return l; };
  const andExpr = (): Ast => { let l = notExpr(); while (isId('and')) { next(); l = { t: 'bin', op: 'and', l, r: notExpr() }; } return l; };
  const notExpr = (): Ast => { if (isId('not')) { next(); enter(); const e = notExpr(); leave(); return { t: 'un', op: 'not', e }; } return cmpExpr(); };
  const cmpExpr = (): Ast => {
    let l = addExpr();
    while (['<', '<=', '>', '>=', '==', '!='].some(isOp)) { const op = (next() as { v: string }).v; l = { t: 'bin', op, l, r: addExpr() }; }
    return l;
  };
  const addExpr = (): Ast => { let l = mulExpr(); while (isOp('+') || isOp('-')) { const op = (next() as { v: string }).v; l = { t: 'bin', op, l, r: mulExpr() }; } return l; };
  const mulExpr = (): Ast => { let l = unary(); while (isOp('*') || isOp('/')) { const op = (next() as { v: string }).v; l = { t: 'bin', op, l, r: unary() }; } return l; };
  const unary = (): Ast => { if (isOp('-')) { next(); enter(); const e = unary(); leave(); return { t: 'un', op: '-', e }; } return primary(); };
  const primary = (): Ast => {
    const t = next();
    if (t.k === 'num') return { t: 'num', v: t.v };
    if (t.k === 'str') return { t: 'str', v: t.v };
    if (t.k === 'lp') { enter(); const e = orExpr(); leave(); expect('rp'); return e; }
    if (t.k === 'id') {
      if (t.v === 'true' || t.v === 'false') return { t: 'id', name: t.v };
      if (peek().k === 'lp') {
        next();
        enter();
        const args: Ast[] = [];
        if (peek().k !== 'rp') { args.push(orExpr()); while (peek().k === 'comma') { next(); args.push(orExpr()); } }
        leave();
        expect('rp');
        return { t: 'call', name: t.v, args };
      }
      return { t: 'id', name: t.v };
    }
    throw new FormulaError(`unexpected token ${t.k} at ${p - 1}`);
  };
  const ast = orExpr();
  if (peek().k !== 'eof') throw new FormulaError(`unexpected trailing input at token ${p}`);
  return ast;
}

export interface EvalEnv {
  /** Component amounts and variables (Decimal), flags (boolean) and strings (e.g. regime). */
  vars: Record<string, Value>;
  /** rule("key") / rule("key","path.to.value") resolver. */
  rule: (key: string, path?: string) => Value;
}

export const FUNCTIONS: Record<string, { min: number; max: number }> = {
  min: { min: 1, max: 32 }, max: { min: 1, max: 32 }, round: { min: 1, max: 2 }, floor: { min: 1, max: 1 }, ceil: { min: 1, max: 1 },
  abs: { min: 1, max: 1 }, if: { min: 3, max: 3 }, rule: { min: 1, max: 2 },
};

const num = (v: Value, where: string): Decimal => {
  if (v instanceof Decimal) return v;
  throw new FormulaError(`${where}: expected a number, got ${typeof v}`);
};
const bool = (v: Value, where: string): boolean => {
  if (typeof v === 'boolean') return v;
  throw new FormulaError(`${where}: expected a condition, got ${v instanceof Decimal ? 'number' : typeof v}`);
};

export function evaluateAst(ast: Ast, env: EvalEnv): Value {
  switch (ast.t) {
    case 'num': return D(ast.v);
    case 'str': return ast.v;
    case 'id': {
      if (ast.name === 'true') return true;
      if (ast.name === 'false') return false;
      // Own properties only: 'constructor', '__proto__' and friends are not variables.
      if (!Object.hasOwn(env.vars, ast.name)) throw new FormulaError(`unknown identifier ${ast.name}`);
      const v = env.vars[ast.name]!;
      if (!(v instanceof Decimal) && typeof v !== 'boolean' && typeof v !== 'string') throw new FormulaError(`identifier ${ast.name} is not a value`);
      return v;
    }
    case 'un': {
      const v = evaluateAst(ast.e, env);
      return ast.op === '-' ? num(v, 'negation').neg() : !bool(v, 'not');
    }
    case 'bin': {
      if (ast.op === 'and') return bool(evaluateAst(ast.l, env), 'and') && bool(evaluateAst(ast.r, env), 'and');
      if (ast.op === 'or') return bool(evaluateAst(ast.l, env), 'or') || bool(evaluateAst(ast.r, env), 'or');
      const l = evaluateAst(ast.l, env); const r = evaluateAst(ast.r, env);
      if (ast.op === '==' || ast.op === '!=') {
        const eq = l instanceof Decimal && r instanceof Decimal ? l.eq(r) : l === r;
        return ast.op === '==' ? eq : !eq;
      }
      const a = num(l, ast.op); const b = num(r, ast.op);
      switch (ast.op) {
        case '+': return a.plus(b);
        case '-': return a.minus(b);
        case '*': return a.times(b);
        case '/': if (b.isZero()) throw new FormulaError('division by zero'); return a.div(b);
        case '<': return a.lt(b); case '<=': return a.lte(b); case '>': return a.gt(b); case '>=': return a.gte(b);
      }
      throw new FormulaError(`unknown operator ${ast.op}`);
    }
    case 'call': {
      const spec = FUNCTIONS[ast.name];
      if (!spec) throw new FormulaError(`unknown function ${ast.name}`);
      if (ast.args.length < spec.min || ast.args.length > spec.max) throw new FormulaError(`${ast.name} expects ${spec.min}-${spec.max} arguments`);
      if (ast.name === 'if') return bool(evaluateAst(ast.args[0]!, env), 'if') ? evaluateAst(ast.args[1]!, env) : evaluateAst(ast.args[2]!, env);
      if (ast.name === 'rule') {
        const key = evaluateAst(ast.args[0]!, env); const path = ast.args[1] ? evaluateAst(ast.args[1], env) : undefined;
        if (typeof key !== 'string' || (path !== undefined && typeof path !== 'string')) throw new FormulaError('rule() takes string arguments');
        return env.rule(key, path);
      }
      const args = ast.args.map((a, i) => num(evaluateAst(a, env), `${ast.name} argument ${i + 1}`));
      switch (ast.name) {
        case 'min': return Decimal.min(...args);
        case 'max': return Decimal.max(...args);
        case 'round': {
          const dp = args[1] ? args[1] : D(0);
          if (!dp.isInteger() || dp.lt(0) || dp.gt(10)) throw new FormulaError('round() decimal places must be an integer between 0 and 10');
          return args[0]!.toDecimalPlaces(dp.toNumber(), Decimal.ROUND_HALF_UP);
        }
        case 'floor': return args[0]!.floor();
        case 'ceil': return args[0]!.ceil();
        case 'abs': return args[0]!.abs();
      }
      throw new FormulaError(`unknown function ${ast.name}`);
    }
  }
}

/** Any failure (parse, type, arithmetic, Decimal range) surfaces as a FormulaError; nothing else escapes. */
export function evaluateFormula(src: string, env: EvalEnv): Value {
  try {
    const v = evaluateAst(parseFormula(src), env);
    if (v instanceof Decimal && !v.isFinite()) throw new FormulaError('result is not a finite number');
    return v;
  } catch (e) {
    if (e instanceof FormulaError) throw e;
    throw new FormulaError(`formula error: ${(e as Error).message}`);
  }
}

/** Identifiers referenced by a formula (component codes and variables), excluding keywords. */
export function formulaReferences(src: string): Set<string> {
  const refs = new Set<string>();
  let ast: Ast;
  try { ast = parseFormula(src); } catch (e) { if (e instanceof FormulaError) throw e; throw new FormulaError(`formula error: ${(e as Error).message}`); }
  const walk = (a: Ast): void => {
    if (a.t === 'id') { if (!['true', 'false'].includes(a.name)) refs.add(a.name); }
    else if (a.t === 'bin') { walk(a.l); walk(a.r); }
    else if (a.t === 'un') walk(a.e);
    else if (a.t === 'call') a.args.forEach(walk);
  };
  walk(ast);
  return refs;
}

/** Built-in variables every formula may use (plan §11.2). */
export const BUILTIN_VARS = [
  'days_in_bs_month', 'paid_days', 'lop_days', 'hourly_divisor', 'ot_hours', 'months_in_service', 'is_female', 'regime', 'bs_month', 'fiscal_year',
] as const;

/** Returns an error message, or null when the formula parses and only references known names. */
export function validateFormula(src: string, knownNames: Iterable<string>): string | null {
  try {
    const known = new Set<string>([...knownNames, ...BUILTIN_VARS]);
    const ast = parseFormula(src);
    const check = (a: Ast): void => {
      if (a.t === 'id' && !['true', 'false'].includes(a.name) && !known.has(a.name)) throw new FormulaError(`unknown identifier ${a.name}`);
      if (a.t === 'call') {
        const spec = FUNCTIONS[a.name];
        if (!spec) throw new FormulaError(`unknown function ${a.name}`);
        if (a.args.length < spec.min || a.args.length > spec.max) throw new FormulaError(`${a.name} expects ${spec.min}-${spec.max} arguments`);
        a.args.forEach(check);
      }
      if (a.t === 'bin') { check(a.l); check(a.r); }
      if (a.t === 'un') check(a.e);
    };
    check(ast);
    return null;
  } catch (e) {
    return (e as Error).message;
  }
}
