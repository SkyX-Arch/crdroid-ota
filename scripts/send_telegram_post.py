#!/usr/bin/env python3
"""
Sends a generated Telegram release post (message.html + buttons.json + meta.json)
via the Telegram Bot API, and pins it if the config requested that.

Reads credentials ONLY from environment variables (populated from GitHub
Secrets by the workflow) - never from files, never printed to logs:

    TELEGRAM_TOKEN        (required)
    TELEGRAM_CHAT_ID      (required)
    TELEGRAM_THREAD_ID    (optional - for posting into a specific forum topic)

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


class TelegramError(Exception):
    pass


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


def send_post(token, chat_id, thread_id, message_html, keyboard, image_path):
    data = {
        "chat_id": chat_id,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(keyboard, ensure_ascii=False),
    }
    if thread_id:
        data["message_thread_id"] = thread_id

    if image_path:
        data["caption"] = message_html
        with open(image_path, "rb") as photo_file:
            result = call_api(token, "sendPhoto", data=data, files={"photo": photo_file})
    else:
        data["text"] = message_html
        result = call_api(token, "sendMessage", data=data)

    return result["message_id"]


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
