"""Pure calculation engines: Bikram Sambat calendar, attendance day and payroll.

No Django, no database, no clock and no I/O: every function depends only on its arguments, so the web code, the
worker and the tests can call them anywhere. Company scoping, permissions and storage belong to the callers.

Their outputs are pinned byte for byte to fixtures recorded from the TS engines (tests/parity/fixtures and
tests/engines/payroll/golden), because results computed by either stack are stored side by side: inputs_hash values,
payslip lines and their explanation text. Standard Python is used wherever that holds exactly:

- Collation of punch ids: pyuca (the Unicode Collation Algorithm), matching Node's localeCompare.
- JSON string escaping in canonical_json: json.dumps, for every string without surrogate code points.
- Dates: datetime.date for calendar arithmetic and ISO formatting; bisect/accumulate for the BS table lookups.
- Money: the decimal module, under a context matching decimal.js (30 digits, half-up).
- Hashing: hashlib.

What is still emulated, and why the standard behaviour would change recorded output:

- payroll/numeric.py, decimal.js semantics on top of decimal: string parsing (hex/binary/octal literals, numeric
  separators), signed zero in toFixed/toString, min/max tie rules and the +-9e15 exponent limits. Python's Decimal
  prints '1E+1' and '-0.00' and accepts other syntax, which would change error messages and explanation text.
- attendance/hash._js_number and payroll/numeric.js_num_str: Number.prototype.toString. Python's repr prints '2.0'
  and '1e-07' and switches to exponent form at other magnitudes, which would change any inputs_hash holding such a
  number.
- attendance/timeutil.js_round and round_minutes: Math.round is half-up; Python's round() is half-to-even.
- attendance/timeutil._civil_from_days: only for years datetime cannot hold (before 1 or after 9999), which JS Dates
  print in the expanded '+YYYYYY' form.
- payroll/_js.py: Object.prototype members visible on plain objects (an HR code named 'toString' resolves to a
  function in JS), JS truthiness, '??', typeof and remainder with the dividend's sign; they decide which error a
  formula or rule path raises. Also UTF-16 code units, because formula error positions count them.
- payroll/formula.py _JS_SPACE: the ECMAScript whitespace set is not str.isspace() (it has U+FEFF and lacks
  U+001C-U+001F and U+0085).
- payroll/words.py: table lookups that print 'undefined' and float remainders, reachable only for amounts of 30 or
  more digits.
"""
