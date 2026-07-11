#!/usr/bin/env python3
"""
SkyX-Arch Telegram Release Post Generator.

Loads a YAML release configuration and an HTML template, replaces
{{PLACEHOLDER}} tokens, and writes:

    <output-dir>/message.html   - final HTML text for Telegram
    <output-dir>/buttons.json   - Telegram inline_keyboard JSON
    <output-dir>/meta.json      - flags consumed by send_telegram_post.py
                                  (image_enabled, image_path, pin_message)

--------------------------------------------------------------------------
Two sources of placeholder data
--------------------------------------------------------------------------

1. Static fields written directly in the YAML config (rom_name, website,
   image, pin_message, known_issues, buttons, ...). Any scalar/nested field
   here automatically becomes {{FIELD_NAME}} - adding a new one does NOT
   require changing this script. `known_issues` is a good example of a
   field that is NOT available anywhere else (an OTA server has no concept
   of "known issues"), so it is always maintained by hand in the config -
   it never comes from the source JSON below.

2. An optional `source:` block that reads a *separate* JSON file already
   committed to the repo (e.g. an OTA-server style file such as
   telegram/data/plato.json) and pulls build-specific data out of it:
   version, download link, checksums, size, build timestamp, etc.

   Different ROMs/OTA servers name these fields differently (one calls the
   download link "download", another calls it "url"). To stay adaptive,
   `source.fields` is a MAPPING you control per config file:

       source:
         path: telegram/data/plato.json   # JSON file, already in the repo
         list_key: response                # top-level key holding a list (optional)
         index: 0                          # which list entry to use (optional, default 0)
         fields:
           download_url: download          # <- generic name : actual key in THIS json
           version: version
           build_date:
             key: timestamp
             transform: unix_timestamp_date
           size:
             key: size
             transform: bytes_to_human

   Each entry in `fields` becomes a placeholder named after its generic
   name, e.g. `download_url` -> {{DOWNLOAD_URL}}. If another ROM's JSON
   calls the download link "url" instead of "download", you only change
   `download_url: download` to `download_url: url` in that ROM's config -
   no Python code changes needed.

   Supported transforms (see TRANSFORMS below):
     - `unix_timestamp_date` - unix seconds -> "YYYY-MM-DD"
     - `bytes_to_human`      - byte count -> "1.80 GB"
     - `github_release_page` - a GitHub release *asset* download URL
       (".../releases/download/{tag}/{asset}") -> the release *page* URL
       (".../releases/tag/{tag}")
     - `github_release_asset` - rewrites a GitHub release asset download
       URL to point at a *different* file from the same release/tag.
       Requires `arg: <filename>`, e.g.:

           boot_img_url:
             key: download          # any asset URL from the same release
             transform: github_release_asset
             arg: boot.img

   New transforms can be added to TRANSFORMS if a future ROM needs one.

   Static config fields always win if both define the same placeholder
   name (e.g. if you still set `build_date:` directly in the YAML, that
   takes priority over a sourced `build_date`).

Usage:
    python3 scripts/generate_telegram_post.py \
        --config telegram/config/release.yml \
        --template telegram/templates/release.html \
        --output-dir telegram/output
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


class ConfigError(Exception):
    """Raised when the release configuration, source data, or template is invalid."""


# ---------------------------------------------------------------------------
# YAML config loading
# ---------------------------------------------------------------------------

def load_yaml(path):
    if not os.path.isfile(path):
        raise ConfigError(f"Configuration file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise ConfigError(f"Failed to parse YAML '{path}': {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Configuration root in '{path}' must be a YAML mapping.")
    return data


def validate_static_config(config):
    """Validates the fields that must always be set directly in the YAML config."""
    errors = []

    if not config.get("rom_name"):
        errors.append("missing required field 'rom_name'")

    website = config.get("website")
    if not isinstance(website, dict) or not website.get("url"):
        errors.append("missing required field 'website.url'")

    buttons = config.get("buttons")
    if not isinstance(buttons, list) or len(buttons) == 0:
        errors.append("missing required field 'buttons' (must be a non-empty list)")
    else:
        for i, btn in enumerate(buttons):
            if not isinstance(btn, dict) or not btn.get("text") or not btn.get("url"):
                errors.append(f"button #{i + 1} must have both 'text' and 'url'")

    if errors:
        raise ConfigError("Invalid release configuration:\n  - " + "\n  - ".join(errors))


def validate_resolved(placeholders):
    """Validates fields that may come either from the static config or from `source:`."""
    errors = []
    for key in ("DEVICE", "BUILD_DATE"):
        if not placeholders.get(key):
            errors.append(
                f"missing value for '{key.lower()}' - set it directly in the config "
                f"or map it from source.fields.{key.lower()}"
            )
    if errors:
        raise ConfigError("Invalid release configuration:\n  - " + "\n  - ".join(errors))


# ---------------------------------------------------------------------------
# Static placeholder flattening ({{ROM_NAME}}, {{WEBSITE_URL}}, ...)
# ---------------------------------------------------------------------------

def flatten(d, parent_key=""):
    """Flattens nested dict keys into FLAT_UPPER_CASE placeholder names."""
    items = {}
    for k, v in d.items():
        new_key = f"{parent_key}_{k}" if parent_key else k
        if isinstance(v, dict):
            items.update(flatten(v, new_key))
        elif isinstance(v, list):
            # Lists need dedicated rendering logic (see render_known_issues);
            # they are intentionally skipped here.
            continue
        else:
            items[new_key.upper()] = v
    return items


def render_known_issues(issues):
    if not issues:
        return "• None"
    return "\n".join(f"• {issue}" for issue in issues)


# ---------------------------------------------------------------------------
# Source JSON (OTA-style build metadata) - adaptive field mapping
# ---------------------------------------------------------------------------

def bytes_to_human(value, arg=None):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024.0:
            return f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} PB"


def unix_timestamp_date(value, arg=None):
    return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%d")


def _parse_github_release_asset_url(value):
    """
    Parses a GitHub release *asset* download URL of the form:
        https://github.com/{owner}/{repo}/releases/download/{tag}/{asset}
    Returns (owner, repo, tag).
    """
    parsed = urlparse(str(value))
    if parsed.netloc != "github.com":
        raise ValueError(f"not a github.com URL: {value}")
    parts = [p for p in parsed.path.split("/") if p]
    # owner, repo, "releases", "download", tag, asset
    if len(parts) < 6 or parts[2] != "releases" or parts[3] != "download":
        raise ValueError(f"not a GitHub release asset download URL: {value}")
    owner, repo, tag = parts[0], parts[1], parts[4]
    return owner, repo, tag


def github_release_page(value, arg=None):
    """Turns a release *asset* download URL into the release *page* URL."""
    owner, repo, tag = _parse_github_release_asset_url(value)
    return f"https://github.com/{owner}/{repo}/releases/tag/{tag}"


def github_release_asset(value, arg):
    """
    Turns a release asset download URL into the download URL of a
    *different* asset from the same release/tag. `arg` is the target
    asset's filename, e.g. "boot.img".
    """
    if not arg:
        raise ValueError("github_release_asset requires 'arg' (the target asset filename), e.g. arg: boot.img")
    owner, repo, tag = _parse_github_release_asset_url(value)
    return f"https://github.com/{owner}/{repo}/releases/download/{tag}/{arg}"


TRANSFORMS = {
    "unix_timestamp_date": unix_timestamp_date,
    "bytes_to_human": bytes_to_human,
    "github_release_page": github_release_page,
    "github_release_asset": github_release_asset,
}


def load_json(path):
    if not os.path.isfile(path):
        raise ConfigError(f"Source data file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Failed to parse source JSON '{path}': {exc}") from exc


def resolve_source_node(data, list_key, index):
    """Navigates {list_key -> [index]} to reach the object holding the build fields."""
    node = data
    if list_key:
        if not isinstance(node, dict) or list_key not in node:
            raise ConfigError(f"Source JSON has no top-level key '{list_key}'")
        node = node[list_key]
    if isinstance(node, list):
        if not node:
            raise ConfigError("Source JSON list is empty - no build entry to read")
        try:
            node = node[index]
        except IndexError:
            raise ConfigError(f"Source JSON list has no entry at index {index}")
    if not isinstance(node, dict):
        raise ConfigError("Resolved source JSON node is not an object")
    return node


def build_source_placeholders(config):
    """
    Reads config['source'] (if present) and returns a dict of
    UPPER_CASE placeholder -> value, using the user-defined field mapping.
    Returns {} if no 'source' block is configured (fully backward compatible).

    source.path is resolved relative to the current working directory
    (i.e. the repo root, since the workflow runs from there after checkout) -
    not relative to the config file's own location.
    """
    source_cfg = config.get("source")
    if not source_cfg:
        return {}

    path = source_cfg.get("path")
    if not path:
        raise ConfigError("'source.path' is required when a 'source' block is set")

    field_map = source_cfg.get("fields") or {}
    if not field_map:
        raise ConfigError("'source.fields' must define at least one field mapping")

    data = load_json(path)
    node = resolve_source_node(data, source_cfg.get("list_key"), source_cfg.get("index", 0))

    placeholders = {}
    for generic_name, spec in field_map.items():
        if isinstance(spec, dict):
            source_key = spec.get("key")
            transform_name = spec.get("transform")
            transform_arg = spec.get("arg")
        else:
            source_key = spec
            transform_name = None
            transform_arg = None

        if not source_key or source_key not in node:
            print(
                f"WARNING: source field '{generic_name}' (key '{source_key}') "
                f"not found in {path}, leaving it empty",
                file=sys.stderr,
            )
            value = ""
        else:
            value = node[source_key]
            if transform_name:
                transform = TRANSFORMS.get(transform_name)
                if not transform:
                    raise ConfigError(
                        f"Unknown transform '{transform_name}' for source field '{generic_name}'. "
                        f"Available transforms: {', '.join(sorted(TRANSFORMS))}"
                    )
                try:
                    value = transform(value, transform_arg)
                except Exception as exc:  # noqa: BLE001
                    raise ConfigError(
                        f"Failed to apply transform '{transform_name}' to field '{generic_name}': {exc}"
                    ) from exc

        placeholders[generic_name.upper()] = value

    return placeholders


# ---------------------------------------------------------------------------
# Placeholder substitution
# ---------------------------------------------------------------------------

def apply_placeholders(text, placeholders):
    def replace(match):
        key = match.group(1).strip()
        if key not in placeholders:
            print(f"WARNING: placeholder '{{{{{key}}}}}' has no value, replacing with empty string", file=sys.stderr)
            return ""
        return str(placeholders[key])

    return re.sub(r"\{\{\s*([A-Z0-9_]+)\s*\}\}", replace, text)


def build_inline_keyboard(buttons, per_row=2):
    rows, row = [], []
    for btn in buttons:
        row.append({"text": btn["text"], "url": btn["url"]})
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


def resolve_image(config):
    image_cfg = config.get("image") or {}
    path = image_cfg.get("path", "")
    enabled = bool(image_cfg.get("enabled")) and bool(path) and os.path.isfile(path)
    return enabled, (path if enabled else "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate a Telegram release post from a YAML config + HTML template.")
    parser.add_argument("--config", default="telegram/config/release.yml", help="Path to release YAML config")
    parser.add_argument("--template", default="telegram/templates/release.html", help="Path to HTML template")
    parser.add_argument("--output-dir", default="telegram/output", help="Directory to write message.html / buttons.json / meta.json")
    args = parser.parse_args()

    try:
        config = load_yaml(args.config)
        validate_static_config(config)

        # 1) Optional adaptive data pulled from a separate JSON file already in
        #    the repo (e.g. an OTA json such as telegram/data/plato.json).
        source_placeholders = build_source_placeholders(config)

        # 2) Static fields written directly in the YAML config. These always
        #    win over sourced values if both define the same placeholder name.
        config_for_flatten = {k: v for k, v in config.items() if k != "source"}
        static_placeholders = flatten(config_for_flatten)

        placeholders = {**source_placeholders, **static_placeholders}
        # known_issues is never sourced from JSON - it's hand-maintained in the config.
        placeholders["KNOWN_ISSUES"] = render_known_issues(config.get("known_issues"))

        validate_resolved(placeholders)

        if not os.path.isfile(args.template):
            raise ConfigError(f"Template file not found: {args.template}")
        with open(args.template, "r", encoding="utf-8") as f:
            template_text = f.read()

        message_html = apply_placeholders(template_text, placeholders)

        # Buttons can also reference placeholders, e.g. url: "{{DOWNLOAD_URL}}",
        # so each release automatically links to the right build artifact.
        resolved_buttons = [
            {
                "text": apply_placeholders(btn["text"], placeholders),
                "url": apply_placeholders(btn["url"], placeholders),
            }
            for btn in config.get("buttons", [])
        ]
        keyboard = build_inline_keyboard(resolved_buttons)

        image_enabled, image_path = resolve_image(config)

        os.makedirs(args.output_dir, exist_ok=True)

        message_path = os.path.join(args.output_dir, "message.html")
        buttons_path = os.path.join(args.output_dir, "buttons.json")
        meta_path = os.path.join(args.output_dir, "meta.json")

        with open(message_path, "w", encoding="utf-8") as f:
            f.write(message_html)

        with open(buttons_path, "w", encoding="utf-8") as f:
            json.dump(keyboard, f, ensure_ascii=False, indent=2)

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "image_enabled": image_enabled,
                    "image_path": image_path,
                    "pin_message": bool(config.get("pin_message", False)),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

        print(f"OK: generated {message_path}")
        print(f"OK: generated {buttons_path}")
        print(f"OK: generated {meta_path}")

    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: unexpected failure: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
