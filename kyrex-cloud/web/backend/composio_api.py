"""Owner-authenticated Composio setup. Credentials never enter the browser."""
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from composio_connections import ComposioConnections
from remote_mcp_api import SecretSafeRoute, owner, perform

router = APIRouter(prefix="/api/connections/composio", route_class=SecretSafeRoute)


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def service():
    return ComposioConnections()


@router.get("")
def listing(request: Request):
    user = owner(request)
    return perform(lambda: service().listing(user))


@router.post("/apps/{toolkit}")
def start(toolkit: str, body: EmptyInput, request: Request):
    user = owner(request)
    return perform(lambda: service().start(user, toolkit))


@router.post("/{connection_id}/complete")
def complete(connection_id: str, body: EmptyInput, request: Request):
    user = owner(request)
    return perform(lambda: service().complete(user, connection_id))
