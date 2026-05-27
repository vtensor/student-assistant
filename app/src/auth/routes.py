# Auth HTTP routes: signup, login, refresh. The only public-facing module
# that mints JWTs.
from fastapi import APIRouter, Depends

from app.dependencies import get_jwt
from app.src.auth import Identity
from app.src.auth.models import LoginBody, SignupBody, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])
_identity = Identity()


@router.post("/signup", response_model=TokenResponse, status_code=201)
async def signup(body: SignupBody) -> TokenResponse:
    return await _identity.signup(body)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginBody) -> TokenResponse:
    return await _identity.login(body)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(jwt_token: str = Depends(get_jwt)) -> TokenResponse:
    return await _identity.refresh(jwt_token)
