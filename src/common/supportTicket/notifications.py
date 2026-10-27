"""In-app notification helpers for support tickets.

Every ticket change that a user should hear about goes through one of the two
``notify_*`` functions below. They only ``session.add`` rows — the caller owns
the transaction, so a notification is committed atomically with the change
that produced it.

Recipients
----------
* **reporter** — the user who raised the ticket. Identified by the ticket's
  ``source`` (``portal`` / ``admin``), ``con_org_id`` and ``reporter_user_id``,
  because portal ``user_mst.user_id`` values collide across tenant databases.
* **vow** — the Control Desk member the ticket is assigned to
  (``assigned_to_con_user_id``). Unassigned tickets notify no VOW member.
"""

from typing import Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import SupportTicket, SupportTicketNotification

# ── Event types (stored in support_ticket_notification.event_type) ─────────────
EVENT_ASSIGNED = "assigned"
EVENT_STATUS_CHANGED = "status_changed"
EVENT_COMMENT = "comment"
EVENT_ATTACHMENT = "attachment"

RECIPIENT_REPORTER = "reporter"
RECIPIENT_VOW = "vow"

_MESSAGE_MAX = 500


def _clip(value: Optional[str], limit: int = _MESSAGE_MAX) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _title(ticket: SupportTicket, headline: str) -> str:
    return _clip(f"{ticket.ticket_no or f'#{ticket.ticket_id}'}: {headline}", 200) or headline


def notify_reporter(
    session: Session,
    ticket: SupportTicket,
    event_type: str,
    headline: str,
    message: Optional[str] = None,
    actor_name: Optional[str] = None,
) -> Optional[SupportTicketNotification]:
    """Queue a notification for the user who raised ``ticket``."""
    if not ticket.reporter_user_id:
        return None
    row = SupportTicketNotification(
        ticket_id=ticket.ticket_id,
        recipient_type=RECIPIENT_REPORTER,
        recipient_source=ticket.source or "portal",
        recipient_org_id=ticket.con_org_id,
        recipient_user_id=int(ticket.reporter_user_id),
        event_type=event_type,
        title=_title(ticket, headline),
        message=_clip(message),
        actor_name=actor_name,
        is_read=0,
    )
    session.add(row)
    return row


def notify_assignee(
    session: Session,
    ticket: SupportTicket,
    event_type: str,
    headline: str,
    message: Optional[str] = None,
    actor_name: Optional[str] = None,
    exclude_con_user_id: Optional[int] = None,
) -> Optional[SupportTicketNotification]:
    """Queue a notification for the VOW member assigned to ``ticket``.

    ``exclude_con_user_id`` suppresses self-notifications (the assignee acting
    on their own ticket should not be told about it).
    """
    assignee = ticket.assigned_to_con_user_id
    if not assignee:
        return None
    if exclude_con_user_id is not None and int(assignee) == int(exclude_con_user_id):
        return None
    row = SupportTicketNotification(
        ticket_id=ticket.ticket_id,
        recipient_type=RECIPIENT_VOW,
        recipient_source=None,
        recipient_org_id=None,
        recipient_user_id=int(assignee),
        event_type=event_type,
        title=_title(ticket, headline),
        message=_clip(message),
        actor_name=actor_name,
        is_read=0,
    )
    session.add(row)
    return row


def resolve_read_scope(payload) -> Tuple[str, dict]:
    """Translate a ``MarkNotificationsReadRequest`` into a query scope + binds.

    Precedence: explicit ``notification_ids`` > ``ticket_id`` > ``all``.
    """
    if payload.notification_ids:
        return "ids", {"ids": [int(i) for i in payload.notification_ids]}
    if payload.ticket_id:
        return "ticket", {"ticket_id": int(payload.ticket_id)}
    if payload.all:
        return "all", {}
    raise HTTPException(status_code=400, detail="Provide notification_ids, ticket_id or all=true")
