import pg from 'pg';

const { Pool, types } = pg;

// Keep numeric/bigint as strings (money must never pass through a JS float); parse int4 normally.
types.setTypeParser(types.builtins.NUMERIC, (v) => v);
types.setTypeParser(types.builtins.INT8, (v) => v);
// Return DATE columns as 'YYYY-MM-DD' strings, not JS Dates in local time.
types.setTypeParser(types.builtins.DATE, (v) => v);

export function createPool(connectionString: string): pg.Pool {
  return new Pool({ connectionString, max: 10 });
}

export type Db = pg.Pool;
export type Queryable = pg.Pool | pg.PoolClient;
