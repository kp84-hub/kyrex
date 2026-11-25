"""Authenticated search-key setup; no credential can appear in validation errors."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

import connectors
from web_search import SearchConnection, SearchError


def owner(request):
    import main
    user = main.require_user(request)
    if request.method != 'GET':
        from account_api import same_origin
        same_origin(request)
    return user


class SecretSafeRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request):
            owner(request)
            try:
                return await original(request)
            except RequestValidationError:
                raise HTTPException(422, 'Check the web search key and try again.') from None
        return handle


router = APIRouter(prefix='/api/connections/web-search', route_class=SecretSafeRoute)


class KeyInput(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    key: str = Field(min_length=8, max_length=4096)


def perform(fn):
    try:
        return JSONResponse(fn(), headers={'Cache-Control': 'no-store'})
    except SearchError as exc:
        raise HTTPException(400, str(exc)) from None
    except connectors.ConnectorError:
        raise HTTPException(503, 'Encrypted web search storage is unavailable.') from None
    except Exception:
        raise HTTPException(503, 'Web search settings are unavailable.') from None


@router.get('')
def status(request: Request):
    user = owner(request)
    return perform(lambda: SearchConnection().view(user))


@router.put('')
def save(body: KeyInput, request: Request):
    user = owner(request)
    return perform(lambda: SearchConnection().save(user, body.key))


@router.delete('')
def disconnect(request: Request):
    user = owner(request)
    return perform(lambda: SearchConnection().delete(user))
