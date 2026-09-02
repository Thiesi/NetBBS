"""
Client for the project-operated managed netbbs.org subdomain + dynamic
DNS service (design doc §16, issue #201).

A node opts in (`netbbs.managed_dns.state`), registers a subdomain
against the managed service's own API (`netbbs.managed_dns.client`,
built separately as `services/managed_dns/` -- not part of this
installable package), and periodically calls back in
(`netbbs.managed_dns.updater`) to keep the registration alive and, for a
dynamic-IP board, keep its DNS record current. The per-registration
bearer credential the service hands back is a separate, auto-generated
secret (design doc §16 Decision 2) -- never this node's own Ed25519 key
-- stored on disk by `netbbs.managed_dns.credential`, and (once
`netbbs.backup` is updated for it, design doc §16 Decision 7) covered by
node backup/restore the same way the SSH host key already is.
"""

from __future__ import annotations

from netbbs.managed_dns.credential import (
    credential_path_for,
    delete_credential,
    load_credential,
    save_credential,
)
from netbbs.managed_dns.state import (
    OptIn,
    RegistrationStatus,
    get_dynamic,
    get_last_contact_at,
    get_opt_in,
    get_registered_name,
    get_registration_status,
    set_dynamic,
    set_last_contact_at,
    set_opt_in,
    set_registered_name,
    set_registration_status,
)

__all__ = [
    "credential_path_for",
    "save_credential",
    "load_credential",
    "delete_credential",
    "OptIn",
    "RegistrationStatus",
    "get_opt_in",
    "set_opt_in",
    "get_registered_name",
    "set_registered_name",
    "get_registration_status",
    "set_registration_status",
    "get_last_contact_at",
    "set_last_contact_at",
    "get_dynamic",
    "set_dynamic",
]
