#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

# API URLs
UPLOAD_V2_URL = "https://slack.com/api/files.uploadV2"
JOIN_URL = "https://slack.com/api/conversations.join"
GET_UPLOAD_URL = "https://slack.com/api/files.getUploadURLExternal"
COMPLETE_UPLOAD_URL = "https://slack.com/api/files.completeUploadExternal"
POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


def error_exit(msg: str, code: int = 1):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(code)


def validate_pdf(path: Path):
    if not path.exists():
        error_exit(f"File not found: {path}")
    if not path.is_file():
        error_exit(f"Path is not a file: {path}")
    if path.suffix.lower() != ".pdf":
        error_exit("Only PDF files are accepted (use a .pdf)")


def load_config():
    """Load token and channel from config.json or .env"""
    token = None
    channel = None

    config_path = Path("config.json")
    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
                token = cfg.get("SLACK_BOT_TOKEN") or cfg.get("slack_bot_token")
                channel = cfg.get("SLACK_CHANNEL_ID") or cfg.get("slack_channel_id")
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


def _debug_request(name: str, method: str, url: str, headers: dict, data: dict = None, files: dict = None):
    safe = {
        "name": name,
        "method": method,
        "url": url,
        "headers": _masked_headers(headers),
    }
    if data is not None:
        safe["data"] = data
    if files:
        meta = {}
        for k, v in files.items():
            meta[k] = {"filename": getattr(v, "name", "<binary>")}
        safe["files"] = meta
    print("== DEBUG HTTP REQUEST ==")
    print(json.dumps(safe, indent=2))


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

    if payload.get("ok") or payload.get("error") == "already_in_channel":
        return True

    err = payload.get("error", "unknown_error")
    if err == "missing_scope":
        print("Hint: Add 'channels:join' scope to the bot and reinstall the app.", file=sys.stderr)
    elif err in ("method_not_supported_for_channel_type", "channel_not_found"):
        print("Hint: If this is a private channel, invite the bot manually.", file=sys.stderr)
    else:
        print(f"Warning: conversations.join failed: {payload}", file=sys.stderr)
    return False


def external_upload_flow(token: str, channel_id: str, file_path: Path, initial_comment: str = None, debug: bool = False):
    """External upload flow (for files.uploadV2 fallback)"""
    size = file_path.stat().st_size
    headers = {"Authorization": f"Bearer {token}"}

    # Step 1: get upload URL
    data1 = {"filename": file_path.name, "length": str(size)}
    if debug:
        _debug_request("files.getUploadURLExternal", "POST", GET_UPLOAD_URL, headers, data1)
    resp1 = requests.post(GET_UPLOAD_URL, headers=headers, data=data1, timeout=30)
    if resp1.status_code != 200:
        error_exit(f"HTTP {resp1.status_code} from getUploadURLExternal: {resp1.text}")
    try:
        p1 = resp1.json()
    except json.JSONDecodeError:
        error_exit("getUploadURLExternal returned non-JSON response")
    if not p1.get("ok"):
        error_exit(f"Slack API error (getUploadURLExternal): {p1}")
    upload_url = p1.get("upload_url")
    file_id = p1.get("file_id")
    if not upload_url or not file_id:
        error_exit(f"Missing upload_url or file_id in response: {p1}")

    # Step 2: PUT file bytes
    put_headers = {"Content-Type": "application/octet-stream", "Content-Length": str(size)}
    if debug:
        _debug_request("PUT upload bytes", "PUT", upload_url, put_headers, {"length": size})
    with file_path.open("rb") as f:
        put_resp = requests.put(upload_url, data=f, headers=put_headers, timeout=60)
    if put_resp.status_code not in (200, 201):
        error_exit(f"Upload to provided URL failed: HTTP {put_resp.status_code} {put_resp.text}")

    # Step 3: complete upload
    complete_payload = {"files": [{"id": file_id, "title": file_path.name}], "channel_id": channel_id}
    if initial_comment:
        complete_payload["initial_comment"] = initial_comment
    headers3 = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    if debug:
        _debug_request("files.completeUploadExternal", "POST", COMPLETE_UPLOAD_URL, headers3, complete_payload)
    resp3 = requests.post(COMPLETE_UPLOAD_URL, headers=headers3, data=json.dumps(complete_payload), timeout=30)
    if resp3.status_code != 200:
        error_exit(f"HTTP {resp3.status_code} from completeUploadExternal: {resp3.text}")
    try:
        p3 = resp3.json()
    except json.JSONDecodeError:
        error_exit("completeUploadExternal returned non-JSON response")
    if not p3.get("ok"):
        error_exit(f"Slack API error (completeUploadExternal): {p3}")

    # Return the permalink for posting a visible message
    file_obj = p3.get("files", [{}])[0]
    return file_obj.get("permalink")


def post_file_link(token: str, channel_id: str, file_permalink: str, debug: bool = False):
    """Post a message with a clickable resume link in Slack Markdown format"""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # Slack Markdown-style clickable link
    text = f"<{file_permalink}|Anagha Jayasankar's resume> submitted successfully!"
    payload = {"channel": channel_id, "text": text, "mrkdwn": True}
    if debug:
        _debug_request("chat.postMessage", "POST", POST_MESSAGE_URL, headers, payload)
    resp = requests.post(POST_MESSAGE_URL, headers=headers, data=json.dumps(payload), timeout=15)
    rj = resp.json()
    if not rj.get("ok"):
        print("Warning: Failed to post message:", rj)
    else:
        print("Message with resume link posted successfully.")


def upload_pdf_v2(token: str, channel_id: str, file_path: Path, initial_comment: str = None, debug: bool = False):
    """Upload via files.uploadV2 (modern flow)"""
    headers = {"Authorization": f"Bearer {token}"}
    data = {"channel_id": channel_id}
    if initial_comment:
        data["initial_comment"] = initial_comment

    with file_path.open("rb") as f:
        files = {"file": (file_path.name, f, "application/pdf")}
        if debug:
            _debug_request("files.uploadV2", "POST", UPLOAD_V2_URL, headers, data, files)
        resp = requests.post(UPLOAD_V2_URL, headers=headers, data=data, files=files, timeout=30)

    if resp.status_code != 200:
        error_exit(f"HTTP {resp.status_code}: {resp.text}")

    try:
        payload = resp.json()
    except json.JSONDecodeError:
        error_exit("Slack returned non-JSON response")

    file_permalink = None
    if payload.get("ok") and payload.get("file"):
        file_permalink = payload["file"].get("permalink")
    else:
        # Fallback to external upload flow
        print("UploadV2 failed or deprecated, using external upload flow...")
        file_permalink = external_upload_flow(token, channel_id, file_path, initial_comment, debug)

    if not file_permalink:
        error_exit("Failed to obtain file permalink after upload")

    # Post a clickable link message
    post_file_link(token, channel_id, file_permalink, debug=debug)



def parse_args():
    p = argparse.ArgumentParser(description="Upload a PDF to a Slack channel")
    p.add_argument("--file", required=True, help="Path to the PDF file to upload")
    p.add_argument("--channel", help="Slack channel ID (overrides config.json/.env)")
    p.add_argument("--comment", help="Optional initial comment")
    p.add_argument("--debug", action="store_true", help="Print debug HTTP requests")
    return p.parse_args()


def main():
    args = parse_args()
    token_env, channel_env = load_config()

    token = token_env
    if not token:
        error_exit("SLACK_BOT_TOKEN is not set.")

    channel_id = args.channel or channel_env
    if not channel_id:
        error_exit("Channel ID is missing.")

    file_path = Path(args.file).expanduser().resolve()
    validate_pdf(file_path)

    # Join public channel if possible
    try_join_channel(token, channel_id, debug=args.debug)

    # Upload file and post a clickable message
    upload_pdf_v2(token, channel_id, file_path, initial_comment=args.comment, debug=args.debug)


if __name__ == "__main__":
    main()
