import logging
from html import escape
from pathlib import Path
from uuid import uuid4

import streamlit as st

from orderbot.actions import (
    create_new_order_request,
    list_customer_action_requests,
    list_support_cases,
    review_action_request,
    update_support_case_routing,
)
from orderbot.auth import (
    AuthenticationError,
    AuthIdentity,
    register_customer,
    restore_identity,
    revoke_session,
    sign_in_with_password,
)
from orderbot.catalog import list_active_products
from orderbot.chat import OrderChatHandler
from orderbot.database import conversation_belongs_to_user, format_timestamp, get_messages, get_order_timeline, order_summary
from orderbot.privacy import is_legacy_transcript_placeholder
from orderbot.sidebar import invalidate_recent_chats_cache, render_sidebar

logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Order Status Assistant",
    page_icon="O",
    layout="wide",
    initial_sidebar_state="collapsed",
)

_CSS_PATH = Path(__file__).parent / "static" / "app.css"


def _load_css() -> str:
    return _CSS_PATH.read_text(encoding="utf-8")


def apply_theme() -> None:
    """Inject the app stylesheet. Kept in static/app.css so main.py stays readable."""
    st.markdown(f"<style>{_load_css()}</style>", unsafe_allow_html=True)


FIRST_LOAD = "UNSET"
_REQUESTS_UNSET = object()


def current_session_id() -> str:
    """Stable id for this browser session, created on first use.

    Used to scope stored conversations so one visitor cannot list or reload
    another's (migration 006). This is a browser session, not a person: it stops
    cross-visitor reads, it is not authentication.
    """
    session_id = st.session_state.get("session_id")

    if not session_id:
        session_id = str(uuid4())
        st.session_state.session_id = session_id

    return session_id


def _start_new_handler(identity: AuthIdentity) -> None:
    st.session_state.legacy_history_notice = False
    st.session_state.chat_handler = OrderChatHandler(
        session_id=current_session_id(),
        owner_user_id=identity.user_id,
        authenticated_email=identity.email,
    )


def _load_conversation(conversation_id: str, identity: AuthIdentity) -> bool:
    """Load a conversation owned by the authenticated user. False if not.

    Every entry point funnels through here — the recent-chats radio and the
    `?chat=` query parameter — so the ownership check covers both. A rejected id
    falls back to a fresh chat rather than raising.
    """
    if not conversation_belongs_to_user(conversation_id, identity.user_id):
        logger.warning("Rejected load of conversation not owned by this session")
        st.session_state.viewing_conversation_id = None
        st.session_state.messages = []
        _start_new_handler(identity)
        st.query_params.pop("chat", None)
        return False

    st.session_state.viewing_conversation_id = conversation_id

    stored_messages = get_messages(conversation_id)
    st.session_state.legacy_history_notice = any(
        is_legacy_transcript_placeholder(message.get("content", ""))
        for message in stored_messages
    )
    st.session_state.chat_handler = OrderChatHandler(
        conversation_id=conversation_id,
        session_id=current_session_id(),
        owner_user_id=identity.user_id,
        authenticated_email=identity.email,
        stored_messages=stored_messages,
    )

    st.session_state.messages = [
        {
            "role": (message.get("role") if message.get("role") in ("user", "assistant") else "assistant"),
            "content": message.get("content", ""),
        }
        for message in stored_messages
        if message.get("content")
        and not is_legacy_transcript_placeholder(message.get("content", ""))
    ]

    st.query_params["chat"] = conversation_id

    return True


def initialize_session_state(identity: AuthIdentity) -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = []

    if "viewing_conversation_id" not in st.session_state:
        st.session_state.viewing_conversation_id = FIRST_LOAD

    if "chat_handler" not in st.session_state:
        st.session_state.chat_handler = None

    requested = st.session_state.pop(
        "resume_conversation_id",
        None,
    )

    viewing = st.session_state.viewing_conversation_id

    if requested:
        _load_conversation(requested, identity)

    elif viewing == FIRST_LOAD:
        url_id = st.query_params.get("chat")

        if url_id:
            _load_conversation(url_id, identity)

        else:
            st.session_state.viewing_conversation_id = None

            if st.session_state.chat_handler is None:
                _start_new_handler(identity)

    elif viewing is not None:
        handler = st.session_state.get("chat_handler")

        if handler is None or handler.conversation_id != viewing:
            _load_conversation(viewing, identity)


def start_new_chat() -> None:
    previous_handler = st.session_state.get("chat_handler")
    owner_user_id = getattr(previous_handler, "owner_user_id", None)
    authenticated_email = getattr(previous_handler, "authenticated_email", None)

    if previous_handler:
        try:
            previous_handler.end_session()
        except Exception:
            logger.exception("Could not mark the previous conversation as ended")

    st.session_state.viewing_conversation_id = None
    st.session_state.messages = []
    st.session_state.legacy_history_notice = False
    st.session_state.chat_handler = OrderChatHandler(
        session_id=current_session_id(),
        owner_user_id=owner_user_id,
        authenticated_email=authenticated_email,
    )

    for key in ("recent_chat_choice", "resume_conversation_id", "suggested_prompt"):
        st.session_state.pop(key, None)

    st.query_params.pop("chat", None)


def queue_new_chat() -> None:
    st.session_state.new_chat_requested = True


def queue_sign_out() -> None:
    st.session_state.sign_out_requested = True


def sign_out_user() -> None:
    tokens = st.session_state.get("auth_tokens") or {}
    if tokens.get("access_token") and tokens.get("refresh_token"):
        revoke_session(tokens["access_token"], tokens["refresh_token"])
    handler = st.session_state.get("chat_handler")
    if handler:
        try:
            handler.end_session()
        except Exception:
            logger.exception("Could not mark the conversation as ended during sign out")
    for key in (
        "auth_tokens",
        "auth_identity",
        "auth_audience",
        "authenticated_audience",
        "legacy_history_notice",
        "auth_mode",
        "chat_handler",
        "messages",
        "viewing_conversation_id",
        "session_id",
        "suggested_prompt",
        "manual_lookup_results",
        "manual_lookup_error",
        "manual_lookup_label",
        "manual_lookup_name_only",
        "resume_conversation_id",
        "recent_chat_choice",
        "recent_chat_options_map",
        "lookup_order_number",
        "pending_new_order_request",
    ):
        st.session_state.pop(key, None)
    st.query_params.clear()


def render_header_for_signed_out() -> None:
    st.markdown(
        """
        <div class="app-brand">
            <div class="brand-mark">OS</div>
            <div><div class="brand-name">Order support</div>
            <div class="brand-caption">Secure customer and staff access</div></div>
        </div>
        <div class="app-header-rule"></div>
        """,
        unsafe_allow_html=True,
    )


def _select_auth_option(state_key: str, option: str) -> None:
    st.session_state[state_key] = option


def render_auth_toggle(label: str, options: list[str], state_key: str) -> str:
    selected = st.session_state.setdefault(state_key, options[0])
    st.markdown(
        f'<div class="auth-toggle-label">{escape(label)}</div>',
        unsafe_allow_html=True,
    )
    toggle_key = state_key.replace("_", "-") + "-toggle"
    with st.container(key=toggle_key):
        columns = st.columns(len(options), gap=None)
        for index, (option, column) in enumerate(zip(options, columns)):
            with column:
                st.button(
                    option,
                    key=f"{toggle_key}-{index}",
                    type="primary" if selected == option else "secondary",
                    width="stretch",
                    on_click=_select_auth_option,
                    args=(state_key, option),
                )
    return st.session_state[state_key]


def render_auth_screen() -> None:
    st.markdown(
        """
        <section class="welcome-panel auth-welcome">
            <div class="welcome-eyebrow">SECURE CUSTOMER SUPPORT</div>
            <div class="welcome-title">Sign in to continue</div>
            <div class="welcome-text">Sign in with your email and password. New customers can create an account.
            Staff access is limited to approved accounts.</div>
        </section>
        """,
        unsafe_allow_html=True,
    )
    center = st.columns([0.8, 1.4, 0.8])[1]
    with center:
        with st.container(border=True, key="auth-panel"):
            audience = render_auth_toggle("Access type", ["Customer", "Support staff"], "auth_audience")
            mode = "Sign in"
            if audience == "Customer":
                mode = render_auth_toggle("Customer account", ["Sign in", "Create account"], "auth_mode")
            with st.form("auth_form", clear_on_submit=True, border=False):
                email = st.text_input("Account email", placeholder="name@example.com")
                password = st.text_input("Password", type="password")
                submitted = st.form_submit_button(
                    "Create customer account" if mode == "Create account" else "Sign in",
                    type="primary",
                    use_container_width=True,
                )

    if submitted:
        try:
            if mode == "Create account":
                identity = register_customer(email, password)
                if identity is None:
                    st.success("Account created. Confirm your email using the link we sent, then sign in with your password.")
                    return
            else:
                identity = sign_in_with_password(
                    email,
                    password,
                    require_staff_check=audience == "Support staff",
                )

            if audience == "Support staff" and not identity.is_staff:
                revoke_session(identity.access_token, identity.refresh_token)
                st.error("This account is not enabled for support staff access.")
            else:
                st.session_state.auth_tokens = {
                    "access_token": identity.access_token,
                    "refresh_token": identity.refresh_token,
                }
                st.session_state.authenticated_audience = audience
                st.rerun()
        except AuthenticationError as exc:
            st.error(str(exc))


def current_authenticated_identity() -> AuthIdentity | None:
    tokens = st.session_state.get("auth_tokens") or {}
    if not tokens.get("access_token") or not tokens.get("refresh_token"):
        return None
    try:
        audience = st.session_state.get("authenticated_audience") or st.session_state.get("auth_audience")
        identity = restore_identity(
            tokens["access_token"],
            tokens["refresh_token"],
            require_staff_check=audience == "Support staff",
        )
        st.session_state.auth_tokens = {
            "access_token": identity.access_token,
            "refresh_token": identity.refresh_token,
        }
        return identity
    except AuthenticationError:
        sign_out_user()
        return None


def render_staff_console(identity: AuthIdentity) -> None:
    render_header(identity, staff=True)
    st.markdown(
        """
        <section class="welcome-panel staff-welcome">
            <div class="welcome-eyebrow">SUPPORT WORKSPACE</div>
            <div class="welcome-title">Support case workspace</div>
            <div class="welcome-text">Find, prioritize, and resolve customer-submitted cases. Case updates are visible
            to customers; they do not change order state in the order-management system.</div>
        </section>
        """,
        unsafe_allow_html=True,
    )
    cases = list_support_cases("all")
    if cases is None:
        st.error("The support queue couldn't be loaded. Please try again shortly.")
        return
    active_count = sum(case["status"] in {"submitted", "reviewed"} for case in cases)
    unassigned_count = sum(
        case["status"] in {"submitted", "reviewed"} and not case.get("assigned_staff_user_id")
        for case in cases
    )
    urgent_count = sum(
        case["status"] in {"submitted", "reviewed"} and case.get("priority") == "urgent"
        for case in cases
    )
    metric_cols = st.columns(3)
    metric_cols[0].metric("Active cases", active_count)
    metric_cols[1].metric("Unassigned", unassigned_count)
    metric_cols[2].metric("Urgent", urgent_count)

    filter_cols = st.columns([1, 1, 1, 1.8], vertical_alignment="bottom")
    with filter_cols[0]:
        status_filter = st.selectbox("Case status", ["Active", "Closed", "All"], key="staff-case-status")
    with filter_cols[1]:
        assignment_filter = st.selectbox(
            "Assignment", ["Everyone", "Unassigned", "Assigned to me"], key="staff-case-assignment"
        )
    with filter_cols[2]:
        priority_filter = st.selectbox(
            "Priority", ["Any", "Urgent", "High", "Normal", "Low"], key="staff-case-priority"
        )
    with filter_cols[3]:
        search = st.text_input("Search cases", placeholder="Case, order, or reason", key="staff-case-search")

    status_key = {"Active": "active", "Closed": "closed", "All": "all"}[status_filter]
    visible_cases = [case for case in cases if (
        status_key == "all"
        or (case["status"] in {"submitted", "reviewed"} if status_key == "active" else case["status"] in {"completed", "rejected", "failed"})
    )]
    if assignment_filter == "Unassigned":
        visible_cases = [case for case in visible_cases if not case.get("assigned_staff_user_id")]
    elif assignment_filter == "Assigned to me":
        visible_cases = [case for case in visible_cases if case.get("assigned_staff_user_id") == identity.user_id]
    if priority_filter != "Any":
        visible_cases = [case for case in visible_cases if case.get("priority") == priority_filter.casefold()]
    if search.strip():
        needle = search.strip().casefold().lstrip("#")
        visible_cases = [
            case for case in visible_cases
            if needle in str(case.get("id", "")).casefold()
            or needle in str(case.get("order_id", "")).casefold()
            or needle in str(case.get("reason", "")).casefold()
            or needle in str((case.get("request_details") or {}).get("item_description", "")).casefold()
            or needle in str((case.get("request_details") or {}).get("topic", "")).casefold()
            or needle in str((case.get("request_details") or {}).get("summary", "")).casefold()
        ]

    st.caption(f"Showing {len(visible_cases)} case{'s' if len(visible_cases) != 1 else ''}")
    if not visible_cases:
        st.info("No cases match these filters.")
        return

    for request in visible_cases:
        request_id = request["id"]
        action = {
            "new_order_request": "New order request",
            "delivery_issue_request": "Delivery investigation",
            "cancel_request": "Cancellation",
            "return_request": "Return",
            "support_request": (request.get("request_details") or {}).get("topic") or "Support request",
        }.get(request["action_type"], "Support request")
        order_label = f"Order #{int(request['order_id']):04d}" if request.get("order_id") else "Account request"
        status = request.get("status", "submitted")
        is_active = status in {"submitted", "reviewed"}
        with st.container(border=True):
            st.markdown(f"**Case #{request_id} · {action} · {order_label}**")
            st.caption(
                f"{status.replace('_', ' ').title()} · {request.get('priority', 'normal').title()} priority · "
                f"Submitted {format_timestamp(request.get('created_at'))}"
            )
            st.markdown(f"**Customer reason**\n\n{request.get('reason') or 'Customer request'}")
            request_details = request.get("request_details") or {}
            if request_details:
                if request_details.get("handoff_summary"):
                    st.markdown(f"**Handoff summary**\n\n{request_details['handoff_summary']}")
                if request_details.get("topic"):
                    st.markdown(f"**Request topic:** {request_details['topic']}")
                if request_details.get("summary"):
                    st.markdown(f"**Request summary**\n\n{request_details['summary']}")
                if request_details.get("details"):
                    st.markdown(f"**Request details**\n\n{request_details['details']}")
                customer_context = request_details.get("customer_context") or []
                if customer_context:
                    with st.expander("Recent customer context"):
                        for message in customer_context:
                            st.write(message)
                snapshot = request_details.get("order_snapshot") or {}
                if snapshot:
                    st.caption(
                        f"Verified order status: {snapshot.get('status', 'Unknown')} · "
                        f"Payment: {snapshot.get('payment_status') or 'Not available'}"
                    )
                    if snapshot.get("tracking_number"):
                        st.caption(
                            f"Shipment: {snapshot.get('carrier') or 'Carrier unavailable'} · "
                            f"Tracking {snapshot['tracking_number']}"
                        )
                    items = snapshot.get("items") or []
                    if items:
                        st.markdown("**Order items**")
                        for item in items:
                            st.write(f"{item.get('quantity', 1)} × {item.get('product_name', 'Item')}")
                policy_evidence = request_details.get("policy_evidence") or []
                if policy_evidence:
                    with st.expander("Retrieved policy references"):
                        for reference in policy_evidence:
                            st.markdown(f"**{reference.get('source', 'Policy')}**")
                            st.write(reference.get("excerpt", ""))
                        st.caption(request_details.get("policy_note", "Reference material only."))
                quantity = request_details.get("quantity", 1)
                item = request_details.get("item_description", "Item not specified")
                if request["action_type"] == "new_order_request":
                    st.markdown(f"**Requested item**\n\n{quantity} × {item}")
                if request_details.get("notes"):
                    st.markdown(f"**Customer details**\n\n{request_details['notes']}")

            current_assignee = request.get("assigned_staff_user_id")
            assign_col, priority_col, save_col = st.columns([1.2, 1.2, 0.8], vertical_alignment="bottom")
            with assign_col:
                if current_assignee == identity.user_id:
                    if st.button("Release assignment", key=f"release-{request_id}", disabled=not is_active):
                        if update_support_case_routing(request_id, identity.user_id, assign_to_me=False):
                            st.rerun()
                        st.error("Couldn't release this case assignment.")
                elif current_assignee is None:
                    if st.button("Assign to me", key=f"assign-{request_id}", disabled=not is_active):
                        if update_support_case_routing(request_id, identity.user_id, assign_to_me=True):
                            st.rerun()
                        st.error("This case may have been assigned by another staff member.")
                else:
                    st.caption("Assigned to another staff member")
            priorities = ["low", "normal", "high", "urgent"]
            with priority_col:
                selected_priority = st.selectbox(
                    "Priority",
                    priorities,
                    index=priorities.index(request.get("priority", "normal")),
                    key=f"case-priority-{request_id}",
                    disabled=not is_active,
                )
            with save_col:
                if st.button("Save priority", key=f"save-priority-{request_id}", disabled=not is_active):
                    if update_support_case_routing(
                        request_id, identity.user_id, priority=selected_priority
                    ):
                        st.success("Priority updated.")
                        st.rerun()
                    st.error("Priority couldn't be updated.")

            updates = request.get("updates", [])
            if updates:
                with st.expander(f"Case history ({len(updates)} update{'s' if len(updates) != 1 else ''})"):
                    for update in updates:
                        previous = (update.get("previous_status") or "submitted").title()
                        changed = (update.get("new_status") or "updated").title()
                        st.markdown(f"**{previous} → {changed}** · {format_timestamp(update.get('created_at'))}")
                        if update.get("note"):
                            st.write(update["note"])

            if not is_active:
                continue
            note = st.text_area(
                "Update shown to the customer (optional)",
                max_chars=500,
                key=f"staff-note-{request_id}",
            )
            next_status = "reviewed" if status == "submitted" else "completed"
            next_label = "Mark reviewed" if next_status == "reviewed" else "Mark complete"
            reviewed_col, rejected_col = st.columns(2)
            with reviewed_col:
                if st.button(next_label, key=f"review-{request_id}", use_container_width=True):
                    if review_action_request(request_id, identity.user_id, next_status, note):
                        st.success("Case status and customer update saved.")
                        st.rerun()
                    st.error("The case could not be updated. It may already have changed.")
            with rejected_col:
                if st.button("Decline request", key=f"reject-{request_id}", use_container_width=True):
                    if review_action_request(request_id, identity.user_id, "rejected", note):
                        st.success("Request declined and customer update recorded.")
                        st.rerun()
                    st.error("The case could not be updated. It may already have changed.")


def render_header(identity: AuthIdentity, staff: bool = False) -> None:
    brand, account, actions = st.columns([4, 3, 3], vertical_alignment="center")
    with brand:
        st.markdown(
            """
            <div class="app-brand">
                <div class="brand-mark">OS</div>
                <div><div class="brand-name">Order support</div>
                <div class="brand-caption">Order help, clearly handled</div></div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with actions:
        if staff:
            st.button("Sign out", key="header-sign-out", on_click=queue_sign_out, use_container_width=True)
        else:
            new_col, signout_col = st.columns(2)
            with new_col:
                st.button("New chat", key="header-new-chat", on_click=queue_new_chat, use_container_width=True)
            with signout_col:
                st.button("Sign out", key="header-sign-out", on_click=queue_sign_out, use_container_width=True)
    with account:
        st.markdown(
            f'<div class="account-chip" title="{escape(identity.email)}">'
            f'<span>Account</span><strong>{escape(identity.email)}</strong></div>',
            unsafe_allow_html=True,
        )
    st.markdown('<div class="app-header-rule"></div>', unsafe_allow_html=True)


def _queue_prompt(prompt: str) -> None:
    st.session_state.pop("resume_conversation_id", None)
    st.session_state.suggested_prompt = prompt


SOURCE_MARKER = "\n\n**Sources:** "


def render_message_content(role: str, content: str) -> None:
    """Render the answer and show citations only when the customer opts in."""
    if role != "assistant" or SOURCE_MARKER not in content:
        st.markdown(content)
        return

    answer, sources = content.rsplit(SOURCE_MARKER, 1)
    st.markdown(answer)
    if st.session_state.get("show_policy_sources", False):
        with st.expander("Policy sources", expanded=False):
            for source in sources.split("; "):
                st.markdown(f"- {source}")


def render_dashboard(identity: AuthIdentity, requests: list[dict] | None) -> None:
    st.markdown(
        f"""
        <section class="welcome-panel dashboard-welcome">
            <div class="welcome-eyebrow">CUSTOMER CARE</div>
            <div class="welcome-title">Customer dashboard</div>
            <div class="welcome-text">Welcome back, {escape(identity.email)}. Your orders, support requests, and help resources are in one place.</div>
        </section>
        """,
        unsafe_allow_html=True,
    )
    active = [request for request in requests or [] if request.get("status") in {"submitted", "reviewed"}]
    completed = [request for request in requests or [] if request.get("status") == "completed"]

    with st.container(border=True, key="customer-dashboard"):
        st.markdown("**Account overview**")
        metric_cols = st.columns(3)
        metric_cols[0].metric("Support requests", len(requests) if requests is not None else "N/A")
        metric_cols[1].metric("In progress", len(active) if requests is not None else "N/A")
        metric_cols[2].metric("Completed", len(completed) if requests is not None else "N/A")

        st.markdown("**Services**")
        action_cols = st.columns(3)
        with action_cols[0]:
            if st.button("Check an order", key="dashboard-check-order", use_container_width=True):
                _queue_prompt("I want to check an order")
                st.rerun()
        with action_cols[1]:
            if st.button("Request a new order", key="dashboard-new-order", use_container_width=True):
                st.session_state.open_new_order_request = True
                st.rerun()
        with action_cols[2]:
            if st.button("View support requests", key="dashboard-support-requests", use_container_width=True):
                st.session_state.open_customer_requests = True
                st.rerun()

    with st.container(border=True, key="dashboard-activity"):
        st.markdown("**Recent support activity**")
        if requests is None:
            st.caption("Request history is temporarily unavailable.")
        elif not requests:
            st.caption("No support requests yet. Submitted requests and staff updates will appear here.")
        else:
            for request in requests[:3]:
                action = {
                    "cancel_request": "Cancellation",
                    "return_request": "Return",
                    "delivery_issue_request": "Delivery investigation",
                    "new_order_request": "New order",
                    "support_request": (request.get("request_details") or {}).get("topic") or "Support request",
                }.get(request.get("action_type"), "Support")
                order = (
                    f"Order #{int(request['order_id']):04d}"
                    if request.get("order_id")
                    else "Account request"
                )
                status = request.get("status", "submitted").replace("_", " ").title()
                detail_col, status_col = st.columns([4, 1], vertical_alignment="center")
                with detail_col:
                    st.markdown(f"**{action} · {order}**")
                    st.caption(f"Updated {format_timestamp(request.get('created_at'))}")
                with status_col:
                    st.markdown(f"**{status}**")


def render_verified_order() -> None:
    handler = st.session_state.chat_handler
    orders = (handler.last_db_results or {}).get("matched") or []
    if not orders:
        return

    st.markdown("#### Your verified order" if len(orders) == 1 else "#### Matching orders")
    for order in orders:
        summary = order_summary(order)
        shipment_time = summary["delivered_at"] or summary["estimated_delivery_at"]
        shipment_label = "Delivered" if summary["delivered_at"] else "Estimated delivery"

        with st.container(border=True):
            identity_col, status_col, delivery_col = st.columns([1.3, 1, 1.4], vertical_alignment="center")
            with identity_col:
                st.caption("ORDER")
                st.markdown(f"### #{summary['order_number']}")
            with status_col:
                st.caption("CURRENT STATUS")
                st.markdown(f"**{summary['status']}**")
            with delivery_col:
                st.caption(shipment_label.upper() if shipment_time else "DELIVERY")
                st.markdown(f"**{format_timestamp(shipment_time) if shipment_time else 'No estimate available'}**")

            if not handler.name_only_verified:
                timeline = get_order_timeline(order)
                if timeline:
                    with st.expander(f"View activity for order #{summary['order_number']}"):
                        for event in timeline[-4:]:
                            event_label = (event.get("event_type") or "Update").replace("_", " ").title()
                            st.markdown(f"**{event_label}** · {format_timestamp(event.get('event_at'))}")


def render_pending_action() -> None:
    pending_action = st.session_state.chat_handler.pending_action
    if not pending_action:
        return

    action_label = {
        "delivery_issue_request": "Delivery investigation",
        "cancel_request": "Cancellation",
        "return_request": "Return",
        "support_request": (pending_action.get("request_details") or {}).get("topic") or "Support request",
    }.get(pending_action["action"], "Support request")
    order_label = (
        f"Order #{pending_action['order_id']:04d}" if pending_action.get("order_id") else "Account request"
    )
    st.markdown("#### Review your support request")
    with st.container(border=True):
        st.markdown(f"**{action_label} · {order_label}**")
        st.caption(
            "Review the handoff details before submitting. This sends a request for staff review; "
            "it does not change your order."
        )
        request_details = pending_action.get("request_details") or {}
        if request_details.get("handoff_summary"):
            st.markdown(f"**Case summary**\n\n{request_details['handoff_summary']}")
        if request_details.get("topic"):
            st.markdown(f"**Topic:** {request_details['topic']}")
        if request_details.get("summary"):
            st.markdown(f"**Request summary**\n\n{request_details['summary']}")
        if request_details.get("details"):
            st.markdown(f"**Request details**\n\n{request_details['details']}")
        customer_context = request_details.get("customer_context") or []
        if customer_context:
            with st.expander("Recent customer context included"):
                for message in customer_context:
                    st.write(message)
        snapshot = request_details.get("order_snapshot") or {}
        if snapshot:
            item_labels = [
                f"{item.get('quantity', 1)} × {item.get('product_name', 'Item')}"
                for item in snapshot.get("items", [])
            ]
            st.markdown(f"**Recorded order status:** {snapshot.get('status', 'Unknown')}")
            if item_labels:
                st.caption("Verified order items: " + ", ".join(item_labels))
        st.markdown(f"**Reason**\n\n{pending_action['reason']}")
        evidence = request_details.get("policy_evidence") or []
        if evidence:
            with st.expander("Policy references included for staff"):
                for reference in evidence:
                    st.markdown(f"**{reference['source']}**")
                    st.write(reference["excerpt"])
                st.caption(request_details.get("policy_note", "Policy references do not determine eligibility."))
        expires_at = pending_action["expires_at"].astimezone()
        st.caption(f"Confirmation expires at {expires_at.strftime('%I:%M %p').lstrip('0')} local time.")
        confirm_col, cancel_col = st.columns(2)
        with confirm_col:
            if st.button(
                "Submit support request",
                key=f"confirm-action-{pending_action['request_key']}",
                type="primary",
                use_container_width=True,
            ):
                submitted, message = st.session_state.chat_handler.confirm_pending_action(
                    pending_action["request_key"]
                )
                st.session_state.messages.append({"role": "assistant", "content": message})
                (st.success if submitted else st.error)(message)
                invalidate_recent_chats_cache()
                st.rerun()
        with cancel_col:
            if st.button(
                "Discard request",
                key=f"cancel-action-{pending_action['request_key']}",
                use_container_width=True,
            ):
                st.session_state.chat_handler.discard_pending_action(pending_action["request_key"])
                st.info("Request discarded.")
                st.rerun()


def render_customer_cases(owner_user_id: str, requests=_REQUESTS_UNSET) -> None:
    """Show this signed-in customer's support request status and staff updates."""
    if requests is _REQUESTS_UNSET:
        requests = list_customer_action_requests(owner_user_id)
    with st.expander(
        "My support requests",
        expanded=bool(st.session_state.pop("open_customer_requests", False)),
        key="customer-requests",
    ):
        if requests is None:
            st.info("Support request updates are temporarily unavailable. Please try again shortly.")
            return
        if not requests:
            st.caption("No support requests have been submitted from your account yet.")
            return

        for request in requests:
            action = {
                "cancel_request": "Cancellation",
                "return_request": "Return",
                "delivery_issue_request": "Delivery investigation",
                "new_order_request": "New order",
                "support_request": (request.get("request_details") or {}).get("topic") or "Support request",
            }.get(request["action_type"], "Support")
            status = request.get("status", "submitted").replace("_", " ").title()
            with st.container(border=True):
                order_label = f"Order #{int(request['order_id']):04d}" if request.get("order_id") else "Account request"
                st.markdown(f"**{action} request · {order_label}**")
                st.caption(
                    f"{status} · Submitted {format_timestamp(request.get('created_at'))}"
                )
                st.caption(f"Reason: {request.get('reason') or 'Customer request'}")
                request_details = request.get("request_details") or {}
                if request_details and request["action_type"] == "support_request":
                    if request_details.get("summary"):
                        st.write(request_details["summary"])
                    if request_details.get("details"):
                        st.caption(request_details["details"])
                elif request_details:
                    st.caption(
                        f"Requested: {request_details.get('quantity', 1)} × "
                        f"{request_details.get('item_description', 'Item not specified')}"
                    )
                    if request_details.get("notes"):
                        st.write(request_details["notes"])
                for update in request.get("updates", []):
                    if update.get("note"):
                        update_status = update.get("new_status", "updated").title()
                        st.markdown(f"**Support update · {update_status}**")
                        st.write(update["note"])
                        st.caption(format_timestamp(update.get("created_at")))
                if request.get("status") == "submitted" and not request.get("updates"):
                    st.caption("A support agent has not reviewed this request yet.")


def render_new_order_request_form(owner_user_id: str) -> None:
    """Collect an explicit staff-reviewed request; never create or charge an order."""
    pending = st.session_state.get("pending_new_order_request")
    if pending and not pending.get("product_id"):
        st.session_state.pop("pending_new_order_request", None)
        pending = None
        st.info("The product list has been updated. Please select your item again.")
    with st.expander(
        "Request a new order",
        expanded=bool(pending or st.session_state.pop("open_new_order_request", False)),
        key="new-order-request",
    ):
        st.caption(
            "Tell us what you would like. A support agent will confirm availability, pricing, and delivery. "
            "Submitting this request does not place an order or take payment."
        )
        if pending:
            with st.container(border=True):
                st.markdown("**Review your request**")
                st.markdown(f"{pending['quantity']} × {pending['item_description']}")
                if pending.get("notes"):
                    st.caption(pending["notes"])
                st.caption("No order will be placed and no payment will be taken by submitting this request.")
                confirm_col, edit_col = st.columns(2)
                with confirm_col:
                    if st.button(
                        "Confirm and send to support",
                        key="confirm-new-order-request",
                        type="primary",
                        use_container_width=True,
                    ):
                        request = create_new_order_request(
                            owner_user_id,
                            pending["product_id"],
                            pending["quantity"],
                            pending.get("notes", ""),
                        )
                        if not request:
                            st.error("We couldn't submit the request. Please try again shortly.")
                            return
                        st.session_state.pop("pending_new_order_request", None)
                        st.success("Your request was sent to support. Track updates in My support requests.")
                        st.rerun()
                with edit_col:
                    if st.button("Edit request", key="edit-new-order-request", use_container_width=True):
                        st.session_state.pop("pending_new_order_request", None)
                        st.rerun()
        else:
            products = list_active_products()
            if products is None:
                st.error("The product catalog couldn't be loaded. Please try again shortly.")
                return
            if not products:
                st.info("There are no products available to request right now. Please contact support.")
                return
            products_by_id = {int(product["id"]): product for product in products}

            def product_label(product_id: int) -> str:
                product = products_by_id[product_id]
                category = product.get("category") or "General"
                label = f"{category} · {product['name']}"
                if product.get("unit_price") is not None and product.get("currency"):
                    label += f" · {product['currency'].strip()} {float(product['unit_price']):,.2f}"
                return label

            with st.form("new-order-request-form", clear_on_submit=True):
                product_id = st.selectbox(
                    "Product",
                    options=list(products_by_id),
                    format_func=product_label,
                )
                quantity = st.number_input("Quantity", min_value=1, max_value=10, value=1, step=1)
                notes = st.text_area(
                    "Additional details (optional)",
                    max_chars=500,
                    placeholder="Preferred options or questions for support",
                )
                submitted = st.form_submit_button("Review request", type="primary")
            if submitted:
                product = products_by_id[int(product_id)]
                st.session_state.pending_new_order_request = {
                    "product_id": int(product_id),
                    "item_description": product["name"],
                    "quantity": int(quantity),
                    "notes": notes.strip(),
                }
                st.rerun()


def render_chat_interface() -> None:
    render_verified_order()

    st.toggle(
        "Show policy sources",
        value=False,
        key="show_policy_sources",
        help="Show the policy sections used to prepare policy answers.",
    )
    if st.session_state.get("legacy_history_notice"):
        st.caption(
            "Some older messages are unavailable because they were redacted before account-protected chat history was added."
        )

    for message in st.session_state.messages:
        avatar = ":material/support_agent:" if message["role"] == "assistant" else ":material/person:"
        with st.chat_message(message["role"], avatar=avatar):
            render_message_content(message["role"], message["content"])

    prompt = st.chat_input("Ask about an order or how we can help...")
    if not prompt:
        prompt = st.session_state.pop("suggested_prompt", None)
    st.caption("Your chat history is saved to your signed-in account so you can resume conversations.")

    if not prompt:
        render_pending_action()
        return

    st.session_state.messages.append(
        {
            "role": "user",
            "content": prompt,
        }
    )

    with st.chat_message("user", avatar=":material/person:"):
        st.markdown(prompt)

    with st.chat_message("assistant", avatar=":material/support_agent:"):
        loading_slot = st.empty()
        try:
            with loading_slot.container():
                status = st.status("Checking your request...", expanded=False)

            response_parts = []
            for piece in st.session_state.chat_handler.stream_response(
                prompt,
                on_step=lambda label: status.update(label=label, state="running"),
            ):
                if piece:
                    response_parts.append(piece)
            response = "".join(response_parts)
            loading_slot.empty()
            render_message_content("assistant", response)

        except Exception:
            logger.exception("Chat response failed")
            response = "I couldn't complete that lookup. Please verify the search value and try again."
            loading_slot.empty()
            st.markdown(response)

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": response,
        }
    )

    render_pending_action()
    invalidate_recent_chats_cache()


def main() -> None:
    apply_theme()

    if st.session_state.pop("sign_out_requested", False):
        sign_out_user()
    elif st.session_state.pop("new_chat_requested", False):
        start_new_chat()

    identity = current_authenticated_identity()
    if identity is None:
        render_header_for_signed_out()
        render_auth_screen()
        st.stop()

    if identity.is_staff:
        render_staff_console(identity)
        st.stop()

    current_session_id()

    initialize_session_state(identity)

    render_header(identity)
    customer_requests = list_customer_action_requests(identity.user_id)
    if not st.session_state.messages:
        render_dashboard(identity, customer_requests)
    render_new_order_request_form(identity.user_id)
    render_customer_cases(identity.user_id, customer_requests)

    render_chat_interface()
    render_sidebar(identity.user_id, identity.email)


if __name__ == "__main__":
    main()
