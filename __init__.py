"""Push notifications for the Herald phone app.

The app only hears from Hermes over its own WebSocket, and iOS suspends that
socket within seconds of the app leaving the foreground. So while the phone
is in a pocket a finished reply or a waiting approval goes unnoticed until the
app is opened again. This plugin closes the gap from inside the backend: it
observes ``post_llm_call`` and ``pre_approval_request`` and hands a small
message to Expo's push service, which delivers it through APNs.

What the phone registers
------------------------
The app writes its Expo push token(s) into the DEFAULT profile's
``profile.yaml`` under ``ui_meta["hermes-bot-push"]``::

    ui_meta:
      hermes-bot-push:
        version: 1
        devices:
          <install-id>: { token: "ExponentPushToken[...]", name: "My iPhone", updatedAt: 1788... }
        labels:
          default: "Hermes"
          pr-fixer: "Fixer"

One ``hermes serve`` runs every profile, but each profile's hook runs with its
own ``HERMES_HOME``; reading the default home's file means every bot on the
machine reaches the same phones without per-profile setup.

What gets sent
--------------
Only who finished (or who needs approval) — never the reply text. The banner
is a tap target; the app fetches the real content over its socket when opened.
Turns are filtered to the sessions the app owns (``Bot Chat`` and
``Group: <room>`` titles) so a terminal session on the laptop does not buzz
the phone. Everything runs on a daemon thread, with a short timeout, and
swallows its own failures: a push that cannot go out is never a reason to
disturb a turn.

Install: ``hermes plugins install iamlukethedev/hermes-bot-push --enable``,
then restart ``hermes serve``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_log = logging.getLogger(__name__)

META_KEY = "hermes-bot-push"
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
SEND_TIMEOUT_S = 5.0
# Expo accepts up to 100 messages per request.
BATCH_SIZE = 100
# Session titles the app owns; anything else is a laptop-side session.
BOT_CHAT_TITLE = "Bot Chat"
GROUP_TITLE_PREFIX = "Group: "

_title_cache: Dict[str, Optional[str]] = {}
_title_cache_lock = threading.Lock()
_TITLE_CACHE_MAX = 512

_targets_cache: Dict[str, Any] = {"mtime": None, "value": None}

# Embeddings, overrides, isolates and the LRM / RLM / ALM marks (mirrors the
# app's lib/bidi.ts).
_BIDI_CONTROLS = dict.fromkeys(
    [0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]
)


def isolate_name(name: str) -> str:
    """A name safe to drop into a banner title: bidi controls stripped, then
    wrapped in a first-strong isolate so a Hebrew or Arabic name cannot
    reorder the words around it."""
    return "\u2068" + name.translate(_BIDI_CONTROLS) + "\u2069"


# ── Registered devices ──────────────────────────────────────────────────────


def _default_profile_yaml() -> Path:
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root()) / "profile.yaml"
    except Exception:
        return Path.home() / ".hermes" / "profile.yaml"


def parse_targets(meta: Any) -> Dict[str, Any]:
    """Normalise the ``ui_meta['hermes-bot-push']`` block.

    Returns ``{"tokens": [str, ...], "labels": {profile: label}}``; tolerant of
    a missing or malformed block (no tokens → nothing is ever sent).

    Version 2 of the block adds ``devices.<id>.liveActivity`` — ActivityKit
    tokens for the Hermes Pet Live Activity. Expo's push service cannot
    deliver those, so this plugin deliberately reads only ``token`` and
    ``labels`` and must keep ignoring the rest.
    """
    tokens: List[str] = []
    labels: Dict[str, str] = {}
    if not isinstance(meta, dict):
        return {"tokens": tokens, "labels": labels}
    devices = meta.get("devices")
    if isinstance(devices, dict):
        for device in devices.values():
            token = device.get("token") if isinstance(device, dict) else None
            if isinstance(token, str) and token.startswith("ExponentPushToken[") and token not in tokens:
                tokens.append(token)
    raw_labels = meta.get("labels")
    if isinstance(raw_labels, dict):
        for name, label in raw_labels.items():
            if isinstance(name, str) and isinstance(label, str) and label.strip():
                labels[name] = label.strip()
    return {"tokens": tokens, "labels": labels}


def _load_targets() -> Dict[str, Any]:
    """Read the registered phones, re-parsing only when the file changed."""
    path = _default_profile_yaml()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"tokens": [], "labels": {}}
    if _targets_cache["mtime"] == mtime and _targets_cache["value"] is not None:
        return _targets_cache["value"]
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        ui_meta = data.get("ui_meta") if isinstance(data, dict) else None
        value = parse_targets(ui_meta.get(META_KEY) if isinstance(ui_meta, dict) else None)
    except Exception as exc:
        _log.debug("hermes-bot-push: could not read %s: %s", path, exc)
        value = {"tokens": [], "labels": {}}
    _targets_cache["mtime"] = mtime
    _targets_cache["value"] = value
    return value


# ── Which turn is this ──────────────────────────────────────────────────────


def _resolve_profile() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return str(get_active_profile_name() or "default")
    except Exception:
        return "default"


def _session_title(session_id: str) -> Optional[str]:
    if not session_id:
        return None
    with _title_cache_lock:
        if session_id in _title_cache:
            return _title_cache[session_id]
    title: Optional[str]
    try:
        from hermes_state import SessionDB

        title = SessionDB(read_only=True).get_session_title(session_id)
    except Exception as exc:
        _log.debug("hermes-bot-push: title lookup failed for %s: %s", session_id, exc)
        return None
    with _title_cache_lock:
        if len(_title_cache) >= _TITLE_CACHE_MAX:
            _title_cache.clear()
        _title_cache[session_id] = title
    return title


def classify_session(title: Optional[str]) -> Optional[Dict[str, str]]:
    """Map a session title to the app route that owns it, or None to skip.

    ``None`` for an unknown title means "not ours" — except that an unreadable
    title (``title is None``) is treated as a bot chat so a transient DB hiccup
    drops nothing.
    """
    if title is None:
        return {"route": "chat"}
    if title == BOT_CHAT_TITLE:
        return {"route": "chat"}
    if title.startswith(GROUP_TITLE_PREFIX) and title[len(GROUP_TITLE_PREFIX):].strip():
        return {"route": "group", "group": title[len(GROUP_TITLE_PREFIX):].strip()}
    return None


# ── Messages ────────────────────────────────────────────────────────────────


def build_messages(
    tokens: Iterable[str],
    *,
    title: str,
    data: Dict[str, Any],
    category: Optional[str] = None,
    time_sensitive: bool = False,
) -> List[Dict[str, Any]]:
    """Expo push messages, one per device. No body: the banner names who, not what."""
    messages: List[Dict[str, Any]] = []
    for token in tokens:
        message: Dict[str, Any] = {
            "to": token,
            "title": title,
            "sound": "default",
            "priority": "high",
            "channelId": "default",
            "data": data,
        }
        if category:
            message["categoryId"] = category
        if time_sensitive:
            message["interruptionLevel"] = "timeSensitive"
        messages.append(message)
    return messages


def _post(messages: List[Dict[str, Any]]) -> None:
    for start in range(0, len(messages), BATCH_SIZE):
        chunk = messages[start : start + BATCH_SIZE]
        body = json.dumps(chunk).encode("utf-8")
        request = urllib.request.Request(
            EXPO_PUSH_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "hermes-bot-push/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_S) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.URLError as exc:
            _log.debug("hermes-bot-push: push send failed: %s", exc)
            continue
        except Exception as exc:
            _log.debug("hermes-bot-push: push send failed: %s", exc)
            continue
        for ticket in payload.get("data") or []:
            if isinstance(ticket, dict) and ticket.get("status") == "error":
                # DeviceNotRegistered and friends: the phone re-registers on
                # its next launch, so log rather than mutate its record here.
                _log.info(
                    "hermes-bot-push: Expo rejected a message: %s",
                    (ticket.get("details") or {}).get("error") or ticket.get("message"),
                )


def _send_async(messages: List[Dict[str, Any]]) -> None:
    if not messages:
        return
    threading.Thread(target=_post, args=(messages,), name="hermes-bot-push", daemon=True).start()


# ── Hooks ───────────────────────────────────────────────────────────────────

_settings: Dict[str, Any] = {"platforms": {"tui"}, "all_sessions": False}


def _wanted(platform: str, session_id: str) -> Optional[Dict[str, str]]:
    platforms = _settings["platforms"]
    if platforms and platform and platform not in platforms:
        return None
    if _settings["all_sessions"]:
        return {"route": "chat"}
    return classify_session(_session_title(session_id))


def _base_data(profile: str, session_id: str, kind: str, target: Dict[str, str]) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "source": "push",
        "kind": kind,
        "route": target["route"],
        "bot": profile,
        "session": session_id,
        "at": int(time.time() * 1000),
    }
    if target["route"] == "group":
        data["group"] = target["group"]
    return data


def _handle_post_llm_call(**kwargs: Any) -> None:
    try:
        session_id = str(kwargs.get("session_id") or "")
        target = _wanted(str(kwargs.get("platform") or ""), session_id)
        if target is None:
            return
        targets = _load_targets()
        if not targets["tokens"]:
            return
        profile = _resolve_profile()
        label = isolate_name(targets["labels"].get(profile, profile))
        if target["route"] == "group":
            title = f"{label} replied in {isolate_name(target['group'])}"
        else:
            title = f"{label} finished"
        _send_async(
            build_messages(
                targets["tokens"],
                title=title,
                data=_base_data(profile, session_id, "complete", target),
            )
        )
    except Exception as exc:
        _log.debug("hermes-bot-push: post_llm_call skipped: %s", exc)


def _handle_pre_approval_request(**kwargs: Any) -> None:
    try:
        # The smart surface fires before the auxiliary model decides; most of
        # those are auto-approved and would only be noise. The interactive
        # surface fires when a human is actually being asked.
        if str(kwargs.get("surface") or "") == "smart":
            return
        session_id = str(kwargs.get("session_id") or "")
        target = _wanted("", session_id)
        if target is None:
            return
        targets = _load_targets()
        if not targets["tokens"]:
            return
        profile = _resolve_profile()
        label = isolate_name(targets["labels"].get(profile, profile))
        _send_async(
            build_messages(
                targets["tokens"],
                title=f"{label} needs approval",
                data=_base_data(profile, session_id, "approval", target),
                category="approval",
                time_sensitive=True,
            )
        )
    except Exception as exc:
        _log.debug("hermes-bot-push: pre_approval_request skipped: %s", exc)


def register(ctx: Any) -> None:
    def setting(key: str, fallback: Any) -> Any:
        try:
            value = ctx.get_config(key, fallback)
        except Exception:
            return fallback
        return fallback if value in (None, "") else value

    platforms = setting("platforms", ["tui"])
    if isinstance(platforms, str):
        platforms = [platforms]
    _settings["platforms"] = {str(p) for p in platforms} if isinstance(platforms, (list, tuple, set)) else {"tui"}
    _settings["all_sessions"] = bool(setting("all_sessions", False))

    if os.environ.get("HERMES_BOT_PUSH_DISABLED"):
        _log.info("hermes-bot-push: disabled by HERMES_BOT_PUSH_DISABLED")
        return

    ctx.register_hook("post_llm_call", _handle_post_llm_call)
    ctx.register_hook("pre_approval_request", _handle_pre_approval_request)
