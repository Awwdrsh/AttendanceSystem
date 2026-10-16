import { post } from './client';

export type UploadPurpose = 'employee_document' | 'company_document' | 'expense_receipt' | 'travel_bill' | 'helpdesk_attachment'
  | 'training_certificate' | 'profile_change' | 'hiring_cv' | 'avatar' | 'tax_proof' | 'grievance_attachment' | 'task_attachment';

export interface UploadedFile { id: string; filename: string; mime: string; bytes: number; purpose: UploadPurpose }

export const FILE_MAX_BYTES = 4 * 1024 * 1024;
export const FILE_ACCEPT = 'application/pdf,image/jpeg,image/png,image/webp';

export function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.onerror = () => reject(new Error('The file could not be read.'));
    r.readAsDataURL(file);
  });
}

/** Upload one PDF or image (max 4 MB); attach the returned id to the record that needs it. */
export async function uploadFile(file: File, purpose: UploadPurpose): Promise<UploadedFile> {
  if (file.size > FILE_MAX_BYTES) throw new Error('The file is too large (max 4 MB).');
  const r = await post<{ file: UploadedFile }>('/files', { purpose, filename: file.name, dataUrl: await fileToDataUrl(file) });
  return r.file;
}
