from fastapi import APIRouter
from sqlalchemy import select

from app.core.deps import CfoUser, DbSession
from app.core.errors import ConflictError, NotFoundError
from app.models import User
from app.schemas import UserCreate, UserOut, UserUpdate
from app.services import auth as auth_service
from app.services.audit import log_audit

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserOut])
def list_users(db: DbSession, actor: CfoUser) -> list[User]:
    return list(db.scalars(select(User).where(User.organization_id == actor.organization_id).order_by(User.id)))


@router.post("", response_model=UserOut, status_code=201)
def create_user(payload: UserCreate, db: DbSession, actor: CfoUser) -> User:
    user = auth_service.create_user(
        db,
        full_name=payload.full_name,
        email=payload.email,
        password=payload.password,
        role=payload.role.value,
        actor=actor.email,
    )
    db.commit()
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdate, db: DbSession, actor: CfoUser) -> User:
    if user_id == actor.id:
        raise ConflictError("You cannot change your own role or active status")
    user = db.scalar(select(User).where(User.id == user_id, User.organization_id == actor.organization_id))
    if user is None:
        raise NotFoundError("User not found")
    changes: list[str] = []
    if payload.role is not None and payload.role.value != user.role:
        changes.append(f"role {user.role} -> {payload.role.value}")
        user.role = payload.role.value
    if payload.is_active is not None and payload.is_active != user.is_active:
        changes.append(f"active -> {payload.is_active}")
        user.is_active = payload.is_active
        if not payload.is_active:
            auth_service.revoke_all_for_user(db, user.id)
    if changes:
        log_audit(db, actor.email, "USER_UPDATED", f"user:{user.id}", "; ".join(changes))
    db.commit()
    return user
