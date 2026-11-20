"""号表（标签）版本与批量改号接口测试。"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "labels.db"))
    with TestClient(app) as c:
        yield c


def _site(client, name="仰韶村遗址"):
    r = client.post("/sites", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["record"]


def _stratum(client, site_id, trench, layer_no, intervals=((1, 100),)):
    r = client.post(
        f"/sites/{site_id}/strata",
        json={
            "trench": trench,
            "layer_no": layer_no,
            "intervals": [
                {"year_from": lo, "year_to": hi} for lo, hi in intervals
            ],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _current_version(client, site_id):
    versions = client.get(f"/sites/{site_id}/label-versions").json()
    return next(v["version"] for v in versions if v["is_current"])


def _renumber(client, site_id, version, trench, entries, expect=200):
    r = client.post(
        f"/sites/{site_id}/label-versions/renumber?version={version}",
        json={
            "trench": trench,
            "entries": [
                {"stratum_id": sid, "layer_no": layer_no}
                for sid, layer_no in entries
            ],
        },
    )
    assert r.status_code == expect, r.text
    return r.json()


def test_first_stratum_creates_initial_version(client):
    site = _site(client)
    resp = _stratum(client, site["id"], "T1", "L1")
    assert resp["label_version"] == 1
    versions = client.get(f"/sites/{site['id']}/label-versions").json()
    assert versions == [
        {
            "site_id": site["id"],
            "version": 1,
            "created": versions[0]["created"],
            "is_current": True,
        }
    ]


def test_each_new_stratum_generates_new_version_keeping_ids(client):
    site = _site(client)
    sid = site["id"]
    a = _stratum(client, sid, "T1", "L1")
    b = _stratum(client, sid, "T1", "L2")
    assert (a["label_version"], b["label_version"]) == (1, 2)
    assert _current_version(client, sid) == 2

    table = client.get(f"/sites/{sid}/label-versions/2").json()
    labels = {(e["trench"], e["layer_no"]): e["stratum_id"] for e in table["entries"]}
    assert labels == {("T1", "L1"): a["record"]["id"], ("T1", "L2"): b["record"]["id"]}

    # 层位 id 与年代段不因建版而改变
    got = client.get(f"/strata/{a['record']['id']}").json()
    assert got["layer_no"] == "L1"
    assert got["intervals"] == [{"year_from": 1, "year_to": 100}]


def test_swap_layer_numbers_within_trench(client):
    site = _site(client)
    sid = site["id"]
    s1 = _stratum(client, sid, "T1", "L1")["record"]
    s2 = _stratum(client, sid, "T1", "L2")["record"]
    version = _current_version(client, sid)

    body = _renumber(client, sid, version, "T1", [(s1["id"], "L2"), (s2["id"], "L1")])
    assert body["label_version"] == version + 1
    assert _current_version(client, sid) == version + 1

    assert client.get(f"/strata/{s1['id']}").json()["layer_no"] == "L2"
    assert client.get(f"/strata/{s2['id']}").json()["layer_no"] == "L1"

    table = client.get(f"/sites/{sid}/label-versions/{version + 1}").json()
    labels = {(e["trench"], e["layer_no"]): e["stratum_id"] for e in table["entries"]}
    assert labels == {("T1", "L2"): s1["id"], ("T1", "L1"): s2["id"]}


def test_historical_lookup_returns_original_stratum_and_current_number(client):
    site = _site(client)
    sid = site["id"]
    s1 = _stratum(client, sid, "T1", "L1")["record"]
    s2 = _stratum(client, sid, "T1", "L2")["record"]
    old_version = _current_version(client, sid)

    _renumber(client, sid, old_version, "T1", [(s1["id"], "L2"), (s2["id"], "L1")])

    # 旧版号下 T1/L1 仍是 s1，即便现在 L1 已给 s2 使用
    r = client.get(
        f"/sites/{sid}/labels?version={old_version}&trench=T1&layer_no=L1"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stratum_id"] == s1["id"]
    assert body["layer_no"] == "L1"
    assert body["current_layer_no"] == "L2"  # s1 现在改成 L2
    assert body["current_label_version"] == old_version + 1

    # 现行版号下 T1/L1 已指向 s2
    r = client.get(
        f"/sites/{sid}/labels?version={old_version + 1}&trench=T1&layer_no=L1"
    )
    assert r.json()["stratum_id"] == s2["id"]

    # 不存在的组合 -> 404
    r = client.get(f"/sites/{sid}/labels?version={old_version}&trench=T1&layer_no=L9")
    assert r.status_code == 404


def test_stale_version_rejected_and_state_unchanged(client):
    site = _site(client)
    sid = site["id"]
    s1 = _stratum(client, sid, "T1", "L1")["record"]
    _stratum(client, sid, "T1", "L2")
    stale = _current_version(client, sid) - 1
    before = client.get(f"/sites/{sid}/label-versions/{stale}").json()
    current_before = _current_version(client, sid)

    r = client.post(
        f"/sites/{sid}/label-versions/renumber?version={stale}",
        json={"trench": "T1", "entries": [{"stratum_id": s1["id"], "layer_no": "L9"}]},
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "STALE_LABEL_VERSION"

    assert client.get(f"/strata/{s1['id']}").json()["layer_no"] == "L1"
    assert _current_version(client, sid) == current_before
    assert client.get(f"/sites/{sid}/label-versions/{stale}").json() == before


def test_duplicate_after_batch_rejected_and_state_unchanged(client):
    site = _site(client)
    sid = site["id"]
    s1 = _stratum(client, sid, "T1", "L1")["record"]
    s2 = _stratum(client, sid, "T1", "L2")["record"]
    _stratum(client, sid, "T1", "L3")
    version = _current_version(client, sid)

    # 只把 s1 改成 L2：s2 仍占 L2，改完重号
    r = client.post(
        f"/sites/{sid}/label-versions/renumber?version={version}",
        json={"trench": "T1", "entries": [{"stratum_id": s1["id"], "layer_no": "L2"}]},
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "DUPLICATE_LAYER_NO"

    # 批内互换已占用的号是合法的（s1<->s2 同批）
    _renumber(client, sid, version, "T1", [(s1["id"], "L2"), (s2["id"], "L1")])

    # 状态未因失败提交而改变：现行版仍是 version + 1
    assert _current_version(client, sid) == version + 1


def test_version_may_be_passed_in_body(client):
    site = _site(client)
    sid = site["id"]
    s1 = _stratum(client, sid, "T1", "L1")["record"]
    version = _current_version(client, sid)
    r = client.post(
        f"/sites/{sid}/label-versions/renumber",
        json={
            "label_version": version,
            "trench": "T1",
            "entries": [{"stratum_id": s1["id"], "layer_no": "L9"}],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["label_version"] == version + 1
    # 两处给出版号但不一致 -> 422
    r = client.post(
        f"/sites/{sid}/label-versions/renumber?version={version}",
        json={
            "label_version": version + 1,
            "trench": "T1",
            "entries": [{"stratum_id": s1["id"], "layer_no": "L8"}],
        },
    )
    assert r.status_code == 422


def test_renumber_rejects_foreign_stratum(client):
    site = _site(client)
    sid = site["id"]
    s1 = _stratum(client, sid, "T1", "L1")["record"]
    s2 = _stratum(client, sid, "T2", "L1")["record"]
    version = _current_version(client, sid)
    r = client.post(
        f"/sites/{sid}/label-versions/renumber?version={version}",
        json={
            "trench": "T1",
            "entries": [
                {"stratum_id": s1["id"], "layer_no": "L2"},
                {"stratum_id": s2["id"], "layer_no": "L3"},  # 属于 T2
            ],
        },
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "INVALID_STRATUM"


def test_relations_and_intervals_follow_original_stratum_after_renumber(client):
    site = _site(client)
    sid = site["id"]
    a = _stratum(client, sid, "T1", "L1")["record"]
    b = _stratum(client, sid, "T2", "L1")["record"]
    r = client.post(
        f"/sites/{sid}/correlations",
        json={"stratum_a_id": a["id"], "stratum_b_id": b["id"]},
    )
    assert r.status_code == 201, r.text

    version = _current_version(client, sid)
    _renumber(client, sid, version, "T1", [(a["id"], "L5")])

    # 对接仍指向原层位，分组结论不变
    corr = client.get(f"/sites/{sid}/correlations").json()
    assert len(corr) == 1
    assert {corr[0]["stratum_a_id"], corr[0]["stratum_b_id"]} == {a["id"], b["id"]}
    view = client.get(f"/strata/{a['id']}/correlation-view").json()
    assert {m["id"] for m in view["members"]} == {a["id"], b["id"]}

    # 版号历史完整：旧版仍保留 L1 -> a
    old = client.get(f"/sites/{sid}/label-versions/{version}").json()
    old_map = {(e["trench"], e["layer_no"]): e["stratum_id"] for e in old["entries"]}
    assert old_map[("T1", "L1")] == a["id"]
    assert old_map[("T2", "L1")] == b["id"]


def test_label_versions_survive_restart(tmp_path):
    db_path = str(tmp_path / "restart.db")
    with TestClient(create_app(db_path=db_path)) as c1:
        site = _site(c1)
        sid = site["id"]
        s1 = _stratum(c1, sid, "T1", "L1")["record"]
        s2 = _stratum(c1, sid, "T1", "L2")["record"]
        v = _current_version(c1, sid)
        _renumber(c1, sid, v, "T1", [(s1["id"], "L2"), (s2["id"], "L1")])
        lookup_before = c1.get(
            f"/sites/{sid}/labels?version={v}&trench=T1&layer_no=L1"
        ).json()

    with TestClient(create_app(db_path=db_path)) as c2:
        assert _current_version(c2, sid) == v + 1
        lookup_after = c2.get(
            f"/sites/{sid}/labels?version={v}&trench=T1&layer_no=L1"
        ).json()
        assert lookup_after["stratum_id"] == s1["id"]  # 旧版 L1 -> s1
        assert lookup_after["current_layer_no"] == "L2"
        assert lookup_after == lookup_before
        assert c2.get(f"/sites/{sid}/label-versions/{v + 1}").status_code == 200


def test_legacy_database_gets_initial_version_on_startup(tmp_path):
    """旧库首次启用：已有层位、没有号表，启动后以现有编号建立初版。"""
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('version', '3');
        CREATE TABLE sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE strata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER NOT NULL REFERENCES sites(id),
            trench TEXT NOT NULL,
            layer_no TEXT NOT NULL,
            note TEXT,
            created TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (site_id, trench, layer_no)
        );
        CREATE TABLE stratum_intervals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stratum_id INTEGER NOT NULL REFERENCES strata(id) ON DELETE CASCADE,
            year_from INTEGER NOT NULL,
            year_to INTEGER NOT NULL,
            CHECK (year_from <= year_to)
        );
        CREATE TABLE correlations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER NOT NULL REFERENCES sites(id),
            stratum_a_id INTEGER NOT NULL REFERENCES strata(id),
            stratum_b_id INTEGER NOT NULL REFERENCES strata(id),
            created TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (stratum_a_id < stratum_b_id),
            UNIQUE (site_id, stratum_a_id, stratum_b_id)
        );
        CREATE TABLE superpositions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER NOT NULL REFERENCES sites(id),
            lower_id INTEGER NOT NULL REFERENCES strata(id),
            upper_id INTEGER NOT NULL REFERENCES strata(id),
            created TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (lower_id <> upper_id),
            UNIQUE (site_id, lower_id, upper_id)
        );
        INSERT INTO sites(id, name) VALUES (1, '旧库遗址');
        INSERT INTO strata(id, site_id, trench, layer_no) VALUES
            (10, 1, 'T1', 'L1'), (11, 1, 'T1', 'L2');
        INSERT INTO stratum_intervals(stratum_id, year_from, year_to) VALUES
            (10, 1, 100), (11, 1, 100);
        """
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path)) as c:
        versions = c.get("/sites/1/label-versions").json()
        assert [(v["version"], v["is_current"]) for v in versions] == [(1, True)]
        table = c.get("/sites/1/label-versions/1").json()
        assert {(e["trench"], e["layer_no"]): e["stratum_id"] for e in table["entries"]} == {
            ("T1", "L1"): 10,
            ("T1", "L2"): 11,
        }
        # 旧层位新增时生成下一版
        body = _stratum(c, 1, "T1", "L3")
        assert body["label_version"] == 2
