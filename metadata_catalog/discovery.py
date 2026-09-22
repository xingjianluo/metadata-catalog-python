"""基于标签/归属的发现与分页查询。

分页稳定性保证：
- 查询在一个**固定快照**上执行（未指定则取当前最新快照，并把该快照号
  通过 ``snapshot_id`` 与下一页 token 一并返回）。后续页携带的 token 内嵌
  同一快照号，因此即使期间有并发写入，整份翻页过程看到的都是同一版本，
  不会出现重复或遗漏。
- 结果集为用户可见实体按 (id) 排序后的确定性列表；token 是
  ``base64("快照号:最后一条id")``，纯键集（keyset）翻页，不依赖偏移。
- 标签过滤：实体包含任一请求标签即命中；owner 过滤要求归属精确相等。
"""
from __future__ import annotations

import base64

from .errors import ValidationError
from .models import User
from .store import InMemoryStore
from .visibility import VisibilityService


class DiscoveryService:
    def __init__(self, store: InMemoryStore, visibility: VisibilityService) -> None:
        self.store = store
        self.visibility = visibility

    @staticmethod
    def _encode_token(snapshot_id: int, last_id: str) -> str:
        raw = f"{snapshot_id}:{last_id}".encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")

    @staticmethod
    def _decode_token(token: str) -> tuple[int, str]:
        try:
            raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
            snap_str, last_id = raw.split(":", 1)
            return int(snap_str), last_id
        except Exception as exc:  # noqa: BLE001 - 统一归一为无效 token
            raise ValidationError(
                f"invalid page token: {token!r}", reason="invalid_page_token"
            ) from exc

    def query(
        self,
        user: User,
        tags: set[str] | None = None,
        owner: str | None = None,
        limit: int = 10,
        page_token: str | None = None,
        snapshot_id: int | None = None,
    ) -> dict:
        if not isinstance(limit, int) or limit <= 0:
            raise ValidationError("limit must be a positive integer",
                                  reason="invalid_limit")
        if limit > 500:
            raise ValidationError("limit must not exceed 500",
                                  reason="invalid_limit")

        if page_token is not None:
            token_snap, last_id = self._decode_token(page_token)
            if snapshot_id is not None and snapshot_id != token_snap:
                raise ValidationError(
                    "page token belongs to a different snapshot; "
                    "restart pagination without the token",
                    reason="snapshot_mismatch",
                )
            snap = token_snap
        else:
            snap = (self.store.current_snapshot() if snapshot_id is None
                    else snapshot_id)
            last_id = None

        visible = self.visibility.visible_entities(user, snap)
        matched_ids = []
        for eid in sorted(visible):
            entity = visible[eid]
            if owner is not None and entity.owner != owner:
                continue
            if tags is not None and not (set(tags) & set(entity.tags)):
                continue
            if last_id is not None and eid <= last_id:
                continue
            matched_ids.append(eid)

        # 多取一条用于判断是否还有下一页，避免末页恰好等于 limit 时
        # 产生一个多余的、结果为空的翻页 token。
        window = matched_ids[: limit + 1]
        page = window[:limit]
        next_token = (
            self._encode_token(snap, page[-1]) if len(window) > limit else None
        )
        return {
            "snapshot_id": snap,
            "items": [visible[eid] for eid in page],
            "next_page_token": next_token,
            "total_matched": len(matched_ids) if last_id is None else None,
        }
