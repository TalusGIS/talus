"""Shared audit-context GUCs for the un-bypassable V003/V012 audit trigger.

Every audited write stamps its row from three transaction-local GUCs the trigger reads:
  app.current_user_id    -> changed_by   (the actor's platform.users.id)
  app.current_client     -> client_info  (which client/router originated the write)
  app.current_actor_type -> actor_type   ('human' | 'ai_inference' | 'service_account')

A service-account principal stamps actor_type='service_account' so its edits are
explicitly, immutably attributable in the audit trail (Talus 19, Q6) — distinct from a
person ('human') and from a Vision/AI-inference edit ('ai_inference', set via its own
path). It is derived from User.principal_type and set in ONE place so every feature-write
path (layer_features and the doors that write through it) stamps it identically. The publisher and
symbology write paths keep their own helper: a service account cannot reach them (they
require publisher/admin/owner, never a data-editor service account), so their writes are
always human.
"""
from typing import Any

from .models.user import User


def actor_type_for(user: User) -> str:
    """The audit actor_type for this principal: 'service_account' for a service account,
    else 'human'. (AI-inference edits set actor_type via their own provenance path.)"""
    return "service_account" if user.principal_type == "service" else "human"


async def set_audit_context(cur: Any, user: User, client: str) -> None:
    """Set the transaction-local GUCs the audit trigger stamps onto the row. Call inside
    the same transaction as the mutating statement, before it executes."""
    await cur.execute(
        "SELECT set_config('app.current_user_id', %s, true)", (str(user.id),)
    )
    await cur.execute("SELECT set_config('app.current_client', %s, true)", (client,))
    await cur.execute(
        "SELECT set_config('app.current_actor_type', %s, true)", (actor_type_for(user),)
    )
