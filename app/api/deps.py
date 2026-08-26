import uuid

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from app.config import get_settings

_header = APIKeyHeader(name="X-API-Key", auto_error=False)
# Who the frontend says is asking. Asserted by its server, which has already
# validated that person's session cookie -- there is no token here to verify,
# because their sessions are opaque ids that mean nothing outside their own
# database.
#
# This is identity, not authorisation. The API key is still the thing that
# decides whether a request is allowed; this only records whose request it was.
# Anyone holding the key can claim to be anyone, and until the service is
# reachable only by their server that stays true. Worth knowing before this is
# ever used to decide what somebody may do, rather than to write down what they
# did.
_user = APIKeyHeader(name="X-PlanckOff-User", auto_error=False)
_role = APIKeyHeader(name="X-PlanckOff-Role", auto_error=False)


async def require_api_key(key: str | None = Security(_header)) -> str:
    settings = get_settings()
    if not key or key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key."
        )
    return key


class Caller(BaseModel):
    """Who is making this request.

    Endpoints depend on this rather than on how it was proved, so a machine
    credential or a verified token can be added later as another way to build
    one, without touching anything that uses it.
    """

    user_id: str | None = None
    role: str = ""

    @property
    def known(self) -> bool:
        return self.user_id is not None


async def require_caller(
    _key: str = Depends(require_api_key),
    user_id: str | None = Security(_user),
    role: str | None = Security(_role),
) -> Caller:
    """The API key as before, plus whoever the frontend says is behind it.

    The user header is optional. A caller that does not send one still works
    and its writes are simply unattributed -- so the frontend can adopt this a
    route at a time instead of all at once.
    """
    if user_id:
        try:
            uuid.UUID(user_id)
        except ValueError as exc:
            # Loudly. A malformed id would otherwise reach the database and
            # come back as a 500 from a column type, which says nothing useful.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-PlanckOff-User must be a uuid.",
            ) from exc
    return Caller(user_id=user_id or None, role=(role or "").strip())
