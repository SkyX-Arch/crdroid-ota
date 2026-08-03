#!/usr/bin/env python3
"""
Sends a generated Telegram release post (message.html + buttons.json + meta.json)
via the Telegram Bot API, and pins it if the config requested that.

Reads credentials ONLY from environment variables (populated from GitHub
Secrets by the workflow) - never from files, never printed to logs:

    TELEGRAM_TOKEN        (required)
    TELEGRAM_CHAT_ID      (required)
    TELEGRAM_THREAD_ID    (optional - for posting into a specific forum topic)

Telegram character limits are handled automatically:
    - photo caption over 1024 chars -> image is sent WITHOUT a caption,
      followed by the full text as separate message(s)
    - message text over 4096 chars  -> split into multiple messages at
      paragraph (blank-line) boundaries, so no HTML tag is ever cut in half
In both cases only the LAST message carries the inline keyboard, and its
message_id is what gets pinned.

Usage:
    python3 scripts/send_telegram_post.py --output-dir telegram/output
"""

import argparse
import json
import os
import sys

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

API_BASE = "https://api.telegram.org/bot{token}/{method}"

# Telegram Bot API hard limits (as of this writing):
#   - sendMessage "text":     4096 characters
#   - sendPhoto   "caption":  1024 characters
# Exceeding these makes the API call fail outright, so we check ourselves
# rather than let the request error out mid-release.
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024


class TelegramError(Exception):
    pass


def split_html_message(text, limit):
    """
    Splits `text` into chunks that each fit within `limit` characters,
    breaking ONLY at blank-line (paragraph) boundaries - never in the
    middle of a line - so an HTML tag is never split across two messages.

    If a single paragraph is itself longer than `limit`, it's emitted
    as its own (oversized) chunk rather than being cut mid-tag; that one
    send will fail with a clear Telegram API error, which is the signal
    to shorten that paragraph in the config (e.g. trim known_issues).
    """
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = para

    if current:
        chunks.append(current)

    return chunks or [""]


def get_required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise TelegramError(f"Missing required secret/environment variable: {name}")
    return value


def call_api(token, method, data=None, files=None):
    url = API_BASE.format(token=token, method=method)
    try:
        response = requests.post(url, data=data, files=files, timeout=30)
    except requests.RequestException as exc:
        raise TelegramError(f"Telegram API request failed ({method}): network error") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise TelegramError(f"Telegram API request failed ({method}): non-JSON response (status {response.status_code})") from exc

    if not payload.get("ok"):
        description = payload.get("description", "unknown error")
        raise TelegramError(f"Telegram API request failed ({method}): {description}")

    return payload["result"]


def send_photo_without_caption(token, chat_id, thread_id, image_path):
    data = {"chat_id": chat_id}
    if thread_id:
        data["message_thread_id"] = thread_id
    with open(image_path, "rb") as photo_file:
        result = call_api(token, "sendPhoto", data=data, files={"photo": photo_file})
    return result["message_id"]


def send_text_chunks(token, chat_id, thread_id, chunks, keyboard):
    """Sends one or more sendMessage calls; only the LAST chunk gets the
    inline keyboard, since that's the one worth pinning/clicking."""
    last_id = None
    for i, chunk in enumerate(chunks):
        data = {
            "chat_id": chat_id,
            "parse_mode": "HTML",
            "text": chunk,
        }
        if thread_id:
            data["message_thread_id"] = thread_id
        if i == len(chunks) - 1:
            data["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
        result = call_api(token, "sendMessage", data=data)
        last_id = result["message_id"]
    return last_id


def send_post(token, chat_id, thread_id, message_html, keyboard, image_path):
    if image_path:
        if len(message_html) <= TELEGRAM_CAPTION_LIMIT:
            data = {
                "chat_id": chat_id,
                "parse_mode": "HTML",
                "caption": message_html,
                "reply_markup": json.dumps(keyboard, ensure_ascii=False),
            }
            if thread_id:
                data["message_thread_id"] = thread_id
            with open(image_path, "rb") as photo_file:
                result = call_api(token, "sendPhoto", data=data, files={"photo": photo_file})
            return result["message_id"]

        # Caption would be rejected by Telegram (over the 1024-char limit).
        # Send the image on its own, then the full text as separate
        # message(s) - nothing gets silently cut or dropped.
        print(
            f"WARNING: message is {len(message_html)} chars, over Telegram's "
            f"{TELEGRAM_CAPTION_LIMIT}-char photo caption limit - sending the "
            f"image without a caption, then the full text as separate message(s)",
            file=sys.stderr,
        )
        send_photo_without_caption(token, chat_id, thread_id, image_path)
        chunks = split_html_message(message_html, TELEGRAM_TEXT_LIMIT)
        return send_text_chunks(token, chat_id, thread_id, chunks, keyboard)

    chunks = split_html_message(message_html, TELEGRAM_TEXT_LIMIT)
    if len(chunks) > 1:
        print(
            f"WARNING: message is {len(message_html)} chars, over Telegram's "
            f"{TELEGRAM_TEXT_LIMIT}-char message limit - splitting into {len(chunks)} messages",
            file=sys.stderr,
        )
    return send_text_chunks(token, chat_id, thread_id, chunks, keyboard)


def pin_message(token, chat_id, message_id):
    call_api(token, "pinChatMessage", data={"chat_id": chat_id, "message_id": message_id})


def main():
    parser = argparse.ArgumentParser(description="Send a generated Telegram release post and pin it.")
    parser.add_argument("--output-dir", default="telegram/output", help="Directory containing message.html / buttons.json / meta.json")
    args = parser.parse_args()

    try:
        token = get_required_env("TELEGRAM_TOKEN")
        chat_id = get_required_env("TELEGRAM_CHAT_ID")
        thread_id = os.environ.get("TELEGRAM_THREAD_ID", "").strip()

        message_path = os.path.join(args.output_dir, "message.html")
        buttons_path = os.path.join(args.output_dir, "buttons.json")
        meta_path = os.path.join(args.output_dir, "meta.json")

        for path in (message_path, buttons_path, meta_path):
            if not os.path.isfile(path):
                raise TelegramError(f"Expected generated file not found: {path}. Run generate_telegram_post.py first.")

        with open(message_path, "r", encoding="utf-8") as f:
            message_html = f.read()
        with open(buttons_path, "r", encoding="utf-8") as f:
            keyboard = json.load(f)
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        image_path = meta.get("image_path") if meta.get("image_enabled") else None
        if image_path and not os.path.isfile(image_path):
            print(f"WARNING: image_path '{image_path}' not found on disk, falling back to text-only message", file=sys.stderr)
            image_path = None

        message_id = send_post(token, chat_id, thread_id, message_html, keyboard, image_path)
        print(f"OK: message sent, message_id={message_id}")

        if meta.get("pin_message"):
            pin_message(token, chat_id, message_id)
            print("OK: message pinned")

    except TelegramError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: unexpected failure: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
