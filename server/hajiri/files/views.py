"""Upload limits shared by the files JSON API (api.py)."""

# A base64 data URL of a 4 MB file, with room for the JSON around it (express.json({ limit: '6mb' }) on /files).
JSON_UPLOAD_MAX = 6 * 1024 * 1024
