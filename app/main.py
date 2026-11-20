"""FastAPI 应用工厂与路由。

并发语义：所有写操作在进程内同一把锁内、同一个 SQLite 事务中完成
“校验 -> 写入 -> 重算求解 -> 提交”，按提交顺序串行核算。若求解无解，
事务回滚，库内保留原有对接与年代结论（查询仍返回上一版可行结果）。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import db, labels
from .schemas import (
    CorrelationCreate,
    RenumberRequest,
    SiteCreate,
    StratumCreate,
    SuperpositionCreate,
)
from .service import load_and_solve, serialize_solution, serialize_stratum

MAX_STRATA_PER_SITE = 40


class ConflictError(HTTPException):
    """语义冲突（409）：约束无解或违反业务规则。"""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        content: dict[str, Any] = {"detail": message}
        if details:
            content.update(details)
        super().__init__(status_code=409, detail=content)


class _Infeasible(Exception):
    """事务内部使用：新记录使整站约束无解，触发回滚。"""


def _default_db_path() -> str:
    configured = os.environ.get("DB_PATH")
    if configured:
        return configured
    # 容器内 /data 由卷挂载；本地裸跑时回退到工作目录下的 data/
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return "/data/archaeo.db"
    return os.path.join(os.getcwd(), "data", "archaeo.db")


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or _default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    conn = db.connect(db_path)
    db.init_db(conn)
    write_lock = threading.Lock()

    app = FastAPI(
        title="考古地层对接 API",
        version="1.0.0",
        description=(
            "录入同一遗址（最多 40 个层位）的探方、层位编号与可能沉积年代段"
            "（整年闭区间，可含空档），登记跨探方层位对接与下层→上层叠压关系；"
            "直接或间接对接的层位视为同一次沉积并共用一个年份，自动完成分组、"
            "成环检测与可行年代段计算。"
        ),
    )
    app.state.conn = conn
    app.state.write_lock = write_lock

    # ---------- 内部辅助 ----------

    def _require_site(site_id: int):
        site = db.get_site(conn, site_id)
        if site is None:
            raise HTTPException(404, f"遗址 {site_id} 不存在")
        return site

    def _require_stratum(stratum_id: int, site_id: int):
        row = db.get_stratum(conn, stratum_id)
        if row is None or row["site_id"] != site_id:
            raise HTTPException(
                404, f"层位 {stratum_id} 不属于遗址 {site_id} 或不存在"
            )
        return row

    def _solution_payload(site_id: int) -> dict[str, Any]:
        solution = load_and_solve(conn, site_id)
        return serialize_solution(solution, db.get_version(conn))

    def _contradiction_payload(site_id: int) -> dict[str, Any]:
        """读取当前连接视图（未提交的新记录）中的矛盾见证。"""
        solution = load_and_solve(conn, site_id)
        if solution.contradiction is None:  # 理论上不会发生
            return {"reason": "UNKNOWN", "message": "约束不成立", "witness": []}
        c = solution.contradiction
        return {
            "reason": c.reason,
            "message": c.message,
            "witness": [
                {"kind": w.kind, "record_id": w.record_id, "detail": w.detail}
                for w in c.witness
            ],
        }

    def _reject_infeasible(site_id: int, message: str) -> None:
        """事务内调用：先抓取新视图中的矛盾，再抛异常触发事务回滚。"""
        contradiction = _contradiction_payload(site_id)
        raise _Infeasible((message, contradiction))

    # ---------- 遗址 ----------

    @app.post("/sites", status_code=201, tags=["sites"])
    def create_site(body: SiteCreate):
        with write_lock:
            if db.get_site_by_name(conn, body.name) is not None:
                raise ConflictError(f"遗址名称 {body.name!r} 已存在")
            with db.transaction(conn):
                site = db.create_site(conn, body.name)
                db.bump_version(conn)
            return {"record": dict(site), "solution": _solution_payload(site["id"])}

    @app.get("/sites", tags=["sites"])
    def list_sites():
        return [dict(r) for r in db.list_sites(conn)]

    @app.get("/sites/{site_id}", tags=["sites"])
    def get_site(site_id: int):
        return dict(_require_site(site_id))

    # ---------- 层位 ----------

    @app.post("/sites/{site_id}/strata", status_code=201, tags=["strata"])
    def create_stratum(site_id: int, body: StratumCreate):
        _require_site(site_id)
        intervals = [(iv.year_from, iv.year_to) for iv in body.intervals]
        with write_lock:
            if db.count_strata(conn, site_id) >= MAX_STRATA_PER_SITE:
                raise ConflictError(
                    f"同一遗址最多 {MAX_STRATA_PER_SITE} 个层位，已达上限"
                )
            if db.find_stratum(conn, site_id, body.trench, body.layer_no) is not None:
                raise ConflictError(
                    f"探方 {body.trench} 的层位 {body.layer_no} 已存在"
                )
            try:
                with db.transaction(conn):
                    row = db.create_stratum(
                        conn, site_id, body.trench, body.layer_no, body.note, intervals
                    )
                    label_version = labels.record_new_stratum(conn, site_id)
                    db.bump_version(conn)
                    if not load_and_solve(conn, site_id).feasible:
                        _reject_infeasible(
                            site_id, "新层位的年代段与现有约束无共同解，已拒绝录入"
                        )
            except _Infeasible as exc:
                message, contradiction = exc.args[0]
                raise ConflictError(
                    message,
                    {
                        "contradiction": contradiction,
                        "previous_solution": _solution_payload(site_id),
                    },
                )
            return {
                "record": serialize_stratum(conn, row),
                "label_version": label_version,
                "solution": _solution_payload(site_id),
            }

    @app.get("/sites/{site_id}/strata", tags=["strata"])
    def list_strata(site_id: int):
        _require_site(site_id)
        return [serialize_stratum(conn, r) for r in db.list_strata(conn, site_id)]

    @app.get("/strata/{stratum_id}", tags=["strata"])
    def get_stratum(stratum_id: int):
        row = db.get_stratum(conn, stratum_id)
        if row is None:
            raise HTTPException(404, f"层位 {stratum_id} 不存在")
        return serialize_stratum(conn, row)

    # ---------- 号表（标签）版本 ----------

    @app.get("/sites/{site_id}/label-versions", tags=["labels"])
    def list_label_versions(site_id: int):
        """该遗址全部号表版本（含现行版）。"""
        _require_site(site_id)
        current = db.get_current_label_version(conn, site_id)
        return [
            {
                "site_id": site_id,
                "version": r["version"],
                "created": r["created"],
                "is_current": r["version"] == current,
            }
            for r in db.list_label_versions(conn, site_id)
        ]

    @app.post("/sites/{site_id}/label-versions/renumber", tags=["labels"])
    def renumber_trench(
        site_id: int, body: RenumberRequest, version: int | None = None
    ):
        """按指定版号提交一批层位改号；成功当次返回新版号。

        提交的现行版号可用查询参数 ``version`` 给出，也可放在请求体
        ``label_version`` 中（二者都给时必须一致）。请求体另给出探方与该批
        层位的新层号。按整批改完后的结果检查该探方内重号，允许互换。
        版号过期、改完仍有重号或写入失败时，现行号表和历史均保持原样。
        """
        _require_site(site_id)
        submitted = version if version is not None else body.label_version
        if submitted is None:
            raise HTTPException(
                422, "缺少号表版号：请在查询参数 version 或请求体 label_version 中给出"
            )
        if (
            version is not None
            and body.label_version is not None
            and version != body.label_version
        ):
            raise HTTPException(
                422, "查询参数 version 与请求体 label_version 不一致"
            )
        entries = [(e.stratum_id, e.layer_no) for e in body.entries]
        with write_lock:
            try:
                plan = labels.plan_renumber(
                    conn, site_id, submitted, body.trench, entries
                )
            except labels.StaleLabelVersion as exc:
                raise ConflictError(
                    str(exc),
                    {
                        "reason": "STALE_LABEL_VERSION",
                        "submitted_version": exc.submitted,
                        "current_version": exc.current,
                    },
                )
            except labels.RenumberConflict as exc:
                raise ConflictError(
                    str(exc),
                    {
                        "reason": exc.reason,
                        "current_version": db.get_current_label_version(
                            conn, site_id
                        ),
                    },
                )
            try:
                with db.transaction(conn):
                    new_version = labels.apply_renumber(conn, plan)
                    db.bump_version(conn)
            except (sqlite3.Error, labels.StaleLabelVersion, labels.RenumberConflict):
                # 写入失败：事务已回滚，号表版本与层位编号均回到提交前
                raise ConflictError(
                    "改号写入失败，现行号表与历史均未改变",
                    {
                        "reason": "LABEL_WRITE_FAILED",
                        "current_version": db.get_current_label_version(
                            conn, site_id
                        ),
                    },
                )
            return {
                "site_id": site_id,
                "label_version": new_version,
                "trench": body.trench,
                "entries": [
                    {
                        "stratum_id": sid,
                        "layer_no": layer_no,
                        "label_version": new_version,
                    }
                    for sid, layer_no in entries
                ],
            }

    @app.get("/sites/{site_id}/label-versions/{version}", tags=["labels"])
    def get_label_version(site_id: int, version: int):
        """某一历史版号表的完整编号（版号过期后仍可查）。"""
        _require_site(site_id)
        head = db.get_label_version_row(conn, site_id, version)
        if head is None:
            raise HTTPException(
                404, f"遗址 {site_id} 不存在号表版号 {version}"
            )
        current = db.get_current_label_version(conn, site_id)
        return {
            "site_id": site_id,
            "version": version,
            "created": head["created"],
            "is_current": version == current,
            "entries": [
                {
                    "stratum_id": r["stratum_id"],
                    "trench": r["trench"],
                    "layer_no": r["layer_no"],
                }
                for r in db.list_label_entries(conn, site_id, version)
            ],
        }

    @app.get("/sites/{site_id}/labels", tags=["labels"])
    def lookup_label(
        site_id: int, version: int, trench: str, layer_no: str
    ):
        """按遗址 + 标签版号 + 探方 + 层号，返回当时对应的层位及其现层号。

        即使该旧层号后来已给另一层使用，也按版号快照返回原层位。
        """
        _require_site(site_id)
        head = db.get_label_version_row(conn, site_id, version)
        if head is None:
            raise HTTPException(404, f"遗址 {site_id} 不存在号表版号 {version}")
        entry = db.find_label_at(conn, site_id, version, trench, layer_no)
        if entry is None:
            raise HTTPException(
                404, f"版号 {version} 的探方 {trench} 中没有层号 {layer_no!r}"
            )
        stratum_id = entry["stratum_id"]
        stratum = db.get_stratum(conn, stratum_id)
        current_version = db.get_current_label_version(conn, site_id)
        return {
            "site_id": site_id,
            "label_version": version,
            "trench": trench,
            "layer_no": layer_no,
            "stratum_id": stratum_id,
            "current_layer_no": stratum["layer_no"] if stratum else None,
            "current_trench": stratum["trench"] if stratum else None,
            "current_label_version": current_version,
        }

    # ---------- 对接 ----------

    @app.post("/sites/{site_id}/correlations", status_code=201, tags=["correlations"])
    def create_correlation(site_id: int, body: CorrelationCreate):
        _require_site(site_id)
        if body.stratum_a_id == body.stratum_b_id:
            raise ConflictError("对接的两个层位不能相同")
        with write_lock:
            row_a = _require_stratum(body.stratum_a_id, site_id)
            row_b = _require_stratum(body.stratum_b_id, site_id)
            if row_a["trench"] == row_b["trench"]:
                raise ConflictError(
                    "对接只能登记不同探方的层位（同一探方层位编号天然分层）"
                )
            if db.find_correlation(
                conn, body.stratum_a_id, body.stratum_b_id
            ) is not None:
                raise ConflictError("这两个层位之间的对接已存在")
            try:
                with db.transaction(conn):
                    row = db.create_correlation(
                        conn, site_id, body.stratum_a_id, body.stratum_b_id
                    )
                    db.bump_version(conn)
                    if not load_and_solve(conn, site_id).feasible:
                        _reject_infeasible(
                            site_id, "该对接会使约束无解（无法共用年份或叠压成环），已拒绝"
                        )
            except _Infeasible as exc:
                message, contradiction = exc.args[0]
                raise ConflictError(
                    message,
                    {
                        "contradiction": contradiction,
                        "previous_solution": _solution_payload(site_id),
                    },
                )
            return {"record": dict(row), "solution": _solution_payload(site_id)}

    @app.get("/sites/{site_id}/correlations", tags=["correlations"])
    def list_correlations(site_id: int):
        _require_site(site_id)
        return [dict(r) for r in db.list_correlations(conn, site_id)]

    @app.delete("/correlations/{correlation_id}", tags=["correlations"])
    def delete_correlation(correlation_id: int):
        with write_lock:
            row = db.get_correlation(conn, correlation_id)
            if row is None:
                raise HTTPException(404, f"对接 {correlation_id} 不存在")
            site_id = row["site_id"]
            with db.transaction(conn):
                db.delete_correlation(conn, correlation_id)
                db.bump_version(conn)
            # 撤销只会放松约束，按剩余对接重算必然可行
            return {"deleted": correlation_id, "solution": _solution_payload(site_id)}

    # ---------- 叠压 ----------

    @app.post("/sites/{site_id}/superpositions", status_code=201, tags=["superpositions"])
    def create_superposition(site_id: int, body: SuperpositionCreate):
        _require_site(site_id)
        if body.lower_id == body.upper_id:
            raise ConflictError("下层与上层不能是同一个层位")
        with write_lock:
            _require_stratum(body.lower_id, site_id)
            _require_stratum(body.upper_id, site_id)
            if db.find_superposition(conn, body.lower_id, body.upper_id) is not None:
                raise ConflictError("该叠压关系已存在")
            try:
                with db.transaction(conn):
                    row = db.create_superposition(
                        conn, site_id, body.lower_id, body.upper_id
                    )
                    db.bump_version(conn)
                    if not load_and_solve(conn, site_id).feasible:
                        _reject_infeasible(
                            site_id, "该叠压关系无解（成环或下层沉积年晚于上层），已拒绝"
                        )
            except _Infeasible as exc:
                message, contradiction = exc.args[0]
                raise ConflictError(
                    message,
                    {
                        "contradiction": contradiction,
                        "previous_solution": _solution_payload(site_id),
                    },
                )
            return {"record": dict(row), "solution": _solution_payload(site_id)}

    @app.get("/sites/{site_id}/superpositions", tags=["superpositions"])
    def list_superpositions(site_id: int):
        _require_site(site_id)
        return [dict(r) for r in db.list_superpositions(conn, site_id)]

    @app.delete("/superpositions/{superposition_id}", tags=["superpositions"])
    def delete_superposition(superposition_id: int):
        with write_lock:
            row = db.get_superposition(conn, superposition_id)
            if row is None:
                raise HTTPException(404, f"叠压关系 {superposition_id} 不存在")
            site_id = row["site_id"]
            with db.transaction(conn):
                db.delete_superposition(conn, superposition_id)
                db.bump_version(conn)
            return {"deleted": superposition_id, "solution": _solution_payload(site_id)}

    # ---------- 查询 ----------

    @app.get("/sites/{site_id}/solution", tags=["query"])
    def get_solution(site_id: int):
        """整站分组、组间叠压边与每组可同时成立的全部年代段。"""
        _require_site(site_id)
        with write_lock:  # 读也排队在写之后，保证读到的是完整已提交状态
            return _solution_payload(site_id)

    @app.get("/strata/{stratum_id}/correlation-view", tags=["query"])
    def get_stratum_correlation_view(stratum_id: int):
        """按原始层位查询：同组全部成员 + 该组可与其他层位同时成立的全部年代段。"""
        row = db.get_stratum(conn, stratum_id)
        if row is None:
            raise HTTPException(404, f"层位 {stratum_id} 不存在")
        site_id = row["site_id"]
        with write_lock:
            solution = load_and_solve(conn, site_id)
            version = db.get_version(conn)
        gid = solution.group_of[stratum_id]
        group = next(g for g in solution.groups if g.group_id == gid)
        members = [
            serialize_stratum(conn, db.get_stratum(conn, sid))
            for sid in group.member_ids
        ]
        return {
            "stratum_id": stratum_id,
            "group_id": gid,
            "version": version,
            "feasible": solution.feasible,
            "members": members,
            "group_domain": [[lo, hi] for lo, hi in group.domain],
            "feasible_years": [[lo, hi] for lo, hi in group.feasible_years],
            "earliest_year": group.earliest_year,
            "latest_year": group.latest_year,
            "contradiction": serialize_solution(solution, version)["contradiction"],
        }

    @app.exception_handler(ConflictError)
    def _conflict_handler(_request, exc: ConflictError):
        return JSONResponse(status_code=409, content=exc.detail)

    return app


app = create_app()
