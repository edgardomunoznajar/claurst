"""Simon web chat — standalone Streamlit app.

Runs against the OpenAI-compatible shim at http://127.0.0.1:8600/v1, which
shells into `simon --print` inside the simon container. This app owns its
entire UI; no aider, no streamlit default clutter.

Run:
    streamlit run web/app.py --server.port 8502 --server.headless true
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import streamlit as st

SHIM_BASE = os.environ.get("SIMON_SHIM_BASE", "http://127.0.0.1:8600/v1")
AUDIT_PATH_IN_CONTAINER = "/var/simon/audit.jsonl"
CONTAINER = os.environ.get("SIMON_CONTAINER", "simon")
ASSETS_DIR = Path(__file__).parent / "assets"

IDENTITIES = {
    "anonymous":        ("anonymous@simon.local",   "UNOFFICIAL"),
    "sarah-unofficial": ("sarah@simon.demo",        "UNOFFICIAL"),
    "bill-official":    ("bill@simon.demo",         "OFFICIAL"),
    "alice-protected":  ("alice@simon.demo",        "PROTECTED"),
}

CLEARANCE_COLOR = {
    "UNOFFICIAL":         "#005194",
    "OFFICIAL":           "#0B79D0",
    "OFFICIAL-SENSITIVE": "#C97A00",
    "PROTECTED":          "#A61717",
}

CLEARANCE_RANK = {
    "UNOFFICIAL": 0,
    "OFFICIAL": 1,
    "OFFICIAL-SENSITIVE": 2,
    "PROTECTED": 3,
}

WORKSPACE_TIERS = [
    ("unofficial",         "UNOFFICIAL"),
    ("official",           "OFFICIAL"),
    ("official-sensitive", "OFFICIAL-SENSITIVE"),
    ("protected",          "PROTECTED"),
]


def load_logo_b64() -> str:
    import base64
    p = ASSETS_DIR / "nqry_logo.svg"
    if not p.exists():
        return ""
    return base64.b64encode(p.read_bytes()).decode()


def send_to_simon(messages: list[dict]) -> str:
    payload = {"model": "simon-agent", "messages": messages, "stream": False}
    with httpx.Client(timeout=600.0) as client:
        r = client.post(f"{SHIM_BASE}/chat/completions", json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def shorten_name(name: str, maxlen: int = 44) -> str:
    stem = name[:-3] if name.endswith(".md") else name
    if len(stem) <= maxlen:
        return stem
    return stem[: maxlen - 1] + "…"


def list_workspace_files() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for subdir, _ in WORKSPACE_TIERS:
        try:
            out = subprocess.run(
                ["docker", "exec", CONTAINER, "ls", f"/workspace/{subdir}"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode != 0:
                result[subdir] = []
                continue
            names = [
                line for line in out.stdout.splitlines()
                if line and not line.endswith(".acl.json")
            ]
            result[subdir] = names
        except Exception:
            result[subdir] = []
    return result


def tail_audit(n: int = 15) -> list[dict]:
    try:
        out = subprocess.run(
            ["docker", "exec", CONTAINER, "tail", "-n", str(n), AUDIT_PATH_IN_CONTAINER],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return []
        entries = []
        for line in out.stdout.splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
    except Exception:
        return []


def init_page():
    st.set_page_config(
        page_title="Simon",
        layout="centered",
        initial_sidebar_state="expanded",
        menu_items={"About": "Simon — gov-agency agent, local Gemma 4 26B"},
    )
    logo = load_logo_b64()
    clearance = st.session_state.get("clearance", "UNOFFICIAL")
    bar_color = CLEARANCE_COLOR.get(clearance, "#005194")
    bar_text = f"{clearance} — SIMON DEMO WORKSPACE"

    st.markdown(
        f"""<style>
          @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap');
          html, body, [class*="st-"], .stMarkdown, .stChatMessage, .stButton > button, .stSelectbox *, .stTextInput input {{
            font-family: "Poppins", -apple-system, system-ui, sans-serif !important;
          }}
          body {{ background: #FBFCFD; color: #212121; }}

          #MainMenu, header[data-testid="stHeader"], footer {{ display: none !important; }}
          div[data-testid="stToolbar"], div[data-testid="stDecoration"] {{ display: none !important; }}
          a[href*="streamlit.io"] {{ display: none !important; }}

          .stApp > header {{ display: none; }}
          [data-testid="stAppViewContainer"] {{ padding-top: 26px; }}

          .simon-bar {{
            position: fixed; top: 0; left: 0; right: 0; z-index: 9999;
            background: {bar_color}; color: #fff;
            font: 500 11px/26px "Poppins", sans-serif;
            letter-spacing: 0.2em; text-align: center;
          }}

          section[data-testid="stSidebar"] {{
            background: #FFFFFF;
            border-right: 1px solid #E6EAEF;
          }}
          section[data-testid="stSidebar"] .block-container {{ padding-top: 26px; }}

          .brand {{ padding-bottom: 16px; border-bottom: 1px solid #E6EAEF; margin-bottom: 18px; }}
          .brand img {{ width: 120px; height: auto; display: block; margin-bottom: 10px; }}
          .brand .product {{
            font-weight: 300; font-size: 26px; letter-spacing: -0.5px; line-height: 1;
            color: #212121;
          }}
          .brand .tag {{ font-size: 12px; color: #707070; margin-top: 4px; }}

          .stChatMessage {{
            background: #FFFFFF;
            border: 1px solid #E6EAEF;
            border-radius: 3px; box-shadow: none;
          }}
          .stChatMessage pre, .stChatMessage code {{
            background: #F5F7FA !important;
            border: 1px solid #E6EAEF !important;
            border-radius: 3px !important;
            font-family: ui-monospace, Menlo, Consolas, monospace !important;
            font-size: 13px !important;
          }}

          .stChatInput {{ border-top: 1px solid #E6EAEF; padding-top: 12px; }}
          .stChatInput textarea {{
            background: #fff !important; border: 1px solid #E6EAEF !important;
            border-radius: 3px !important; color: #212121 !important;
          }}
          .stChatInput textarea:focus {{ border-color: #2196F3 !important; }}

          .stButton > button[kind="primary"] {{
            background: #005194; color: #fff; border: 0;
            border-radius: 2px; font-weight: 500; letter-spacing: 0.02em;
          }}
          .stButton > button[kind="primary"]:hover {{ background: #0059A3; }}

          .audit-row {{
            font: 400 11px/1.4 ui-monospace, Menlo, Consolas, monospace;
            padding: 6px 8px; border-left: 3px solid #E6EAEF;
            margin-bottom: 4px; background: #F9FAFB; border-radius: 2px;
            color: #4f4f4f;
          }}
          .audit-allow {{ border-left-color: #2E7D32; }}
          .audit-deny  {{ border-left-color: #C62828; background: #FDF3F3; }}
          .audit-subj  {{ color: #212121; font-weight: 500; }}
          .audit-op    {{ color: #005194; }}
          .audit-uri   {{ color: #4f4f4f; word-break: break-all; }}

          .ws-row {{
            display: flex; align-items: center; gap: 8px;
            padding: 6px 4px; border-bottom: 1px solid #F0F2F5;
            font: 400 12px/1 "Poppins", sans-serif;
          }}
          .ws-row .dot {{
            width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0;
          }}
          .ws-row .name {{ color: #212121; flex: 1; }}
          .ws-row .count {{ color: #707070; font-variant-numeric: tabular-nums; font-size: 11px; }}
          .ws-row .tier {{
            font-size: 9px; letter-spacing: 0.12em; color: #707070;
          }}
          .ws-file {{
            font: 400 11px/1.35 ui-monospace, Menlo, Consolas, monospace;
            padding: 1px 6px; color: #4f4f4f;
          }}
          .ws-file.denied {{ color: #B8BCC3; text-decoration: line-through; }}
          section[data-testid="stSidebar"] details {{
            border: 0 !important; background: transparent !important;
          }}
          section[data-testid="stSidebar"] details summary {{
            padding: 0 4px !important; font-size: 11px !important; color: #707070 !important;
          }}

          h1, h2, h3 {{ font-weight: 500; color: #212121; }}
          a, a:visited {{ color: #005194; }}
        </style>""",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"""<div class="simon-bar">{bar_text}</div>
        <div class="brand">
          {'<img src="data:image/svg+xml;base64,' + logo + '" alt="NQRY" />' if logo else ''}
          <div class="product">Simon</div>
          <div class="tag">Gov-agency agent · local Gemma 4 26B</div>
        </div>""",
        unsafe_allow_html=True,
    )


def sidebar():
    with st.sidebar:
        identity = st.selectbox(
            "Identity",
            list(IDENTITIES.keys()),
            index=list(IDENTITIES.keys()).index(st.session_state.get("identity", "anonymous")),
            help="Changes the subject the shim asserts to simon. Affects ACL decisions.",
        )
        email, clearance = IDENTITIES[identity]
        st.session_state["identity"] = identity
        st.session_state["email"] = email
        st.session_state["clearance"] = clearance
        st.caption(f"**{email}** · clearance `{clearance}`")

        st.divider()

        if st.button("Clear conversation", use_container_width=True):
            st.session_state["messages"] = []
            st.rerun()

        st.divider()
        st.markdown("**Workspace**")
        user_rank = CLEARANCE_RANK.get(clearance, 0)
        files_by_tier = list_workspace_files()
        for subdir, tier in WORKSPACE_TIERS:
            tier_rank = CLEARANCE_RANK[tier]
            allowed = tier_rank <= user_rank
            names = files_by_tier.get(subdir, [])
            dot_color = CLEARANCE_COLOR.get(tier, "#005194") if allowed else "#D0D4DA"
            st.markdown(
                f'<div class="ws-row">'
                f'<span class="dot" style="background:{dot_color}"></span>'
                f'<span class="name">{subdir}</span>'
                f'<span class="count">{len(names)}</span>'
                f'<span class="tier">{tier}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
            if names:
                with st.expander("files", expanded=False):
                    cls = "ws-file" if allowed else "ws-file denied"
                    rows = "".join(
                        f'<div class="{cls}">{shorten_name(n)}</div>' for n in names
                    )
                    st.markdown(rows, unsafe_allow_html=True)

        st.divider()
        st.markdown("**Audit tail**")
        auto = st.checkbox("Auto-refresh (2s)", value=False)
        entries = tail_audit(15)
        if not entries:
            st.caption("_no audit entries yet_")
        else:
            for e in reversed(entries):
                decision = e.get("decision", {}).get("decision", "?")
                cls = "audit-allow" if decision == "allow" else "audit-deny"
                subj = e.get("subject", "?")
                op = e.get("operation", "?")
                res = e.get("resource", {})
                uri = res.get("uri", "")
                st.markdown(
                    f'<div class="audit-row {cls}">'
                    f'<span class="audit-subj">{subj}</span> · '
                    f'<span class="audit-op">{op}</span> · {decision}<br>'
                    f'<span class="audit-uri">{uri[:120]}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        if auto:
            time.sleep(2)
            st.rerun()


def chat():
    if "messages" not in st.session_state:
        st.session_state["messages"] = []

    for m in st.session_state["messages"]:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    if prompt := st.chat_input("Ask Simon. ACL gate applies to every tool call."):
        st.session_state["messages"].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            placeholder = st.empty()
            placeholder.markdown("_thinking…_")
            try:
                reply = send_to_simon(st.session_state["messages"])
            except Exception as e:
                reply = f"**error talking to simon shim:** `{e}`"
            placeholder.markdown(reply)
        st.session_state["messages"].append({"role": "assistant", "content": reply})
        st.rerun()


def main():
    init_page()
    sidebar()
    chat()


if __name__ == "__main__":
    main()
