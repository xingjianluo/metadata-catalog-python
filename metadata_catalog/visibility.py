"""基于归属与显式授权的可见性控制。

可见性判定规则（对任一固定快照，结论确定）：
1. 管理员（``is_admin=True``）可见全部实体；
2. 实体的 ``owner`` 可见自己拥有的实体；
3. 被显式授予 owner 级读权限的用户，可见该快照下全部实体；
4. 被显式逐实体授权的用户，仅可见被授予的实体；
5. 其余情况不可见。

越权访问的处理原则：``require_read`` 对"不存在"与"存在但无权限"返回
**不同原因码**（``not_found`` / ``permission_denied``），
绝不以"返回空结果"掩盖越权。
一条血缘仅当其两端实体对该用户均可见时才对该用户可见，
因此用户不可能通过血缘边推断出无权实体的存在。
"""
from __future__ import annotations

from .errors import NotFoundError, PermissionDeniedError
from .models import LineageEdge, User
from .store import InMemoryStore


class VisibilityService:
    def __init__(self, store: InMemoryStore) -> None:
        self.store = store

    def grant(self, user_name: str, entity_id: str) -> int:
        """显式授予某用户对某实体的读权限。"""
        if not user_name or not entity_id:
            raise ValueError("user_name and entity_id must be non-empty")
        with self.store.transaction():
            snap = self.store.current_snapshot()
            entity = self.store.get_entity_at(entity_id, snap)
            if entity is None:
                raise NotFoundError(
                    f"cannot grant: entity not found: {entity_id!r}"
                )
            new_snap = self.store.begin()
            self.store.grant(new_snap, user_name, entity_id)
            return new_snap

    def grant_owner_access(self, user_name: str) -> int:
        """授予 owner 级读权限（可读取该授权快照之后可见的全部实体）。"""
        if not user_name:
            raise ValueError("user_name must be non-empty")
        with self.store.transaction():
            new_snap = self.store.begin()
            self.store.grant_owner_access(new_snap, user_name)
            return new_snap

    def _snapshot(self, snapshot_id: int | None) -> int:
        return self.store.current_snapshot() if snapshot_id is None \
            else snapshot_id

    def can_read(self, user: User, entity_id: str,
                 snapshot_id: int | None = None) -> bool:
        snap = self._snapshot(snapshot_id)
        entity = self.store.get_entity_at(entity_id, snap)
        if entity is None:
            return False
        if user.is_admin:
            return True
        if entity.owner == user.name:
            return True
        per_entity, owner_readers = self.store.grants_at(snap)
        if user.name in owner_readers:
            return True
        return entity_id in per_entity.get(user.name, set())

    def require_read(self, user: User, entity_id: str,
                     snapshot_id: int | None = None) -> int:
        """校验读权限，返回判定所用快照号；失败抛出可区分异常。"""
        snap = self._snapshot(snapshot_id)
        if self.store.get_entity_at(entity_id, snap) is None:
            raise NotFoundError(f"entity not found: {entity_id!r}")
        if not self.can_read(user, entity_id, snap):
            raise PermissionDeniedError(
                f"user {user.name!r} is not authorized to read "
                f"entity {entity_id!r}",
                reason="permission_denied",
            )
        return snap

    def visible_entities(self, user: User,
                         snapshot_id: int | None = None) -> dict:
        """返回用户在该快照下可见的实体字典。"""
        snap = self._snapshot(snapshot_id)
        view = self.store.view_at(snap)
        if user.is_admin:
            return dict(view.entities)
        per_entity, owner_readers = self.store.grants_at(snap)
        granted = per_entity.get(user.name, set())
        owner_all = user.name in owner_readers
        result = {}
        for eid, entity in view.entities.items():
            if owner_all or entity.owner == user.name or eid in granted:
                result[eid] = entity
        return result

    def visible_edges(self, user: User,
                      snapshot_id: int | None = None) -> tuple[LineageEdge, ...]:
        """返回两端实体均对用户可见的血缘边。"""
        snap = self._snapshot(snapshot_id)
        view = self.store.view_at(snap)
        visible = self.visible_entities(user, snap)
        return tuple(
            e for e in view.edges
            if e.source_entity in visible and e.target_entity in visible
        )
