// Mirrors backend/app/services/manual_upload.py. These are transport budgets;
// account quota and durable accounting remain authoritative in the backend.
export const MAX_UPLOAD_FILES = 20;
export const MAX_UPLOAD_FILE_BYTES = 500 * 1024 * 1024;
export const MAX_UPLOAD_BATCH_BYTES = 1024 * 1024 * 1024;
export const MAX_UPLOAD_REQUEST_BYTES = MAX_UPLOAD_BATCH_BYTES + 2 * 1024 * 1024;
