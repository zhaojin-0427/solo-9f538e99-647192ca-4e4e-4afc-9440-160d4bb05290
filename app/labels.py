"""号表（标签）版本管理。

- 每个遗址维护一条只增不减的号表版本链：第 1 版（初版）在库首次启用/旧库
  升级时由现有编号建立，之后每次“新增层位”或“批量改号”成功提交都生成
  新一版。
- 版本表是完整快照（层位 id -> 探方/层号），因此历史版号在重启、再改号之后
  仍可原样查询；层位 id、年代段与对接/叠压关系始终指向原层位，不因改号改变。
- 批量改号只作用于一个探方：按整批改完后的结果检查该探方内是否重号，
  批内互换不会被误判；改完仍有重号则整批拒绝，现行号表与历史照旧。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from . import db

# 改号时的临时占位前缀：互换需要先把本批层位腾空，避免撞唯一约束
_TMP_PREFIX = "\x00tmp:"


class StaleLabelVersion(Exception):
    """提交所用版号不是现行版。"""

    def __init__(self, site_id: int, submitted: int, current: Optional[int]):
        self.site_id = site_id
        self.submitted = submitted
        self.current = current
        super().__init__(
            f"号表版号 {submitted} 已过期，现行版为 {current}"
        )


class RenumberConflict(Exception):
    """整批改号校验失败：重号、层位不属于该探方、批内重复等。"""

    def __init__(self, message: str, reason: str = "DUPLICATE_LAYER_NO"):
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class RenumberPlan:
    """校验通过、尚未落库的整批改号方案。"""

    site_id: int
    trench: str
    base_version: int
    new_version: int
    changes: list[tuple[int, str]]  # (stratum_id, 新层号)


def current_version(conn: sqlite3.Connection, site_id: int) -> Optional[int]:
    return db.get_current_label_version(conn, site_id)


def require_current(conn: sqlite3.Connection, site_id: int, submitted: int) -> int:
    """返回现行版号；提交版号过期则抛 StaleLabelVersion。"""
    current = current_version(conn, site_id)
    if current != submitted:
        raise StaleLabelVersion(site_id, submitted, current)
    return current  # type: ignore[return-value]


def plan_renumber(
    conn: sqlite3.Connection,
    site_id: int,
    submitted_version: int,
    trench: str,
    entries: list[tuple[int, str]],
) -> RenumberPlan:
    """在现行版号表基础上规划一次整批改号，只做校验，不写库。

    校验项：
    1. 提交版号必须等于现行版；
    2. 层位必须属于本遗址，且在其现行号表中的探方等于请求探方；
    3. 同一层位在一批中不得出现两次；
    4. 按整批改完后的结果，该探方内层号不得重复（批内互换因此合法）。
    """
    base_version = require_current(conn, site_id, submitted_version)

    seen: set[int] = set()
    for stratum_id, _ in entries:
        if stratum_id in seen:
            raise RenumberConflict(
                f"层位 {stratum_id} 在同一批中重复出现", "INVALID_STRATUM"
            )
        seen.add(stratum_id)
        current_entry = db.get_current_label_entry(conn, site_id, stratum_id)
        if current_entry is None or current_entry["trench"] != trench:
            raise RenumberConflict(
                f"层位 {stratum_id} 不属于探方 {trench}（或不属于遗址 {site_id}）",
                "INVALID_STRATUM",
            )

    # 该探方在现行版中的完整层号占用情况，叠加本批改动后检查
    pairs = db.label_pairs_in_trench(conn, site_id, base_version, trench)
    for stratum_id, _ in entries:
        for layer_no, owner in pairs.items():
            if owner == stratum_id:
                del pairs[layer_no]
                break
    for stratum_id, new_layer_no in entries:
        other = pairs.get(new_layer_no)
        if other is not None:
            raise RenumberConflict(
                f"改号后层号 {new_layer_no!r} 在该探方内重复"
                f"（层位 {other} 已占用）"
            )
        pairs[new_layer_no] = stratum_id

    return RenumberPlan(
        site_id=site_id,
        trench=trench,
        base_version=base_version,
        new_version=base_version + 1,
        changes=list(entries),
    )


def apply_renumber(conn: sqlite3.Connection, plan: RenumberPlan) -> int:
    """在调用方事务内执行改号并生成新版号表，返回新版号。

    先以临时占位名腾空本批层位，再写入最终层号，使整批互换在
    ``UNIQUE(site_id, trench, layer_no)`` 约束下也能成立；临时名含
    层位 id 且位于独立命名空间，不会与真实层号相撞。
    """
    for stratum_id, _ in plan.changes:
        db.rename_stratum(
            conn, stratum_id, plan.trench, f"{_TMP_PREFIX}{stratum_id}"
        )
    for stratum_id, new_layer_no in plan.changes:
        db.rename_stratum(conn, stratum_id, plan.trench, new_layer_no)
    db.snapshot_labels(conn, plan.site_id, plan.new_version)
    return plan.new_version


def record_new_stratum(conn: sqlite3.Connection, site_id: int) -> int:
    """新增层位成功时，把整表现行编号复制为新一版号表，返回新版号。"""
    version = db.next_label_version(conn, site_id)
    db.snapshot_labels(conn, site_id, version)
    return version
