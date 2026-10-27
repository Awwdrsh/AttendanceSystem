"""Pure Nepal payroll computation: formula DSL, component graph, proration, versioned tax schedules, SSF/PF and TDS
projection (plan.md §11).

Port of packages/payroll-engine. No Django imports: inputs and results are plain dicts with the same camelCase keys
as the TS engine, money is carried as decimal strings, and all arithmetic is decimal.Decimal under ENGINE_CONTEXT
(30 significant digits, half-up), matching the decimal.js settings of the TS version.
"""
from .compute import MAX_LINE_AMOUNT, PayrollError, compute_payslip, payable_fraction
from .defaults import DEFAULT_COMPONENTS, DEFAULT_RULES, TAX_SCHEDULE_2082, TAX_SCHEDULE_2083, rules_for_fy
from .formula import (
    BUILTIN_VARS, FUNCTIONS, Ast, EvalEnv, FormulaError, Value, evaluate_formula, formula_references, parse_formula,
    validate_formula,
)
from .numeric import ENGINE_CONTEXT, D, Decimal, DecimalError, money, paisa, rupee
from .optimum import OptimumCit, optimum_cit
from .tax import TaxInput, compute_tds
from .types import *  # noqa: F401,F403
from .words import amount_in_words_en, amount_in_words_ne, format_npr, to_devanagari_digits

__all__ = [
    'MAX_LINE_AMOUNT', 'PayrollError', 'compute_payslip', 'payable_fraction', 'DEFAULT_COMPONENTS', 'DEFAULT_RULES',
    'TAX_SCHEDULE_2082', 'TAX_SCHEDULE_2083', 'rules_for_fy', 'BUILTIN_VARS', 'FUNCTIONS', 'Ast', 'EvalEnv',
    'FormulaError', 'Value', 'evaluate_formula', 'formula_references', 'parse_formula', 'validate_formula',
    'ENGINE_CONTEXT', 'D', 'Decimal', 'DecimalError', 'money', 'paisa', 'rupee', 'OptimumCit', 'optimum_cit',
    'TaxInput', 'compute_tds', 'amount_in_words_en', 'amount_in_words_ne', 'format_npr', 'to_devanagari_digits',
]
