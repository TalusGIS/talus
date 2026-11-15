from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class User(BaseModel):
    id: UUID
    username: str
    email: str
    display_name: str
    # 'human' (a person, lazily created on first login) or 'service' (a non-interactive
    # service account, provisioned by bootstrap-19-service-accounts.sh). Defaults to
    # 'human' so STUB_USER and any human-only construction site is unaffected. Drives the
    # audit actor_type (commit 3) and admin filtering — NOT the auth gate. (Talus 19, Q2.)
    principal_type: Literal["human", "service"] = "human"
    # Does this principal hold the talus-admin realm role? Populated once at auth time from the token
    # (auth.get_current_user / get_optional_user). Lets view_allowed grant admins god-mode READ access to
    # every layer/map without threading claims through every read gate (admin-sees-all, 2026-06-30). Default
    # False so any non-auth construction site (tests, service helpers) is non-admin unless explicitly set.
    is_admin: bool = False
