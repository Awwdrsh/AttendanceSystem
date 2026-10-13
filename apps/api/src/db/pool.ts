import pg from 'pg';

const { Pool, types } = pg;

// Keep numeric/bigint as strings (money must never pass through a JS float); parse int4 normally.
types.setTypeParser(types.builtins.NUMERIC, (v) => v);
types.setTypeParser(types.builtins.INT8, (v) => v);
// Return DATE columns as 'YYYY-MM-DD' strings, not JS Dates in local time.
types.setTypeParser(types.builtins.DATE, (v) => v);

export function createPool(connectionString: string): pg.Pool {
  // Nepal has no DST, so a fixed session zone makes current_date / date arithmetic in SQL mean Nepal calendar dates.
  // Set at connection start-up so no query can run before it applies.
  return new Pool({ connectionString, max: 10, options: '-c TimeZone=Asia/Kathmandu' });
}

export type Db = pg.Pool;
export type Queryable = pg.Pool | pg.PoolClient;
