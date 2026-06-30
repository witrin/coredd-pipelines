"""Allow workspace members (annotators) to PATCH record fields.

The Localization CustomField is written from the browser by the annotator, but
stock Argilla gates ``RecordPolicy.update`` to owner/admin, so an annotator
PATCHing the record gets a 403. This overlay relaxes only ``update`` to a
workspace-member check, matching ``RecordPolicy.create_response``. Every other
policy (``delete``, ``create_suggestion``, ``delete_suggestions``, …) is left
exactly as Argilla ships it.

Loaded at interpreter start via ``permissions.pth`` so the override is in place
before any request handler resolves ``RecordPolicy.update``. Nothing in the
installed ``argilla_server`` package is edited.
"""

from argilla_server.api.policies.v1.record_policy import RecordPolicy
from argilla_server.models import Record, User


def _update(record: Record):
    async def is_allowed(actor: User) -> bool:
        return actor.is_owner or await actor.is_member(record.dataset.workspace_id)

    return is_allowed


RecordPolicy.update = classmethod(lambda cls, record: _update(record))
