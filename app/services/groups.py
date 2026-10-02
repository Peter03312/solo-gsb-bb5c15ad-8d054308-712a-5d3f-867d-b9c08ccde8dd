"""狭窄水道组。

调度员可将**至少两条现有航段**登记为同一狭窄水道组：

* 提交航段编号集合，成功返回组号与成员清单（按编号升序）；组建成后**不可
  修改**（不提供更新/删除端点），每条航段至多属于一个组；
* 拒绝未知航段（``SEGMENT_NOT_FOUND``）、提交集合内的重复编号
  （``DUPLICATE_SEGMENT_IDS``）与冲突归组——航段已属于其他组
  （``SEGMENT_ALREADY_GROUPED``）；任一失败都在同一事务内回滚，**不留半成品**；
* **组占用**：船进入组内任一航段后，从进入分钟 ``d`` 到抵达前一分钟
  ``d+t-1`` 独占**整组**——不同成员航段的通行不得重叠，抵达分钟 ``d+t``
  可由下一船进入（与单航段独占口径一致）；
* 组建前已确认的快照按其**原航段与固化航时**计入新组占用；既有快照不改写、
  已有重叠也不清理。

实现上沿用"按航段的占用集合"：把组内任一成员航段的占用分钟**并集扩散**到
全组各成员（``expand_blocked_for_groups``），规划/确认/批次/复核原有的按
航段判定逻辑无需改变；批内拟占用在并入时同样按组扩散
（``add_leg_occupancy``）。
"""
from collections import Counter
from typing import Iterable, Optional

from ..errors import ApiError


# ---- 组关系读取 ------------------------------------------------------------------

def load_group_map(conn) -> dict[int, int]:
    """返回 {seg_id: group_id}（仅含已归组航段）。"""
    rows = conn.execute(
        "SELECT seg_id, group_id FROM channel_group_members"
    ).fetchall()
    return {r["seg_id"]: r["group_id"] for r in rows}


def _members_by_group(seg_to_group: dict[int, int]) -> dict[int, list[int]]:
    members: dict[int, list[int]] = {}
    for seg_id, gid in seg_to_group.items():
        members.setdefault(gid, []).append(seg_id)
    return members


# ---- 组占用传播 ------------------------------------------------------------------

def expand_blocked_for_groups(blocked: dict[int, set[int]],
                              seg_to_group: dict[int, int]) -> None:
    """把组内任一成员航段的占用分钟并集扩散到全组各成员（就地修改）。

    blocked 为 {seg_id: {绝对分钟}}。扩散后，规划对任一成员航段的按航段
    判定即等价于"整组独占"：进入组内任一航段的航程不得与组内任何既有
    占用分钟重叠。
    """
    for gid, segs in _members_by_group(seg_to_group).items():
        union: set[int] = set()
        for s in segs:
            union.update(blocked.get(s, ()))
        if union:
            for s in segs:
                blocked.setdefault(s, set()).update(union)


def add_leg_occupancy(blocked: dict[int, set[int]], seg_id: int,
                      minutes: Iterable[int],
                      seg_to_group: dict[int, int]) -> None:
    """把一段航程（进入分钟 d 起 d..d+t-1）并入 blocked；航段已归组时
    对全组各成员一并封闭（批内前船拟占用与已确认占用同口径）。"""
    gid = seg_to_group.get(seg_id)
    if gid is None:
        blocked.setdefault(seg_id, set()).update(minutes)
        return
    for s, g in seg_to_group.items():
        if g == gid:
            blocked.setdefault(s, set()).update(minutes)


# ---- 登记 / 查询 ------------------------------------------------------------------

def create_channel_group(conn, seg_ids: list[int],
                         note: Optional[str] = None) -> dict:
    """登记狭窄水道组，返回 {"group_id": ..., "segment_ids": [升序成员]}。

    单事务（BEGIN IMMEDIATE）内完成校验与写入，任一失败整体回滚：
      * 提交集合含重复编号 -> 422 DUPLICATE_SEGMENT_IDS；
      * 未知航段 -> 404 SEGMENT_NOT_FOUND；
      * 航段已属于其他组 -> 409 SEGMENT_ALREADY_GROUPED。
    成员数量下限（>=2）由请求模型校验（VALIDATION_ERROR）。
    """
    dups = sorted(s for s, c in Counter(seg_ids).items() if c > 1)
    if dups:
        raise ApiError(
            422, "DUPLICATE_SEGMENT_IDS",
            f"提交的航段编号集合含重复编号: {dups}",
            {"duplicate_segment_ids": dups},
        )
    unique = sorted(set(seg_ids))
    placeholders = ",".join("?" for _ in unique)

    try:
        conn.execute("BEGIN IMMEDIATE")

        known = {
            r["seg_id"] for r in conn.execute(
                f"SELECT seg_id FROM segments WHERE seg_id IN ({placeholders})",
                unique,
            ).fetchall()
        }
        unknown = [s for s in unique if s not in known]
        if unknown:
            raise ApiError(
                404, "SEGMENT_NOT_FOUND",
                f"未知航段: {unknown}",
                {"unknown_segment_ids": unknown},
            )

        conflicts = [
            {"seg_id": r["seg_id"], "group_id": r["group_id"]}
            for r in conn.execute(
                f"SELECT seg_id, group_id FROM channel_group_members"
                f" WHERE seg_id IN ({placeholders}) ORDER BY seg_id",
                unique,
            ).fetchall()
        ]
        if conflicts:
            raise ApiError(
                409, "SEGMENT_ALREADY_GROUPED",
                "航段已属于其他狭窄水道组，不得重复归组: "
                f"{[c['seg_id'] for c in conflicts]}",
                {"conflicts": conflicts},
            )

        cur = conn.execute(
            "INSERT INTO channel_groups(note) VALUES (?)", (note,)
        )
        gid = cur.lastrowid
        conn.executemany(
            "INSERT INTO channel_group_members(group_id, seg_id) VALUES (?, ?)",
            [(gid, s) for s in unique],
        )
        conn.execute("COMMIT")
    except ApiError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"group_id": gid, "segment_ids": unique}


def list_channel_groups(conn) -> list[dict]:
    """全部狭窄水道组（按组号升序，成员按航段编号升序）。"""
    group_rows = conn.execute(
        "SELECT group_id FROM channel_groups ORDER BY group_id"
    ).fetchall()
    member_rows = conn.execute(
        "SELECT group_id, seg_id FROM channel_group_members"
        " ORDER BY group_id, seg_id"
    ).fetchall()
    members: dict[int, list[int]] = {}
    for r in member_rows:
        members.setdefault(r["group_id"], []).append(r["seg_id"])
    return [
        {"group_id": r["group_id"], "segment_ids": members.get(r["group_id"], [])}
        for r in group_rows
    ]


def get_channel_group(conn, group_id: int) -> dict:
    """按组号查询；未知组号 -> 404 CHANNEL_GROUP_NOT_FOUND。"""
    row = conn.execute(
        "SELECT group_id FROM channel_groups WHERE group_id = ?", (group_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "CHANNEL_GROUP_NOT_FOUND",
                       f"狭窄水道组不存在: {group_id}", {"group_id": group_id})
    members = [
        r["seg_id"] for r in conn.execute(
            "SELECT seg_id FROM channel_group_members"
            " WHERE group_id = ? ORDER BY seg_id",
            (group_id,),
        ).fetchall()
    ]
    return {"group_id": group_id, "segment_ids": members}
