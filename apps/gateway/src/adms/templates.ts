/**
 * ZKTeco template uploads (plan Appendix B, dewanlab/ZhuKe423 implementations). Templates arrive either as OPERLOG
 * lines prefixed FP / FACE / BIODATA, or as /iclock/fdata bodies with table=FINGERTMP|FACE|BIODATA. All use
 * tab-separated key=value pairs. Field names and type codes are from community implementations and must be verified
 * on purchased firmware (plan §7.3).
 *
 *   FP PIN=1\tFID=6\tSize=1234\tValid=1\tTMP=<base64>
 *   FACE PIN=1\tFID=0\tSIZE=..\tVALID=1\tTMP=<base64>
 *   BIODATA Pin=1\tNo=0\tIndex=0\tValid=1\tDuress=0\tType=2\tMajorVer=..\tMinorVer=..\tFormat=..\tTmp=<base64>
 */
export interface TemplateUpload {
  pin: string;
  modality: 'fingerprint' | 'face' | 'palm' | 'unknown';
  vendorFormat: string;
  /** base64 template as sent by the device (never decoded or logged). */
  templateB64: string;
  valid: boolean;
}

export interface TemplateParseResult { templates: TemplateUpload[]; photos: number; unknown: number; users: number }

/** ZKTeco BIODATA Type codes (community sources; verify on hardware). */
function biodataModality(type: string): TemplateUpload['modality'] {
  switch (type) {
    case '1': return 'fingerprint';
    case '2': case '9': return 'face';
    case '6': case '8': return 'palm';
    default: return 'unknown';
  }
}

function kv(line: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of line.split('\t')) {
    const i = part.indexOf('=');
    if (i > 0) out[part.slice(0, i).trim().toLowerCase()] = part.slice(i + 1).trim();
  }
  return out;
}

const B64_RE = /^[A-Za-z0-9+/=\r\n]+$/;

/** Parse OPERLOG or fdata bodies for template records. `table` is the query-string table hint (may be empty). */
export function parseTemplateUpload(table: string, body: string): TemplateParseResult {
  const res: TemplateParseResult = { templates: [], photos: 0, unknown: 0, users: 0 };
  const hint = table.toUpperCase();
  if (hint === 'ATTPHOTO' || hint === 'USERPIC' || hint === 'BIOPHOTO') { res.photos++; return res; }
  for (const rawLine of body.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const m = /^(FP|FACE|BIODATA|USER|OPLOG|USERPIC|BIOPHOTO)\s+(.*)$/i.exec(line);
    const prefix = (m ? m[1]! : hint).toUpperCase();
    const fields = kv(m ? m[2]! : line);
    const pin = fields.pin ?? '';
    if (prefix === 'USER') { res.users++; continue; }
    if (prefix === 'OPLOG') continue;
    if (prefix === 'USERPIC' || prefix === 'BIOPHOTO') { res.photos++; continue; }
    const tmp = fields.tmp ?? '';
    if (!pin || !tmp || !B64_RE.test(tmp)) { res.unknown++; continue; }
    const valid = (fields.valid ?? '1') !== '0';
    if (prefix === 'FP' || prefix === 'FINGERTMP') {
      res.templates.push({ pin, modality: 'fingerprint', vendorFormat: 'zk_fp_v10', templateB64: tmp.replace(/\s/g, ''), valid });
    } else if (prefix === 'FACE') {
      res.templates.push({ pin, modality: 'face', vendorFormat: 'zk_face_v7', templateB64: tmp.replace(/\s/g, ''), valid });
    } else if (prefix === 'BIODATA') {
      const type = fields.type ?? '';
      res.templates.push({ pin, modality: biodataModality(type), vendorFormat: `zk_biodata_t${type || '?'}_v${fields.majorver ?? '?'}.${fields.minorver ?? '?'}`, templateB64: tmp.replace(/\s/g, ''), valid });
    } else {
      res.unknown++;
    }
  }
  return res;
}
