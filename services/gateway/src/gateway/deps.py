"""FastAPI dependencies: DB session + current authenticated user."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from db.models import User
from db.session import get_sessionmaker
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.security import decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/login")


async def get_db() -> AsyncIterator[AsyncSession]:
    maker = get_sessionmaker()
    async with maker() as session:
        yield session


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    subject = decode_access_token(token)
    user_id: UUID | None = None
    if subject is not None:
        try:
            user_id = UUID(subject)
        except ValueError:
            user_id = None
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user")
    return user
