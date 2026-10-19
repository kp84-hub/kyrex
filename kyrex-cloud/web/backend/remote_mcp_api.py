"""Authenticated remote MCP settings; keys and transport stay on the host."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from remote_mcp_connections import RemoteMCPConnections, MCPConnectionError
from kyrex.tools.remote_mcp import RemoteMCPError
import connectors

class SecretSafeRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request):
            owner(request)  # authenticate before parsing any credential body
            try:
                return await original(request)
            except RequestValidationError:
                # FastAPI's default includes rejected input, which may be a key.
                raise HTTPException(422, "Check the MCP connection fields and try again.") from None
        return handle


router = APIRouter(prefix="/api/connections/mcp", route_class=SecretSafeRoute)


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    url: str = Field(max_length=2048)
    token: str = Field(default="", max_length=4096)
    auth_header: str = "Authorization"


class SelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tools: list[str] = Field(max_length=200)


def owner(request):
    import main
    return main.require_user(request)


def store():
    return RemoteMCPConnections()


def perform(fn):
    try:
        return fn()
    except (MCPConnectionError, RemoteMCPError) as exc:
        raise HTTPException(400, str(exc)) from None
    except connectors.ConnectorError:
        raise HTTPException(503, "MCP credential storage is unavailable. Check the server encryption configuration.") from None
    except Exception:
        raise HTTPException(503, "MCP connection storage or service is unavailable.") from None


@router.get("")
def listing(request: Request):
    user = owner(request)
    return perform(lambda: {"connections": store().list(user)})


@router.post("")
def connect(body: ConnectionInput, request: Request):
    user = owner(request)
    return perform(lambda: store().connect(user, **body.model_dump()))


@router.post("/{connection_id}/test")
def test(connection_id: str, request: Request):
    user = owner(request)
    return perform(lambda: store().test(user, connection_id))


@router.put("/{connection_id}/tools")
def select(connection_id: str, body: SelectionInput, request: Request):
    user = owner(request)
    return perform(lambda: store().select(user, connection_id, body.tools))


@router.delete("/{connection_id}")
def remove(connection_id: str, request: Request):
    user = owner(request)
    def delete():
        store().delete(user, connection_id)
        return {"disconnected": True}
    return perform(delete)
