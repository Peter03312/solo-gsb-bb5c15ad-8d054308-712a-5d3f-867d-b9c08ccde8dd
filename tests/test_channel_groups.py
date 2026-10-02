"""狭窄水道组测试。

规则：
* 至少两条现有航段可登记为同一组，返回组号与成员；组建成后不可修改，
  航段不得重复归组；未知航段 / 重复编号 / 冲突归组均拒绝且不留半成品；
* 船进入组内任一航段后，从进入分钟到抵达前一分钟独占整组：不同成员
  航段的通行不得重叠，抵达分钟可由下一船进入；
* 组建前的已确认快照按其原航段与固化航时计入新组占用；既有快照不改写、
  已有重叠也不清理；
* 单船与批次预排避开组占用；确认在原事务内按当前组关系与已确认及批内
  拟占用复算，不一致沿用 PLAN_MISMATCH 且整批回滚；
* 改期建议避开其他快照的组占用并排除自身；原行程安全复核仍只判断潮位。
"""
import json

from app.database import connect

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _plan(client, body):
    r = client.post("/api/trips/plan", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _confirm(client, plan):
    r = client.post("/api/trips/confirm", json=plan)
    assert r.status_code == 201, r.text
    return r.json()


def _create_group(client, seg_ids, note=None):
    body = {"segment_ids": seg_ids}
    if note is not None:
        body["note"] = note
    return client.post("/api/channel-groups", json=body)


def _raw_insert_trip(rev, itinerary, origin="A", destination="B",
                     start=0, window=20, draft=1.0, height=0.0, margin=0.0):
    """直接写入一个已确认快照（绕过确认事务），用于构造既有占用。"""
    conn = connect()
    arrival = start if not itinerary else itinerary[-1]["end_minute"]
    cur = conn.execute(
        "INSERT INTO confirmed_trips"
        " (revision, origin, destination, start_minute, window_minutes,"
        "  draft, height, margin, arrival_minute, itinerary)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rev, origin, destination, start, window, draft, height, margin,
         arrival, json.dumps(itinerary)),
    )
    trip_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trip_id


def _two_parallel_segments(client):
    """A->B 两条平行航段 s1、s2（同参数），返回 (s1, s2)。"""
    add_node(client, "A"); add_node(client, "B")
    s1 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    s2 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    return s1, s2


PLAN_BODY = {
    "origin": "A", "destination": "B",
    "start_minute": 0, "window_minutes": 20,
    "draft": 1.0, "height": 0.0, "margin": 0.0,
}


# ---------------------------------------------------------------------------
# 1. 登记：成功返回组号与成员；GET 列表 / 详情
# ---------------------------------------------------------------------------

def test_create_group_returns_id_and_members(client):
    s1, s2 = _two_parallel_segments(client)
    r = _create_group(client, [s2, s1], note="狭窄水道-1")
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["group_id"] == 1
    # 成员按航段编号升序返回
    assert data["segment_ids"] == [s1, s2]

    lst = client.get("/api/channel-groups").json()
    assert lst == [{"group_id": 1, "segment_ids": [s1, s2]}]
    one = client.get("/api/channel-groups/1").json()
    assert one == {"group_id": 1, "segment_ids": [s1, s2]}

    r = client.get("/api/channel-groups/999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "CHANNEL_GROUP_NOT_FOUND"


def test_create_group_requires_at_least_two_segments(client):
    add_node(client, "A"); add_node(client, "B")
    s1 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    for payload in [[], [s1]]:
        r = _create_group(client, payload)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get("/api/channel-groups").json() == []


# ---------------------------------------------------------------------------
# 2. 拒绝口径：未知航段 / 重复编号 / 冲突归组，均不留半成品
# ---------------------------------------------------------------------------

def test_unknown_segment_rejected(client):
    s1, s2 = _two_parallel_segments(client)
    r = _create_group(client, [s1, 999])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SEGMENT_NOT_FOUND"
    assert r.json()["error"]["details"]["unknown_segment_ids"] == [999]
    # 不留半成品：无组产生，s1 仍未归组、可另行登记
    assert client.get("/api/channel-groups").json() == []
    assert _create_group(client, [s1, s2]).status_code == 201


def test_duplicate_segment_ids_rejected(client):
    s1, s2 = _two_parallel_segments(client)
    r = _create_group(client, [s1, s2, s1])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DUPLICATE_SEGMENT_IDS"
    assert r.json()["error"]["details"]["duplicate_segment_ids"] == [s1]
    assert client.get("/api/channel-groups").json() == []


def test_conflicting_membership_rejected_and_group_immutable(client):
    add_node(client, "A"); add_node(client, "B")
    s1 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    s2 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    s3 = add_segment(client, "A", "B", travel_time=2, depth=99.0)

    r = _create_group(client, [s1, s2])
    assert r.status_code == 201

    # s2 已属组 1：冲突归组拒绝，且 s3 不被牵连（不留半成品）
    r = _create_group(client, [s2, s3])
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "SEGMENT_ALREADY_GROUPED"
    assert err["details"]["conflicts"] == [{"seg_id": s2, "group_id": 1}]

    # 原组保持不变（不可修改）；s3 仍未归组
    assert client.get("/api/channel-groups").json() == [
        {"group_id": 1, "segment_ids": [s1, s2]}
    ]
    # 不相交的航段集合可另建一组
    s4 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    r = _create_group(client, [s3, s4])
    assert r.status_code == 201
    assert r.json()["group_id"] == 2


# ---------------------------------------------------------------------------
# 3. 组占用：进入组内任一航段即独占整组；抵达分钟可由下一船进入
# ---------------------------------------------------------------------------

def test_group_occupancy_blocks_all_member_segments(client):
    s1, s2 = _two_parallel_segments(client)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 既有快照：s1 于 0 进入，占 0,1（2 抵达）
    _raw_insert_trip(rev, [
        {"seg_id": s1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ])
    # 未归组时：s2 互不相干，0 进入 s2、第 2 分钟抵达
    plan = _plan(client, {**PLAN_BODY, "revision": rev})
    assert [(l["seg_id"], l["start_minute"]) for l in plan["itinerary"]] == [(s2, 0)]

    # 登记为同一组后：s1 的占用对整组封闭，s2 在 0/1 进入均冲突，
    # 最早 2 进入（前船抵达分钟可由下一船进入），第 4 分钟抵达
    assert _create_group(client, [s1, s2]).status_code == 201
    plan = _plan(client, {**PLAN_BODY, "revision": rev})
    assert [(l["seg_id"], l["start_minute"]) for l in plan["itinerary"]] == [(s1, 2)]
    assert plan["arrival_minute"] == 4


def test_preexisting_snapshot_counts_toward_new_group(client):
    """组建前的已确认快照按原航段与固化航时计入新组占用。"""
    s1, s2 = _two_parallel_segments(client)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 组建**前**确认的快照：s1 占 3,4（3 进入、5 抵达）
    _raw_insert_trip(rev, [
        {"seg_id": s1, "origin": "A", "destination": "B",
         "start_minute": 3, "end_minute": 5, "travel_time": 2},
    ])
    assert _create_group(client, [s1, s2]).status_code == 201

    plan = _plan(client, {**PLAN_BODY, "revision": rev})
    # 0 进入（占 0,1）与 3,4 的组占用不冲突，仍最早；把起点推到 2 后：
    # 2 进入占 2,3（3 冲突）、3/4 进入均冲突，最早 5 进入
    assert [l["start_minute"] for l in plan["itinerary"]] == [0]
    plan = _plan(client, {**PLAN_BODY, "start_minute": 2, "window_minutes": 18,
                          "revision": rev})
    assert [l["start_minute"] for l in plan["itinerary"]] == [5]
    assert plan["arrival_minute"] == 7


def test_group_of_three_members_blocks_all(client):
    add_node(client, "A"); add_node(client, "B")
    s1 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    s2 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    s3 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    _create_group(client, [s1, s2, s3])

    # s2 被占 0,1 -> s1/s3 同样封闭
    _raw_insert_trip(rev, [
        {"seg_id": s2, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ])
    plan = _plan(client, {**PLAN_BODY, "revision": rev})
    assert [(l["seg_id"], l["start_minute"]) for l in plan["itinerary"]] == [(s1, 2)]


def test_overlapping_legacy_snapshots_in_group_kept_and_blocking(client):
    """组内既有快照彼此重叠也原样保留，重叠分钟对后续规划仍封闭。"""
    s1, s2 = _two_parallel_segments(client)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    _create_group(client, [s1, s2])
    # 两条彼此重叠的历史快照（分属不同成员航段）
    _raw_insert_trip(rev, [
        {"seg_id": s1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ])
    _raw_insert_trip(rev, [
        {"seg_id": s2, "origin": "A", "destination": "B",
         "start_minute": 1, "end_minute": 3, "travel_time": 2},
    ])
    plan = _plan(client, {**PLAN_BODY, "revision": rev})
    # 组占用 0..2（两条快照并集），最早 3 进入
    assert [l["start_minute"] for l in plan["itinerary"]] == [3]
    # 既有快照不改写
    trips = client.get("/api/trips").json()
    assert [[l["start_minute"] for l in t["itinerary"]] for t in trips] == [[0], [1]]


# ---------------------------------------------------------------------------
# 4. 单船确认：原事务内按当前组关系复算，不一致 PLAN_MISMATCH 且不保存
# ---------------------------------------------------------------------------

def test_confirm_recalculates_with_current_groups(client):
    s1, s2 = _two_parallel_segments(client)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 既有快照占 s1 的 0,1；未归组时规划走 s2 于 0 进入
    _raw_insert_trip(rev, [
        {"seg_id": s1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ])
    plan = _plan(client, {**PLAN_BODY, "revision": rev})
    assert [(l["seg_id"], l["start_minute"]) for l in plan["itinerary"]] == [(s2, 0)]

    # 规划后登记组：s2 的 0 进入与组占用冲突 -> 确认复算不一致
    assert _create_group(client, [s1, s2]).status_code == 201
    r = client.post("/api/trips/confirm", json=plan)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"
    assert len(client.get("/api/trips").json()) == 1  # 不保存

    # 重新规划（避开组占用）后可确认
    replan = _plan(client, {**PLAN_BODY, "revision": rev})
    assert [l["start_minute"] for l in replan["itinerary"]] == [2]
    trip = _confirm(client, replan)
    assert trip["arrival_minute"] == 4


# ---------------------------------------------------------------------------
# 5. 批次：预排避开组占用与前船拟占用；确认按当前组关系复算、全批回滚
# ---------------------------------------------------------------------------

def _batch_body(rev, window=20):
    ship = {"origin": "A", "destination": "B", "start_minute": 0,
            "window_minutes": window, "draft": 1.0, "height": 0.0, "margin": 0.0}
    return {"revision": rev, "ships": [dict(ship), dict(ship)]}


def test_batch_plan_avoids_group_occupancy_and_intra_batch(client):
    s1, s2 = _two_parallel_segments(client)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    _create_group(client, [s1, s2])

    r = client.post("/api/trips/batch/plan", json=_batch_body(rev))
    assert r.status_code == 200, r.text
    ships = r.json()["ships"]
    # 船 0：s1 于 0 进入占 0,1；其拟占用对整组封闭 -> 船 1 最早 2 进入
    assert [(l["seg_id"], l["start_minute"]) for l in ships[0]["itinerary"]] == [(s1, 0)]
    assert [(l["seg_id"], l["start_minute"]) for l in ships[1]["itinerary"]] == [(s1, 2)]
    assert ships[1]["arrival_minute"] == 4

    # 批次确认成功：两快照落库，组占用对后续单船规划立即生效
    confirm_body = {
        "revision": rev,
        "idempotency_key": "grp-batch-1",
        "ships": [
            {**_batch_body(rev)["ships"][i], "itinerary": ships[i]["itinerary"]}
            for i in range(2)
        ],
    }
    r = client.post("/api/trips/batch/confirm", json=confirm_body)
    assert r.status_code == 201, r.text
    assert [t["arrival_minute"] for t in r.json()["trips"]] == [2, 4]

    plan = _plan(client, {**PLAN_BODY, "revision": rev})
    assert [l["start_minute"] for l in plan["itinerary"]] == [4]


def test_batch_confirm_uses_current_groups_and_rolls_back(client):
    s1, s2 = _two_parallel_segments(client)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 预排时未归组：船 0 走 s1@0，船 1 走 s2@0（互不相干）
    body = _batch_body(rev)
    bp = client.post("/api/trips/batch/plan", json=body).json()
    assert [s["itinerary"][0]["seg_id"] for s in bp["ships"]] == [s1, s2]
    confirm_body = {
        "revision": rev,
        "idempotency_key": "grp-batch-2",
        "ships": [
            {**body["ships"][i], "itinerary": bp["ships"][i]["itinerary"]}
            for i in range(2)
        ],
    }

    # 确认前登记组：两船同分钟进入同组不同成员航段 -> 复算不一致，全批回滚
    assert _create_group(client, [s1, s2]).status_code == 201
    r = client.post("/api/trips/batch/confirm", json=confirm_body)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PLAN_MISMATCH"
    assert err["details"]["ship_index"] == 1
    assert err["details"]["reason"] == "itinerary_mismatch"
    assert client.get("/api/trips").json() == []  # 全批不保存

    # 重新预排（按组占用错开）后同键确认成功（幂等记录未登记，键可复用）
    bp2 = client.post("/api/trips/batch/plan", json=body).json()
    confirm_body["ships"] = [
        {**body["ships"][i], "itinerary": bp2["ships"][i]["itinerary"]}
        for i in range(2)
    ]
    r = client.post("/api/trips/batch/confirm", json=confirm_body)
    assert r.status_code == 201, r.text
    assert [t["arrival_minute"] for t in r.json()["trips"]] == [2, 4]


# ---------------------------------------------------------------------------
# 6. 复核 / 改期：复核只判潮位；改期避开其他快照的组占用并排除自身
# ---------------------------------------------------------------------------

def test_review_safety_still_tide_only_within_group(client):
    """组内快照彼此重叠不构成潮位风险：原行程复核仍只判断潮位。"""
    s1, s2 = _two_parallel_segments(client)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    _create_group(client, [s1, s2])
    _raw_insert_trip(rev, [
        {"seg_id": s1, "origin": "A", "destination": "B",
         "start_minute": 2, "end_minute": 4, "travel_time": 2},
    ])
    mine = _raw_insert_trip(rev, [
        {"seg_id": s2, "origin": "A", "destination": "B",
         "start_minute": 2, "end_minute": 4, "travel_time": 2},
    ])
    data = client.post(f"/api/trips/{mine}/review",
                       json={"target_revision": rev}).json()
    assert data["safe"] is True
    assert data["reschedule"] is None


def test_reschedule_avoids_other_snapshots_group_occupancy(client):
    add_node(client, "A"); add_node(client, "B")
    s1 = add_segment(client, "A", "B", travel_time=3, depth=4.0)
    s2 = add_segment(client, "A", "B", travel_time=3, depth=4.0)
    # 潮位：0..4 低、5..19 足够（吃水 6 + 余量 0.5，静水 4 -> 需潮位 >= 2.5）
    rev = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    _create_group(client, [s1, s2])
    # 本快照：s1 于 5 进入占 5,6,7
    mine = _raw_insert_trip(rev, [
        {"seg_id": s1, "origin": "A", "destination": "B",
         "start_minute": 5, "end_minute": 8, "travel_time": 3},
    ], draft=6.0, margin=0.5)

    # 更正：5..10 低潮（原行程违规），11 起恢复
    client.post("/api/tide/corrections",
                json={"corrections": {m: 0.0 for m in range(5, 11)}})
    # 其他快照在**另一成员航段** s2 上占 11,12,13（组占用对 s1 同样封闭）
    other = _raw_insert_trip(rev, [
        {"seg_id": s2, "origin": "A", "destination": "B",
         "start_minute": 11, "end_minute": 14, "travel_time": 3},
    ], draft=6.0, margin=0.5)

    data = client.post(f"/api/trips/{mine}/review", json={}).json()
    assert data["safe"] is False
    rs = data["reschedule"]
    # 若无组占用：11 出发；避开他船在 s2 上的组占用后最早 14 出发
    assert rs["itinerary"][0]["seg_id"] == s1
    assert rs["itinerary"][0]["start_minute"] == 14
    assert rs["arrival_minute"] == 17
    assert rs["changed_leg_count"] == 1

    # 建议不占位、快照不改写
    trips = {t["trip_id"]: t for t in client.get("/api/trips").json()}
    assert [l["start_minute"] for l in trips[mine]["itinerary"]] == [5]
    assert [l["start_minute"] for l in trips[other]["itinerary"]] == [11]


def test_reschedule_excludes_own_group_occupancy(client):
    """排除自身：自身在组内的历史占用不挡自己的改期建议。"""
    add_node(client, "A"); add_node(client, "B")
    s1 = add_segment(client, "A", "B", travel_time=3, depth=4.0)
    s2 = add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    _create_group(client, [s1, s2])
    # 本快照：s2 于 5 进入占 5,6,7（s1 上无其他快照）
    mine = _raw_insert_trip(rev, [
        {"seg_id": s2, "origin": "A", "destination": "B",
         "start_minute": 5, "end_minute": 8, "travel_time": 3},
    ], draft=6.0, margin=0.5)

    # 只把第 5 分钟改成低潮：原 5 出发违规；6 起潮位恢复
    client.post("/api/tide/corrections", json={"corrections": {5: 0.0}})
    data = client.post(f"/api/trips/{mine}/review", json={}).json()
    assert data["safe"] is False
    rs = data["reschedule"]
    # 6 出发占 6,7,8——其中 6,7 仅是自身在组内的历史占用；排除自身后可行
    assert rs["itinerary"][0]["start_minute"] == 6
    assert rs["changed_leg_count"] == 1


# ---------------------------------------------------------------------------
# 7. 组占用与分时管制并存：快照固化航时计入组占用，新航程按当前管制
# ---------------------------------------------------------------------------

def test_group_occupancy_uses_snapshot_frozen_travel_time(client):
    add_node(client, "A"); add_node(client, "B")
    s1 = add_segment(client, "A", "B", travel_time=5, depth=99.0)
    s2 = add_segment(client, "A", "B", travel_time=5, depth=99.0)
    rev = seed_initial_revision(_level(0, 39, 3.0))
    # 管制：s1 在 [0,40) 内进入航时 5（与 s2 基础航时并列，同刻 s1 编号小优先）
    r = client.put(f"/api/segments/{s1}/travel-controls",
                   json={"controls": [{"start_minute": 0, "end_minute": 40,
                                       "travel_time": 5}]})
    assert r.status_code == 200
    plan = _plan(client, {**PLAN_BODY, "window_minutes": 40, "revision": rev})
    assert plan["itinerary"][0]["seg_id"] == s1
    assert plan["itinerary"][0]["travel_time"] == 5
    _confirm(client, plan)  # s1 占 0..4（固化航时 5）

    # 管制改为航时 2，再登记组：快照按固化航时占 0..4 计入组占用，
    # 新船在任一成员航段最早 5 进入、按当前管制航时 2
    client.put(f"/api/segments/{s1}/travel-controls",
               json={"controls": [{"start_minute": 0, "end_minute": 40,
                                   "travel_time": 2}]})
    assert _create_group(client, [s1, s2]).status_code == 201
    plan = _plan(client, {**PLAN_BODY, "window_minutes": 40, "revision": rev})
    assert plan["itinerary"][0]["start_minute"] == 5
    assert plan["itinerary"][0]["travel_time"] == 2
    assert plan["arrival_minute"] == 7
