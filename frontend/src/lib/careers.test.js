import { describe, expect, it } from 'vitest';
import { careersPath } from './careers';

const JOB = '01a18182-8fb1-7e64-a835-9183b8302769';

describe('careersPath', () => {
  it('reads the server share links /careers/<code> and /careers/<code>/<jobId>', () => {
    expect(careersPath('DEMO', null)).toEqual({ company: 'DEMO', jobId: '' });
    expect(careersPath(`DEMO/${JOB}`, null)).toEqual({ company: 'DEMO', jobId: JOB });
  });
  it('reads the in-page links /careers/<jobId>?company=<code>', () => {
    expect(careersPath(JOB, 'DEMO')).toEqual({ company: 'DEMO', jobId: JOB });
    expect(careersPath('', 'DEMO')).toEqual({ company: 'DEMO', jobId: '' });
    expect(careersPath(undefined, null)).toEqual({ company: null, jobId: '' });
  });
});
