#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

# Slack API URLs
UPLOAD_V2_URL = "https://slack.com/api/files.uploadV2"
JOIN_URL = "https://slack.com/api/conversations.join"
GET_UPLOAD_URL = "https://slack.com/api/files.getUploadURLExternal"
COMPLETE_UPLOAD_URL = "https://slack.com/api/files.completeUploadExternal"
POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
FILES_INFO_URL = "https://slack.com/api/files.info"

def error_exit(msg: str, code: int = 1):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(code)

def validate_pdf(path: Path):
    if not path.exists():
        error_exit(f"File not found: {path}")
    if not path.is_file():
        error_exit(f"Path is not a file: {path}")
    if path.suffix.lower() != ".pdf":
        error_exit("Only PDF files are accepted (use .pdf)")

def load_config():
    token = None
    channel = None

    config_path = Path("config.json")
    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
                token = cfg.get("SLACK_BOT_TOKEN")
                channel = cfg.get("SLACK_CHANNEL_ID")
        except Exception as e:
            error_exit(f"Failed to read config.json: {e}")

    if not token or not channel:
        load_dotenv(override=False)
        token = token or os.getenv("SLACK_BOT_TOKEN")
        channel = channel or os.getenv("SLACK_CHANNEL_ID")

    return token, channel

def _masked_headers(headers: dict) -> dict:
    if not headers:
        return {}
    out = dict(headers)
    auth = out.get("Authorization")
    if auth and isinstance(auth, str) and auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1]
        out["Authorization"] = f"Bearer {token[:10]}..."
    return out

def _debug_request(name: str, method: str, url: str, headers: dict | None, data: dict | None = None, files: dict | None = None):
    safe = {"name": name, "method": method, "url": url, "headers": _masked_headers(headers or {})}
    if data is not None:
        safe["data"] = data
    if files:
        meta = {}
        for k, v in files.items():
            if isinstance(v, tuple) and len(v) >= 3:
                filename, _fh, content_type = v[:3]
                size = None
                try:
                    if hasattr(_fh, 'name'):
                        size = Path(_fh.name).stat().st_size
                except Exception:
                    size = None
                meta[k] = {"filename": filename, "content_type": content_type, "size_bytes": size}
            else:
                meta[k] = "<binary>"
        safe["files"] = meta
    print("== DEBUG HTTP REQUEST ==")
    print(json.dumps(safe, indent=2))

def get_file_permalink(token: str, file_id: str, debug: bool = False) -> str | None:
    headers = {"Authorization": f"Bearer {token}"}
    params = {"file": file_id}
    if debug:
        _debug_request("files.info", "GET", FILES_INFO_URL, headers, params)
    resp = requests.get(FILES_INFO_URL, headers=headers, params=params, timeout=15)
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except json.JSONDecodeError:
        return None
    if not data.get("ok"):
        return None
    file_obj = data.get("file") or {}
    return file_obj.get("permalink")

def try_join_channel(token: str, channel_id: str, debug: bool = False) -> bool:
    headers = {"Authorization": f"Bearer {token}"}
    data = {"channel": channel_id}
    if debug:
        _debug_request("conversations.join", "POST", JOIN_URL, headers, data)
    resp = requests.post(JOIN_URL, headers=headers, data=data, timeout=15)

    if resp.status_code != 200:
        print(f"Warning: conversations.join HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
        return False

    try:
        payload = resp.json()
    except json.JSONDecodeError:
        print("Warning: conversations.join returned non-JSON response", file=sys.stderr)
        return False

    if payload.get("ok"):
        return True

    err = payload.get("error", "unknown_error")
    if err == "already_in_channel":
        return True
    if err == "missing_scope":
        print("Hint: Add 'channels:join' scope to the bot and reinstall the app to allow auto-join.", file=sys.stderr)
    elif err in ("method_not_supported_for_channel_type", "channel_not_found"):
        print("Hint: If this is a private channel, invite the bot to the channel in Slack.", file=sys.stderr)
    else:
        print(f"Warning: conversations.join failed: {payload}", file=sys.stderr)
    return False

def post_file_link(token: str, channel_id: str, slack_file_permalink: str, debug: bool = False):
    """
    Post a message with Slack and Google Drive links.
    - Slack file will expand visually.
    - Google Drive link remains clickable without a preview.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Static Google Drive link
    gdrive_link = "https://drive.google.com/file/d/1w5nf0goS7y2OOruq2vp-mXnQ2JUVDOvw/view"

    # Markdown text for the block
    block_text = (
        f"Anagha Jaysankar's resume uploaded successfully! "
        f"<{slack_file_permalink}|(Slack link)> "
        f"<{gdrive_link}|(gDrive link)>"
    )

    payload = {
        "channel": channel_id,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",  # ensures clickable links
                    "text": block_text
                }
            }
        ],
        # Fallback text for clients not supporting blocks
        "text": "Resume submitted successfully! Links: Slack resume / gDrive resume",
        "unfurl_links": False,  # prevent GDrive link from previewing
        "unfurl_media": True     # allow Slack file preview
    }

    if debug:
        _debug_request("chat.postMessage", "POST", POST_MESSAGE_URL, headers, payload)

    resp = requests.post(POST_MESSAGE_URL, headers=headers, data=json.dumps(payload), timeout=15)
    rj = resp.json()
    if not rj.get("ok"):
        print("Warning: Failed to post file message:", rj)
    else:
        print("Message with resume links posted successfully.")

def external_upload_flow(token: str, channel_id: str, file_path: Path, initial_comment: str | None, debug: bool = False):
    size = file_path.stat().st_size
    headers = {"Authorization": f"Bearer {token}"}

    # Step 1: get upload URL
    data1 = {"filename": file_path.name, "length": str(size)}
    if debug:
        _debug_request("files.getUploadURLExternal", "POST", GET_UPLOAD_URL, headers, data1)
    resp1 = requests.post(GET_UPLOAD_URL, headers=headers, data=data1, timeout=30)
    resp1.raise_for_status()
    p1 = resp1.json()
    if not p1.get("ok"):
        error_exit(f"Slack API error (getUploadURLExternal): {p1}")

    upload_url = p1.get("upload_url")
    file_id = p1.get("file_id")
    if not upload_url or not file_id:
        error_exit(f"Missing upload_url or file_id in response: {p1}")

    # Step 2: PUT the file bytes
    with file_path.open("rb") as f:
        put_resp = requests.put(upload_url, data=f, headers={"Content-Type": "application/octet-stream"})
    if put_resp.status_code not in (200, 201):
        error_exit(f"Upload to provided URL failed: HTTP {put_resp.status_code} {put_resp.text}")

    # Step 3: complete upload
    complete_payload = {"files": [{"id": file_id, "title": file_path.name}], "channel_id": channel_id}
    headers3 = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if debug:
        _debug_request("files.completeUploadExternal", "POST", COMPLETE_UPLOAD_URL, headers3, complete_payload)
    resp3 = requests.post(COMPLETE_UPLOAD_URL, headers=headers3, data=json.dumps(complete_payload), timeout=30)
    resp3.raise_for_status()
    p3 = resp3.json()
    if not p3.get("ok"):
        error_exit(f"Slack API error (completeUploadExternal): {p3}")

    # Post Slack file preview + GDrive link
    file_obj = p3["files"][0] if isinstance(p3.get("files"), list) else p3.get("file")
    permalink = file_obj.get("permalink") or get_file_permalink(token, file_obj["id"], debug)
    post_file_link(token, channel_id, permalink or file_obj["id"], debug)

    return file_obj

def upload_pdf_v2(token: str, channel_id: str, file_path: Path, initial_comment: str | None, debug: bool = False):
    headers = {"Authorization": f"Bearer {token}"}
    data = {"channel_id": channel_id}
    if initial_comment:
        data["initial_comment"] = initial_comment
    with file_path.open("rb") as f:
        files = {"file": (file_path.name, f, "application/pdf")}
        if debug:
            _debug_request("files.uploadV2", "POST", UPLOAD_V2_URL, headers, data, files)
        resp = requests.post(UPLOAD_V2_URL, headers=headers, data=data, files=files, timeout=30)

    if resp.status_code != 200 or not resp.json().get("ok"):
        print("UploadV2 failed or deprecated, using external upload flow...")
        return external_upload_flow(token, channel_id, file_path, initial_comment, debug)

    payload = resp.json()
    file_obj = payload["files"][0] if isinstance(payload.get("files"), list) else payload.get("file")
    # Post Slack file preview + GDrive link using correct permalink
    permalink = file_obj.get("permalink") or get_file_permalink(token, file_obj["id"], debug)
    post_file_link(token, channel_id, permalink or file_obj["id"], debug)
    print("Upload successful (V2).")
    return file_obj

def parse_args():
    p = argparse.ArgumentParser(description="Upload a PDF to a Slack channel")
    p.add_argument("--file", required=True, help="Path to the PDF file to upload")
    p.add_argument("--channel", help="Slack channel ID (overrides config)")
    p.add_argument("--comment", help="Optional initial comment")
    p.add_argument("--debug", action="store_true", help="Print HTTP request payloads")
    return p.parse_args()

def main():
    args = parse_args()
    token_env, channel_env = load_config()
    token = token_env or error_exit("SLACK_BOT_TOKEN not set")
    channel_id = args.channel or channel_env or error_exit("Channel ID not set")

    file_path = Path(args.file).expanduser().resolve()
    validate_pdf(file_path)

    try_join_channel(token, channel_id, debug=args.debug)
    upload_pdf_v2(token, channel_id, file_path, initial_comment=args.comment, debug=args.debug)

if __name__ == "__main__":
    main()
