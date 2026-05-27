# Streamlit frontend for the student-agent backend.
# ChatGPT-style: sidebar with chat history (grouped Today / Yesterday / Older)
# + main pane with message bubbles + chat input at the bottom.
# White theme via .streamlit/config.toml.
import base64
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
import streamlit as st

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
PAGE_SIZE = 10

# Proactively refresh the JWT when it has this many seconds (or fewer) of
# life left. Must be smaller than JWT_EXPIRATION_MINUTES * 60 on the backend.
# 60s margin handles clock skew and any in-flight calls.
REFRESH_MARGIN_SECONDS = 60


# ---------------------------------------------------------------------------
# Auth client: proactive refresh on every render + 401 fallback in api()
# ---------------------------------------------------------------------------

def _headers() -> dict:
    tok = st.session_state.get("access_token")
    return {"authorization": f"Bearer {tok}"} if tok else {}


def _decode_exp(token: str | None) -> int | None:
    """Read the `exp` claim from a JWT WITHOUT verifying the signature.
    Returns the expiry as a Unix timestamp, or None if the token is malformed.
    Verification happens on the backend; the frontend only needs to know
    when the token will expire so it can refresh in time."""
    if not token:
        return None
    try:
        payload_b64 = token.split(".")[1]
        # base64url decode needs padding to a multiple of 4
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload.get("exp", 0)) or None
    except Exception:
        return None


def _refresh_token() -> bool:
    """Exchange the current JWT for a new one. Requires the current token
    to still be valid (backend's /auth/refresh decodes + re-signs)."""
    tok = st.session_state.get("access_token")
    if not tok:
        return False
    try:
        r = httpx.post(
            f"{BACKEND_URL}/auth/refresh",
            headers={"authorization": f"Bearer {tok}"},
            timeout=10,
        )
    except Exception:
        return False
    if r.status_code != 200:
        return False
    st.session_state["access_token"] = r.json()["access_token"]
    return True


def ensure_fresh_token() -> bool:
    """Proactive refresh. Call once at the top of every render (which fires
    on every user interaction in Streamlit) and once before each API call.
    Returns True if the session now holds a usable token, False if the
    caller should bounce the user to login."""
    tok = st.session_state.get("access_token")
    if not tok:
        return False
    exp = _decode_exp(tok)
    if exp is None:
        return False
    now = int(time.time())
    if now < exp - REFRESH_MARGIN_SECONDS:
        return True  # plenty of life left
    # within margin (or already past it) - try to refresh while token may
    # still be valid; if it is fully past exp, backend will reject and we
    # propagate that as a hard logout to the caller.
    return _refresh_token()


def api(method: str, path: str, **kwargs: Any) -> httpx.Response:
    """Make an API call after ensuring the token is fresh. Falls back to a
    one-shot 401-refresh-retry for clock-skew / race-condition safety."""
    if not ensure_fresh_token():
        # session is gone - clear and force re-login on next rerun
        for k in ("access_token", "student_id"):
            st.session_state.pop(k, None)
        st.error("Session expired. Please log in again.")
        return httpx.Response(401)
    url = f"{BACKEND_URL}{path}"
    headers = {**_headers(), **kwargs.pop("headers", {})}
    try:
        r = httpx.request(method, url, headers=headers, timeout=60, **kwargs)
    except Exception as e:
        st.error(f"network error: {e}")
        return httpx.Response(599)
    if r.status_code == 401 and _refresh_token():
        headers = {**_headers(), **kwargs.pop("headers", {})}
        r = httpx.request(method, url, headers=headers, timeout=60, **kwargs)
    return r


# ---------------------------------------------------------------------------
# Auth screens
# ---------------------------------------------------------------------------

def render_login() -> None:
    st.title("Student Learning Assistant")
    tab_login, tab_signup = st.tabs(["Log in", "Sign up"])

    with tab_login:
        with st.form("login"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Log in", use_container_width=True)
        if submitted:
            r = httpx.post(
                f"{BACKEND_URL}/auth/login",
                json={"email": email, "password": password},
                timeout=15,
            )
            if r.status_code != 200:
                st.error(r.json().get("error", {}).get("message", "login failed"))
                return
            body = r.json()
            st.session_state["access_token"] = body["access_token"]
            st.session_state["student_id"] = body["student_id"]
            st.session_state["chat_id"] = None
            st.session_state["messages"] = []
            st.rerun()

    with tab_signup:
        with st.form("signup"):
            email = st.text_input("Email", key="su_email")
            password = st.text_input(
                "Password (min 8 chars)", type="password", key="su_password"
            )
            name = st.text_input("Name", key="su_name")
            col_a, col_b = st.columns(2)
            with col_a:
                grade = st.number_input(
                    "Grade", min_value=1, max_value=12, value=10, key="su_grade"
                )
            with col_b:
                board = st.selectbox("Board", ["CBSE", "ICSE"], key="su_board")
            target_exam = st.text_input(
                "Target exam", value="CBSE Board Exams 2027", key="su_target"
            )
            daily_minutes = st.number_input(
                "Daily study minutes", min_value=10, max_value=600, value=90,
                key="su_daily",
            )
            submitted = st.form_submit_button("Sign up", use_container_width=True)
        if submitted:
            r = httpx.post(
                f"{BACKEND_URL}/auth/signup",
                json={
                    "email": email, "password": password, "name": name,
                    "grade": int(grade), "board": board,
                    "target_exam": target_exam,
                    "daily_study_time_minutes": int(daily_minutes),
                },
                timeout=15,
            )
            if r.status_code != 201:
                st.error(r.json().get("error", {}).get("message", "signup failed"))
                return
            body = r.json()
            st.session_state["access_token"] = body["access_token"]
            st.session_state["student_id"] = body["student_id"]
            st.session_state["chat_id"] = None
            st.session_state["messages"] = []
            st.rerun()


# ---------------------------------------------------------------------------
# Chat history sidebar
# ---------------------------------------------------------------------------

def _bucket(dt_iso: str, today: date) -> str:
    try:
        d = datetime.fromisoformat(dt_iso.replace("Z", "+00:00")).date()
    except Exception:
        return "Older"
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    return "Older"


def _render_chat_row(c: dict) -> None:
    """One row in the sidebar: full-width chat title button with a ⋯ popover
    overlaid on its right edge (revealed on hover via CSS). The popover holds
    a confirm-required Delete action."""
    chat_id = c["chat_id"]
    title = (c.get("title") or "(new chat)")[:40]
    # st.container(key=...) emits a wrapper with class `st-key-chat_row_<id>`
    # so CSS can position the popover absolutely on top of the title button.
    with st.container(key=f"chat_row_{chat_id}"):
        if st.button(title, key=f"chat_{chat_id}", use_container_width=True):
            st.session_state["chat_id"] = chat_id
            st.session_state["messages"] = []
            st.session_state["loaded_before"] = None
            st.rerun()
        with st.popover("⋯", use_container_width=False):
            confirm_key = f"confirm_delete_{chat_id}"
            if not st.session_state.get(confirm_key):
                if st.button(
                    "🗑 Delete chat",
                    key=f"delete_{chat_id}",
                    use_container_width=True,
                ):
                    st.session_state[confirm_key] = True
                    st.rerun()
            else:
                # Streamlit disallows nested columns inside the sidebar
                # (the chat row is already in a column). Stack the
                # confirm/cancel buttons vertically instead.
                st.caption("Delete this chat permanently?")
                if st.button(
                    "Yes, delete",
                    key=f"yes_{chat_id}",
                    type="primary",
                    use_container_width=True,
                ):
                    r = api("DELETE", f"/chat/{chat_id}")
                    if r.status_code in (200, 204):
                        if st.session_state.get("chat_id") == chat_id:
                            st.session_state["chat_id"] = None
                            st.session_state["messages"] = []
                            st.session_state["loaded_before"] = None
                        st.session_state.pop(confirm_key, None)
                        st.toast("Chat deleted")
                        st.rerun()
                    else:
                        try:
                            msg = r.json().get("error", {}).get("message", r.text)
                        except Exception:
                            msg = r.text or f"HTTP {r.status_code}"
                        st.error(f"Delete failed: {msg}")
                        st.session_state.pop(confirm_key, None)
                if st.button(
                    "Cancel", key=f"no_{chat_id}", use_container_width=True
                ):
                    st.session_state.pop(confirm_key, None)
                    st.rerun()


def _inject_sidebar_css() -> None:
    """Hide the popover-trigger column until the chat row is hovered. Targets
    Streamlit's stable [data-testid] attributes; verified against 1.40.x DOM.
    Also styles the bottom profile footer (avatar + name)."""
    st.markdown(
        """
        <style>
        /* Chat row: full-width title button with a ⋯ popover overlaid on
           the right edge. The container is `st-key-chat_row_<id>` (added by
           st.container(key=...) in 1.39+). */
        section[data-testid="stSidebar"] [class*="st-key-chat_row_"] {
            position: relative;
        }
        section[data-testid="stSidebar"]
          [class*="st-key-chat_row_"]
          div[data-testid="stPopover"] {
            position: absolute;
            top: 4px;
            right: 6px;
            opacity: 0;
            z-index: 10;
            transition: opacity 0.15s ease-in-out;
        }
        section[data-testid="stSidebar"]
          [class*="st-key-chat_row_"]:hover
          div[data-testid="stPopover"],
        section[data-testid="stSidebar"]
          [class*="st-key-chat_row_"]
          div[data-testid="stPopover"]:focus-within {
            opacity: 1;
        }
        /* The popover trigger button itself: compact + transparent so it
           overlays the title button cleanly. */
        section[data-testid="stSidebar"]
          [class*="st-key-chat_row_"]
          div[data-testid="stPopover"] button {
            min-width: 0;
            padding: 2px 8px;
            background: rgba(255, 255, 255, 0.85);
        }

        /* Profile footer popover (still uses st.columns): hover-reveal too. */
        section[data-testid="stSidebar"]
          div[data-testid="stHorizontalBlock"]
          div[data-testid="stPopover"] {
            opacity: 0;
            transition: opacity 0.15s ease-in-out;
        }
        section[data-testid="stSidebar"]
          div[data-testid="stHorizontalBlock"]:hover
          div[data-testid="stPopover"],
        section[data-testid="stSidebar"]
          div[data-testid="stHorizontalBlock"]
          div[data-testid="stPopover"]:focus-within {
            opacity: 1;
        }
        /* Profile footer: round avatar + user name. */
        .sa-avatar {
            width: 34px;
            height: 34px;
            border-radius: 50%;
            background: #e2e8f0;
            border: 1px solid #cbd5e1;
            margin: 0 auto;
        }
        .sa-name {
            font-weight: 500;
            font-size: 0.92rem;
            line-height: 34px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        /* Pin the profile footer (the last stHorizontalBlock rendered in
           the sidebar) to the bottom-left of the viewport so it doesn't
           scroll away when the chat list grows. Width matches Streamlit's
           default sidebar (244px). */
        section[data-testid="stSidebar"]
          div[data-testid="stHorizontalBlock"]:last-of-type {
            position: fixed;
            bottom: 0;
            left: 0;
            width: 244px;
            background: #ffffff;
            padding: 12px 16px;
            border-top: 1px solid #e2e8f0;
            z-index: 100;
            box-sizing: border-box;
        }
        /* Add bottom padding to the scrollable sidebar content so the last
           chat row isn't hidden behind the fixed footer. */
        section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"],
        section[data-testid="stSidebar"]
          [data-testid="stSidebarContent"]
          > div:first-child {
            padding-bottom: 80px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_profile_footer() -> None:
    """Bottom-of-sidebar profile area: round avatar (blank placeholder) + the
    user's name + a ⋯ popover containing the Log out action. The ⋯ trigger is
    hidden until the row is hovered (same CSS rule as chat rows)."""
    # cache the profile name in session_state to avoid an API call every rerun
    name = st.session_state.get("profile_name")
    if not name:
        r = api("GET", "/profile")
        if r.status_code == 200:
            try:
                name = (r.json() or {}).get("name", "") or ""
            except Exception:
                name = ""
            st.session_state["profile_name"] = name
        else:
            name = ""

    col_av, col_nm, col_menu = st.columns(
        [0.18, 0.62, 0.20], gap="small", vertical_alignment="center"
    )
    with col_av:
        st.markdown("<div class='sa-avatar'></div>", unsafe_allow_html=True)
    with col_nm:
        st.markdown(
            f"<div class='sa-name'>{name or 'User'}</div>",
            unsafe_allow_html=True,
        )
    with col_menu:
        with st.popover("⋯", use_container_width=True):
            if st.button(
                "🚪 Log out", key="logout_btn", use_container_width=True
            ):
                for k in list(st.session_state.keys()):
                    del st.session_state[k]
                st.rerun()


def render_sidebar() -> None:
    _inject_sidebar_css()
    with st.sidebar:
        if st.button("+ New chat", use_container_width=True, type="primary"):
            st.session_state["chat_id"] = None
            st.session_state["messages"] = []
            st.rerun()

        r = api("GET", "/chat/history", params={"limit": 50})
        if r.status_code != 200:
            st.caption("(could not load chat history)")
            return
        chats = r.json()
        today = date.today()
        grouped: dict[str, list] = {"Today": [], "Yesterday": [], "Older": []}
        for c in chats:
            grouped[_bucket(c.get("last_active_at", ""), today)].append(c)

        for group in ("Today", "Yesterday", "Older"):
            if not grouped[group]:
                continue
            st.caption(group)
            for c in grouped[group]:
                _render_chat_row(c)

        _render_profile_footer()


# ---------------------------------------------------------------------------
# Main chat pane
# ---------------------------------------------------------------------------

def _load_initial_messages() -> None:
    chat_id = st.session_state.get("chat_id")
    if not chat_id:
        st.session_state["messages"] = []
        return
    r = api(
        "GET", f"/chat/{chat_id}/messages",
        params={"limit": PAGE_SIZE},
    )
    if r.status_code != 200:
        st.session_state["messages"] = []
        return
    msgs = r.json()
    st.session_state["messages"] = msgs
    if msgs:
        st.session_state["loaded_before"] = msgs[0]["created_at"]


def _load_older_messages() -> None:
    chat_id = st.session_state.get("chat_id")
    before = st.session_state.get("loaded_before")
    if not chat_id or not before:
        return
    r = api(
        "GET", f"/chat/{chat_id}/messages",
        params={"limit": PAGE_SIZE, "before": before},
    )
    if r.status_code != 200:
        return
    older = r.json()
    if not older:
        st.toast("no older messages")
        return
    st.session_state["messages"] = older + st.session_state["messages"]
    st.session_state["loaded_before"] = older[0]["created_at"]


def render_chat() -> None:
    chat_id = st.session_state.get("chat_id")
    msgs = st.session_state.get("messages", [])

    # Auto-load on chat switch
    if chat_id and not msgs and st.session_state.get("loaded_for") != chat_id:
        _load_initial_messages()
        st.session_state["loaded_for"] = chat_id
        msgs = st.session_state.get("messages", [])

    if chat_id and msgs:
        if st.button("Load older messages"):
            _load_older_messages()
            st.rerun()

    for m in msgs:
        role = m.get("role", "user")
        # only render user + assistant in the timeline
        if role not in ("user", "assistant"):
            continue
        with st.chat_message(role):
            st.write(m.get("content", ""))

    user_input = st.chat_input("Type your message...")
    if user_input:
        with st.chat_message("user"):
            st.write(user_input)

        # Stream the assistant reply from POST /achat (SSE). st.write_stream
        # progressively renders tokens into the bubble as they arrive.
        full_text_holder: dict = {"text": "", "chat_id": chat_id, "error": None}

        def token_generator():
            if not ensure_fresh_token():
                full_text_holder["error"] = "Session expired. Please log in again."
                return
            headers = {
                "authorization": f"Bearer {st.session_state.get('access_token')}",
                "accept": "text/event-stream",
            }
            payload = {"message": user_input, "chat_id": chat_id}
            try:
                with httpx.stream(
                    "POST",
                    f"{BACKEND_URL}/achat",
                    headers=headers,
                    json=payload,
                    timeout=180,
                ) as r:
                    if r.status_code != 200:
                        try:
                            err_msg = r.read().decode("utf-8", "replace")[:200]
                        except Exception:
                            err_msg = f"HTTP {r.status_code}"
                        full_text_holder["error"] = f"chat failed: {err_msg}"
                        return
                    current_event = None
                    for raw_line in r.iter_lines():
                        if isinstance(raw_line, bytes):
                            line = raw_line.decode("utf-8", "replace")
                        else:
                            line = raw_line
                        # blank line terminates an SSE frame; reset and continue
                        if not line:
                            current_event = None
                            continue
                        if line.startswith("event: "):
                            current_event = line[len("event: "):].strip()
                            continue
                        if line.startswith("data: "):
                            data_str = line[len("data: "):]
                            try:
                                data = json.loads(data_str)
                            except Exception:
                                continue
                            if current_event == "token":
                                text = data.get("text", "")
                                if text:
                                    full_text_holder["text"] += text
                                    yield text
                            elif current_event == "done":
                                cid = data.get("chat_id")
                                if cid:
                                    full_text_holder["chat_id"] = cid
                                # final_text from server is authoritative
                                ft = data.get("full_text")
                                if ft:
                                    full_text_holder["text"] = ft
                            elif current_event == "error":
                                full_text_holder["error"] = data.get(
                                    "message",
                                    "Sorry, an internal issue happened. "
                                    "Please try again after some time.",
                                )
            except Exception as e:
                full_text_holder["error"] = f"network error: {e}"

        # Show a friendly "cooking" placeholder immediately so the user
        # doesn't stare at an empty bubble during the 5-7s of tool-calling
        # hops. As soon as the first text token arrives, clear the
        # placeholder and start filling the bubble character-by-character.
        cooking_lines = [
            "📚 _Pulling out your books..._",
            "🍳 _Cooking up your study plan..._",
            "✏️ _Sketching your plan..._",
            "🤔 _Thinking it through..._",
            "🔎 _Looking up the right materials..._",
        ]
        import random as _r
        cooking_text = _r.choice(cooking_lines)

        with st.chat_message("assistant"):
            cooking_slot = st.empty()
            cooking_slot.markdown(cooking_text)
            bubble_slot = st.empty()

            buf = ""
            for chunk in token_generator():
                if not buf:
                    cooking_slot.empty()
                buf += chunk
                bubble_slot.markdown(buf)

            # If the stream produced no tokens at all (e.g. transport error
            # before first token, error event arrived), clear the cooking
            # placeholder so the bubble doesn't end up stuck on it.
            if not buf:
                cooking_slot.empty()
                if full_text_holder.get("text"):
                    bubble_slot.markdown(full_text_holder["text"])

        if full_text_holder["error"]:
            st.error(full_text_holder["error"])
            return

        # commit to local state so the next rerun shows the same content
        st.session_state["chat_id"] = full_text_holder["chat_id"]
        now = datetime.now(timezone.utc).isoformat()
        st.session_state.setdefault("messages", []).extend([
            {"role": "user", "content": user_input, "created_at": now},
            {"role": "assistant", "content": full_text_holder["text"], "created_at": now},
        ])
        st.rerun()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="Student Learning Assistant",
        page_icon="📚",
        layout="wide",
    )
    # Streamlit reruns the script on every user interaction (button click,
    # input change, page navigation). Refresh the JWT proactively here so
    # downstream API calls never hit the expired state.
    if st.session_state.get("access_token") and not ensure_fresh_token():
        # token unrecoverable - clear and fall through to login
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.warning("Session expired. Please log in again.")

    if not st.session_state.get("access_token"):
        render_login()
        return
    render_sidebar()
    render_chat()


if __name__ == "__main__":
    main()
