# Slack PDF Uploader

A minimal, runnable solution to upload a PDF (e.g., your resume) to a Slack channel using Slack Web API.

## Quick Start

- **Python**: 3.9+
- **Dependencies**: `requests`, `python-dotenv`

### 1) Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) Configure (config.json preferred)

Create `config.json` in the project root:

```json
{
  "SLACK_BOT_TOKEN": "xoxb-...",          // token with files:write scope
  "SLACK_CHANNEL_ID": "channel-id"        // target channel ID
}
```

Option B — environment fallback:

Copy `.env.example` to `.env` and fill in values if you don't want a config file:

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL_ID=channel-id
```

Precedence: `config.json` → `.env`/environment.

Place your resume PDF somewhere locally, e.g., `./resume.pdf`.

### 3) Run uploader

Uses `files.uploadV2` primarily. If unavailable in your workspace, it automatically falls back to the external upload flow (`files.getUploadURLExternal` + `files.completeUploadExternal`). After upload, it fetches the file's permalink and posts a clickable message with that link.

Note: The script currently posts a static Google Drive link alongside the Slack file link. You can change this in `uploader.py` by editing the `gdrive_link` value in `post_file_link`.

```bash
python uploader.py --file "./resume.pdf" --comment "Submitting my resume"
```

Options:

```bash
# Override channel and print HTTP payloads (token masked)
python uploader.py --file "./resume.pdf" \
  --channel C0123456789 \
  --comment "Submitting my resume" \
  --debug
```

If `--channel` is omitted, the script uses `SLACK_CHANNEL_ID` from `config.json` or `.env`.

---

## API Choice and Rationale

- **Primary**: `POST https://slack.com/api/files.uploadV2` (recommended modern upload)
- **Fallback**: `files.getUploadURLExternal` → PUT bytes → `files.completeUploadExternal`
- **Permalink**: `files.info` to resolve the canonical Slack file permalink
- **Message**: `chat.postMessage` to post a clickable link to the uploaded file
- **Auth**: Bearer token (`xoxb-...`) with `files:write`, `files:read`, and `chat:write` scopes; bot must be in the target channel.

Key parameters used:
- `file` (multipart) and `channel_id` (for V2)
- `filename`, `length` (for external pre-signed URL)
- `files`, `channel_id`, `initial_comment` (for complete)

---

## Workflow (Steps Taken)

1. Receive Slack token and ensure it is a `xoxb-` Bot token with `files:write`.
2. Put token and channel into `config.json` (or `.env` fallback).
3. Script attempts `conversations.join` (public channels) to avoid `not_in_channel`.
4. Upload via `files.uploadV2`; if not available, use external upload flow.
5. Resolve the file permalink using `files.info` (or use the permalink included in the upload response if present).
6. Post a message with a clickable link to the uploaded file. Slack file previews are allowed; external links (like Google Drive) do not unfurl.

---

## Permalinks and Scopes

- The script retrieves the correct Slack file permalink via `files.info`. Some upload responses may already include `permalink`; otherwise we fetch it explicitly.
- Required scopes:
  - `files:write` (to upload files)
  - `files:read` (to call `files.info` and fetch permalink)
  - Optional: `channels:join` to let the bot auto-join the target public channel
- The posted message uses Block Kit with `mrkdwn` and sets `unfurl_links: false` and `unfurl_media: true` so the Slack file expands but external links do not.

---

## Code Snippet (Core calls)

```python
# files.uploadV2
requests.post(
    "https://slack.com/api/files.uploadV2",
    headers={"Authorization": f"Bearer {token}"},
    data={"channel_id": channel_id, "initial_comment": initial_comment or ""},
    files={"file": (Path(file_path).name, open(file_path, "rb"), "application/pdf")},
)

# External upload (fallback)
# 1) get URL
requests.post(
    "https://slack.com/api/files.getUploadURLExternal",
    headers={"Authorization": f"Bearer {token}"},
    data={"filename": file_name, "length": str(size)},
)
# 2) PUT bytes to returned upload_url
# 3) complete upload
requests.post(
    "https://slack.com/api/files.completeUploadExternal",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    data=json.dumps({"files": [{"id": file_id, "title": file_name}], "channel_id": channel_id}),
)

# Resolve permalink and post a clickable link

file_id = file_id  # from upload response (e.g., p3["files"][0]["id"]) 
permalink = upload_payload.get("file", {}).get("permalink") if 'upload_payload' in globals() else None
if not permalink:
    info = requests.get(
        "https://slack.com/api/files.info",
        headers={"Authorization": f"Bearer {token}"},
        params={"file": file_id},
    ).json()
    permalink = info.get("file", {}).get("permalink")

gdrive_link = "https://drive.google.com/your/file"  # replace as needed

requests.post(
    "https://slack.com/api/chat.postMessage",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    data=json.dumps({
        "channel": channel_id,
        "blocks": [{
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"Anagha Jaysankar's resume uploaded successfully! <{permalink}|(Slack link)> <{gdrive_link}|(gDrive link)>"}
        }],
        "text": "Resume submitted successfully!",
        "unfurl_links": False,
        "unfurl_media": True
    }),
)
```

---

## Alternate Methods

- **curl** (V2)

```bash
curl -F file=@./resume.pdf \
  -F channel_id=C0123456789 \
  -F initial_comment="Submitting my resume" \
  -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  https://slack.com/api/files.uploadV2
```

- **curl** (external flow)

Use the API calls above via `curl` or Postman: first `files.getUploadURLExternal`, then PUT to `upload_url`, then `files.completeUploadExternal`.

---

## Troubleshooting / Learnings

- **not_in_channel**: Invite the app/bot to the channel or grant `channels:join` and reinstall.
- **invalid_auth/missing_scope**: Ensure a Bot token (`xoxb-...`) with `files:write`, `files:read`, and `chat:write`, then reinstall after scope changes.
- **permalink missing**: Ensure `files:read` is granted so `files.info` can return the permalink.
- **method_deprecated / unknown_method**: Workspace doesn’t support the attempted method; the script falls back automatically.
- **Rate limits (429)**: Backoff and honor `Retry-After`.

