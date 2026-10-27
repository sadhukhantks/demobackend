"""Tests for the Loom Modification register — src/juteProduction/loom_modification.py
(prefix /api/loomModification). Phase 1 of
docs/superpowers/specs/2026-09-26-loom-modification-impact-design.md.

Portal persona: DB + auth are mocked (no real DB). The router imports get_tenant_db /
get_current_user_with_refresh into its own namespace; FastAPI resolves them via
Depends, so they are overridden through app.dependency_overrides keyed by those
exact symbols (test_weaving_masters.py pattern). ``execute.side_effect`` is fed in
the EXECUTE ORDER documented in the router's module docstring:

  _save_modification:
    edit only   E1 existing header, E2 children count, E3 old snapshot ids,
                E4 child REVERSAL snapshot (cnt > 0 only)
    kind != INITIAL   C1 parent header, C2 latest stage (FOLLOWUP / RECTIFY — skipped on
                      an edit of a stage with children) or parent snapshot ids (REVERSAL),
                      C3 chain reverted looms (FOLLOWUP / RECTIFY)
    P1 party (VENDOR only), G1 group row (loom_group_id only)
    L1 loom context, O1 loom spans (overlap)
    W1 insert (lastrowid) | update, W2 stamp root (create + INITIAL), W3 soft-delete
    snapshot (edit), W4 snapshot insert x N, D1 data check

Covered: setup shape + open_events rule; group create / edit / delete (dup code,
non-loom, mixed branch, branch of another company, inactive newly ticked, retired
existing member kept, guarded delete); modification create (INITIAL happy path,
FOLLOWUP wrong parent, same-day stage, per-loom overlap, done_date outside span,
future date, VENDOR without / unknown party, REVERSAL subset rule, parent is a
REVERSAL, save_as_group, group of another branch, reverted loom in a later stage);
edit (happy, immutability with children, child start guard, middle stage free edit,
terminal stage linearity, unticking a loom a child REVERSAL carries, 404); delete
(refused with children, delete chain, chain on non-root, plain, 404); insert earlier
stage (incl. branch pinned to the chain); add members (incl. reverted loom); group
edit branch rule; the reads.
Commit is never called on a 400 path.
"""

from datetime import date
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.main import app
from src.juteProduction import loom_modification as lm
from src.juteProduction.loom_modification import (
    get_current_user_with_refresh,
    get_tenant_db,
)

client = TestClient(app)
BASE = "/api/loomModification"

_UNSET = object()


def _row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _res(*, fetchall=_UNSET, fetchone=_UNSET, lastrowid=_UNSET):
    res = MagicMock()
    if fetchall is not _UNSET:
        res.fetchall.return_value = fetchall
    if fetchone is not _UNSET:
        res.fetchone.return_value = fetchone
    if lastrowid is not _UNSET:
        res.lastrowid = lastrowid
    return res


def _loom(machine_id, name, branch_id=12, active=1, is_loom=1, branch_co_id=9, line_no="11"):
    return _row(
        {
            "machine_id": machine_id,
            "machine_name": name,
            "mech_code": name,
            "active": active,
            "line_no": line_no,
            "machine_type_name": "LOOM" if is_loom else "Spinning",
            "is_loom": is_loom,
            "dept_id": 15009,
            "branch_id": branch_id,
            "branch_co_id": branch_co_id,
        }
    )


def _ids(*ids):
    return [_row({"machine_id": i}) for i in ids]


def _event(**over):
    mapping = {
        "modification_id": 100,
        "co_id": 9,
        "branch_id": 12,
        "branch_name": "Empire",
        "loom_group_id": None,
        "group_code": None,
        "group_name": None,
        "mod_date": date(2026, 7, 1),
        "mod_to_date": None,
        "settle_days": 3,
        "mod_kind": "INITIAL",
        "parent_modification_id": None,
        "root_modification_id": 100,
        "mod_category": "MECHANICAL",
        "title": "Beat-up cam replaced",
        "expected_effect": "+5% eff",
        "done_by": "INHOUSE",
        "party_id": None,
        "party_name": None,
        "total_cost": 1500.0,
        "remarks": None,
        "active": 1,
        "loom_count": 2,
        "children_count": 0,
        "stage_count": 1,
        "stage_no": 1,
        "description": "cam",
    }
    mapping.update(over)
    return _row(mapping)


def _dc(before=40, after=35):
    return _res(fetchone=_row({"before_days": before, "after_days": after}))


class _Base:
    def setup_method(self):
        self.db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.db

    def teardown_method(self):
        app.dependency_overrides.clear()

    def _params_of(self, call_index: int) -> dict:
        return self.db.execute.call_args_list[call_index].args[1]


# =============================================================================
# /setup
# =============================================================================


class TestSetup(_Base):
    def test_setup_shape_and_open_events_rule(self):
        looms = [_loom(30, "S293", line_no="11"), _loom(31, "S294", line_no="12")]
        stages = [
            # chain 100: INITIAL then FOLLOWUP fully reverted -> not open
            _row({"modification_id": 100, "root_modification_id": 100, "parent_modification_id": None,
                  "branch_id": 12, "loom_group_id": None, "group_code": None,
                  "mod_date": date(2026, 7, 1), "mod_to_date": None, "mod_kind": "INITIAL",
                  "title": "Cam", "snapshot_count": 2, "unreverted_count": 2}),
            _row({"modification_id": 101, "root_modification_id": 100, "parent_modification_id": 100,
                  "branch_id": 12, "loom_group_id": None, "group_code": None,
                  "mod_date": date(2026, 7, 10), "mod_to_date": None, "mod_kind": "FOLLOWUP",
                  "title": "Tweak", "snapshot_count": 2, "unreverted_count": 0}),
            # chain 200: single INITIAL with un-reverted looms -> open
            _row({"modification_id": 200, "root_modification_id": 200, "parent_modification_id": None,
                  "branch_id": 12, "loom_group_id": 3, "group_code": "G1",
                  "mod_date": date(2026, 7, 5), "mod_to_date": None, "mod_kind": "INITIAL",
                  "title": "Motor", "snapshot_count": 3, "unreverted_count": 3}),
        ]
        self.db.execute.side_effect = [
            _res(fetchall=looms),
            _res(fetchall=[]),
            _res(fetchall=[]),
            _res(fetchall=stages),
        ]
        r = client.get(f"{BASE}/setup?co_id=9&branch_id=12")
        assert r.status_code == 200
        d = r.json()["data"]
        assert [l["machine_id"] for l in d["looms"]] == [30, 31]
        assert d["lines"] == ["11", "12"]
        assert d["groups"] == [] and d["parties"] == []
        assert {k["code"] for k in d["mod_kinds"]} == {"INITIAL", "FOLLOWUP", "RECTIFY", "REVERSAL"}
        assert {"code": "SPEED_CHANGE", "label": "Speed change"} in d["mod_categories"]
        assert d["done_by_options"] == [
            {"code": "INHOUSE", "label": "In-house"}, {"code": "VENDOR", "label": "Vendor"}
        ]
        assert d["default_settle_days"] == 3
        assert [s["modification_id"] for s in d["open_events"]] == [200]
        assert len(d["all_stages"]) == 3
        assert d["all_stages"][0]["mod_date"] == "2026-07-01"
        assert d["open_events"][0]["label"].startswith("Motor — 2026-07-05")
        # loom query binds the type NAME, never an id
        p = self._params_of(0)
        assert p["loom_type"] == "Loom" and p["co_id"] == 9 and p["branch_id"] == 12

    def test_setup_missing_co_id_returns_400(self):
        r = client.get(f"{BASE}/setup")
        assert r.status_code == 400
        assert "co_id" in r.json()["detail"]


# =============================================================================
# Loom groups
# =============================================================================


class TestLoomGroupCreate(_Base):
    def _payload(self, **over):
        p = {"co_id": 9, "group_code": "EJM-L4", "group_name": "Line 4 study",
             "group_purpose": "STUDY", "machine_ids": [30, 31]}
        p.update(over)
        return p

    def test_create_happy_path_derives_branch_and_inserts_members(self):
        self.db.execute.side_effect = [
            _res(fetchone=None),
            _res(fetchall=[_loom(30, "S293"), _loom(31, "S294")]),
            _res(lastrowid=5),
            _res(),
            _res(),
        ]
        r = client.post(f"{BASE}/loom_group_create", json=self._payload())
        assert r.status_code == 200, r.text
        assert r.json()["data"]["loom_group_id"] == 5
        assert self._params_of(2)["branch_id"] == 12
        assert self._params_of(2)["group_purpose"] == "STUDY"
        assert self._params_of(3) == {"loom_group_id": 5, "machine_id": 30}
        assert self._params_of(4) == {"loom_group_id": 5, "machine_id": 31}
        self.db.commit.assert_called_once()

    def test_create_duplicate_code_returns_400(self):
        self.db.execute.side_effect = [_res(fetchone=_row({"loom_group_id": 2}))]
        r = client.post(f"{BASE}/loom_group_create", json=self._payload())
        assert r.status_code == 400
        assert r.json()["detail"] == lm.GROUP_DUP_MSG
        self.db.commit.assert_not_called()

    def test_create_non_loom_member_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=None),
            _res(fetchall=[_loom(30, "S293"), _loom(31, "SPIN-1", is_loom=0)]),
        ]
        r = client.post(f"{BASE}/loom_group_create", json=self._payload())
        assert r.status_code == 400
        assert r.json()["detail"] == "machine 31 is not a loom"
        self.db.commit.assert_not_called()

    def test_create_unknown_machine_returns_400(self):
        self.db.execute.side_effect = [_res(fetchone=None), _res(fetchall=[_loom(30, "S293")])]
        r = client.post(f"{BASE}/loom_group_create", json=self._payload())
        assert r.status_code == 400
        assert "31 is not a loom" in r.json()["detail"]

    def test_create_mixed_branch_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=None),
            _res(fetchall=[_loom(30, "S293", branch_id=12), _loom(31, "S294", branch_id=13)]),
        ]
        r = client.post(f"{BASE}/loom_group_create", json=self._payload())
        assert r.status_code == 400
        assert r.json()["detail"] == lm.MIXED_BRANCH_MSG

    def test_create_branch_of_another_company_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=None),
            _res(fetchall=[_loom(30, "S293", branch_co_id=2), _loom(31, "S294", branch_co_id=2)]),
        ]
        r = client.post(f"{BASE}/loom_group_create", json=self._payload())
        assert r.status_code == 400
        assert r.json()["detail"] == lm.BRANCH_NOT_OF_CO_MSG

    def test_create_body_branch_mismatch_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=None),
            _res(fetchall=[_loom(30, "S293"), _loom(31, "S294")]),
        ]
        r = client.post(f"{BASE}/loom_group_create", json=self._payload(branch_id=99))
        assert r.status_code == 400
        assert r.json()["detail"] == lm.BRANCH_MISMATCH_MSG

    def test_create_inactive_newly_ticked_loom_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=None),
            _res(fetchall=[_loom(30, "S293"), _loom(31, "S294", active=0)]),
        ]
        r = client.post(f"{BASE}/loom_group_create", json=self._payload())
        assert r.status_code == 400
        assert "S294" in r.json()["detail"] and "inactive" in r.json()["detail"]

    def test_create_blank_code_returns_400_without_db(self):
        r = client.post(f"{BASE}/loom_group_create", json=self._payload(group_code="  "))
        assert r.status_code == 400
        assert r.json()["detail"] == lm.GROUP_CODE_BLANK_MSG
        self.db.execute.assert_not_called()

    def test_create_no_members_returns_400(self):
        r = client.post(f"{BASE}/loom_group_create", json=self._payload(machine_ids=[]))
        assert r.status_code == 400
        assert r.json()["detail"] == lm.GROUP_NO_MEMBERS_MSG

    def test_create_bad_purpose_returns_400(self):
        r = client.post(f"{BASE}/loom_group_create", json=self._payload(group_purpose="TEST"))
        assert r.status_code == 400
        assert r.json()["detail"] == lm.GROUP_BAD_PURPOSE_MSG

    def test_create_duplicate_member_returns_400(self):
        r = client.post(f"{BASE}/loom_group_create", json=self._payload(machine_ids=[30, 30]))
        assert r.status_code == 400
        assert "listed twice" in r.json()["detail"]


class TestLoomGroupEditDetailDelete(_Base):
    _group = {"loom_group_id": 5, "co_id": 9, "branch_id": 12, "branch_name": "Empire",
              "group_code": "EJM-L4", "group_name": "Line 4", "group_purpose": "STUDY",
              "remarks": None, "active": 1}

    def test_edit_rename_only_patches_header(self):
        self.db.execute.side_effect = [_res(fetchone=_row(self._group)), _res(fetchone=None), _res()]
        r = client.put(f"{BASE}/loom_group_edit/5?co_id=9", json={"group_code": "EJM-L5"})
        assert r.status_code == 200
        p = self._params_of(2)
        assert p["group_code"] == "EJM-L5" and p["group_name"] is None and p["loom_group_id"] == 5
        self.db.commit.assert_called_once()

    def test_edit_replace_members_keeps_retired_existing_member(self):
        """S293 was retired after joining: it stays (existing) while S294 is newly ticked."""
        self.db.execute.side_effect = [
            _res(fetchone=_row(self._group)),
            _res(fetchall=_ids(30)),
            _res(fetchall=[_loom(30, "S293", active=0), _loom(31, "S294")]),
            _res(),
            _res(),
            _res(),
            _res(),
        ]
        r = client.put(f"{BASE}/loom_group_edit/5?co_id=9", json={"machine_ids": [30, 31]})
        assert r.status_code == 200, r.text
        assert self.db.execute.call_count == 7
        assert self._params_of(4) == {"loom_group_id": 5}
        assert self._params_of(6) == {"loom_group_id": 5, "machine_id": 31}

    def test_edit_newly_ticked_inactive_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=_row(self._group)),
            _res(fetchall=_ids(30)),
            _res(fetchall=[_loom(30, "S293"), _loom(31, "S294", active=0)]),
        ]
        r = client.put(f"{BASE}/loom_group_edit/5?co_id=9", json={"machine_ids": [30, 31]})
        assert r.status_code == 400
        assert "inactive" in r.json()["detail"]
        self.db.commit.assert_not_called()

    def test_edit_branch_without_members_that_differs_returns_400(self):
        self.db.execute.side_effect = [_res(fetchone=_row(self._group))]
        r = client.put(f"{BASE}/loom_group_edit/5?co_id=9", json={"branch_id": 13})
        assert r.status_code == 400
        assert r.json()["detail"] == lm.GROUP_BRANCH_MSG
        assert self.db.execute.call_count == 1
        self.db.commit.assert_not_called()

    def test_edit_branch_without_members_is_never_written(self):
        self.db.execute.side_effect = [_res(fetchone=_row(self._group)), _res()]
        r = client.put(f"{BASE}/loom_group_edit/5?co_id=9", json={"branch_id": 12, "group_name": "Line 4b"})
        assert r.status_code == 200, r.text
        p = self._params_of(1)
        assert p["branch_id"] is None and p["group_name"] == "Line 4b"

    def test_edit_duplicate_code_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=_row(self._group)),
            _res(fetchone=_row({"loom_group_id": 9})),
        ]
        r = client.put(f"{BASE}/loom_group_edit/5?co_id=9", json={"group_code": "OTHER"})
        assert r.status_code == 400
        assert r.json()["detail"] == lm.GROUP_DUP_MSG

    def test_edit_not_found_returns_404(self):
        self.db.execute.side_effect = [_res(fetchone=None)]
        r = client.put(f"{BASE}/loom_group_edit/5?co_id=9", json={"group_name": "x"})
        assert r.status_code == 404

    def test_edit_missing_co_id_returns_400(self):
        r = client.put(f"{BASE}/loom_group_edit/5", json={"group_name": "x"})
        assert r.status_code == 400

    def test_detail_returns_members_and_event_count(self):
        self.db.execute.side_effect = [
            _res(fetchone=_row(self._group)),
            _res(fetchall=[
                _row({"loom_group_dtl_id": 1, "machine_id": 30, "machine_name": "S293",
                      "mech_code": "S293", "line_no": "11", "machine_active": 1}),
                _row({"loom_group_dtl_id": 2, "machine_id": 31, "machine_name": "S294",
                      "mech_code": "S294", "line_no": "11", "machine_active": 0}),
            ]),
            _res(fetchone=_row({"cnt": 2})),
        ]
        r = client.get(f"{BASE}/loom_group_detail/5?co_id=9")
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["group_code"] == "EJM-L4" and d["member_count"] == 2 and d["event_count"] == 2
        assert d["members"][1]["machine_active"] == 0
        assert d["members"][0]["label"] == "S293"

    def test_detail_not_found_returns_404(self):
        self.db.execute.side_effect = [_res(fetchone=None)]
        r = client.get(f"{BASE}/loom_group_detail/5?co_id=9")
        assert r.status_code == 404

    def test_delete_refused_while_events_reference_the_group(self):
        self.db.execute.side_effect = [
            _res(fetchone=_row(self._group)),
            _res(fetchone=_row({"cnt": 3})),
        ]
        r = client.delete(f"{BASE}/loom_group_delete/5?co_id=9")
        assert r.status_code == 400
        assert "3 modification event" in r.json()["detail"]
        self.db.commit.assert_not_called()

    def test_delete_soft_deletes_header_and_members(self):
        self.db.execute.side_effect = [
            _res(fetchone=_row(self._group)),
            _res(fetchone=_row({"cnt": 0})),
            _res(),
            _res(),
        ]
        r = client.delete(f"{BASE}/loom_group_delete/5?co_id=9")
        assert r.status_code == 200
        assert self.db.execute.call_count == 4
        self.db.commit.assert_called_once()

    def test_list_binds_filters(self):
        self.db.execute.return_value.fetchall.return_value = [
            _row({**self._group, "member_count": 6})
        ]
        r = client.get(f"{BASE}/loom_group_list?co_id=9&branch_id=12&group_purpose=study&search=L4")
        assert r.status_code == 200
        assert r.json()["data"][0]["member_count"] == 6
        p = self.db.execute.call_args.args[1]
        assert p == {"co_id": 9, "branch_id": 12, "group_purpose": "STUDY", "search": "%L4%"}


# =============================================================================
# Modification create
# =============================================================================


def _create_payload(**over):
    p = {
        "co_id": 9,
        "members": [{"machine_id": 30}, {"machine_id": 31}],
        "mod_date": "2026-07-01",
        "settle_days": 3,
        "mod_kind": "INITIAL",
        "mod_category": "MECHANICAL",
        "title": "Beat-up cam replaced",
        "done_by": "INHOUSE",
        "total_cost": 1500,
    }
    p.update(over)
    return p


_TWO_LOOMS = [_loom(30, "S293"), _loom(31, "S294")]


class TestModificationCreate(_Base):
    def test_initial_happy_path(self):
        self.db.execute.side_effect = [
            _res(fetchall=[_loom(30, "S293"), _loom(31, "S294")]),  # L1
            _res(fetchall=[]),                                       # O1
            _res(lastrowid=100),                                     # W1
            _res(),                                                  # W2 stamp root
            _res(), _res(),                                          # W4 x2
            _dc(40, 35),                                             # D1
        ]
        r = client.post(f"{BASE}/modification_create", json=_create_payload())
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["modification_id"] == 100 and d["root_modification_id"] == 100
        assert d["data_check"] == {"before_days": 40, "after_days": 35}
        assert "40 producing loom-days" in d["message"]
        ins = self._params_of(2)
        assert ins["branch_id"] == 12 and ins["root_modification_id"] is None
        assert ins["parent_modification_id"] is None and ins["mod_kind"] == "INITIAL"
        assert ins["party_id"] is None and ins["total_cost"] == 1500.0
        assert self._params_of(3) == {"modification_id": 100}
        assert self._params_of(4)["machine_id"] == 30 and self._params_of(4)["done_date"] is None
        assert self._params_of(6)["machine_ids"] == [30, 31]
        self.db.commit.assert_called_once()

    def test_data_check_zero_side_is_flagged_in_message(self):
        self.db.execute.side_effect = [
            _res(fetchall=_TWO_LOOMS), _res(fetchall=[]), _res(lastrowid=100), _res(),
            _res(), _res(), _dc(0, 12),
        ]
        r = client.post(f"{BASE}/modification_create", json=_create_payload())
        assert r.status_code == 200
        assert "NOT ENOUGH DATA" in r.json()["data"]["message"]

    def test_followup_wrong_parent_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event()),                                                  # C1 parent
            _res(fetchone=_row({"modification_id": 105, "title": "Tweak",
                                "mod_date": date(2026, 7, 10), "mod_to_date": None,
                                "mod_kind": "FOLLOWUP"})),                            # C2 latest
        ]
        r = client.post(
            f"{BASE}/modification_create",
            json=_create_payload(mod_kind="FOLLOWUP", parent_modification_id=100, mod_date="2026-07-20"),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "the latest stage of this chain is Tweak, 2026-07-10 — follow that one"
        self.db.commit.assert_not_called()

    def test_same_day_stage_returns_400(self):
        parent = _event(mod_date=date(2026, 7, 1), mod_to_date=date(2026, 7, 3))
        self.db.execute.side_effect = [
            _res(fetchone=parent),
            _res(fetchone=_row({"modification_id": 100, "title": "Beat-up cam replaced",
                                "mod_date": date(2026, 7, 1), "mod_to_date": date(2026, 7, 3),
                                "mod_kind": "INITIAL"})),
        ]
        r = client.post(
            f"{BASE}/modification_create",
            json=_create_payload(mod_kind="FOLLOWUP", parent_modification_id=100, mod_date="2026-07-03"),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.SAME_DAY_MSG.format(end=date(2026, 7, 3))
        self.db.commit.assert_not_called()

    def test_followup_happy_path_inherits_root(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event()),                                  # C1
            _res(fetchone=_row({"modification_id": 100, "title": "Beat-up cam replaced",
                                "mod_date": date(2026, 7, 1), "mod_to_date": None,
                                "mod_kind": "INITIAL"})),             # C2
            _res(fetchall=[]),                                        # C3 no reverted looms
            _res(fetchall=_TWO_LOOMS),
            _res(fetchall=[]),
            _res(lastrowid=102),
            _res(), _res(),
            _dc(),
        ]
        r = client.post(
            f"{BASE}/modification_create",
            json=_create_payload(mod_kind="FOLLOWUP", parent_modification_id=100, mod_date="2026-07-20"),
        )
        assert r.status_code == 200, r.text
        assert r.json()["data"]["root_modification_id"] == 100
        assert self._params_of(2) == {"root_modification_id": 100, "mod_date": date(2026, 7, 20),
                                      "machine_ids": [30, 31]}
        ins = self._params_of(5)
        assert ins["root_modification_id"] == 100 and ins["parent_modification_id"] == 100
        assert self.db.execute.call_count == 9  # no stamp-root step for a FOLLOWUP

    def test_followup_with_reverted_loom_returns_400(self):
        """§5: a REVERSAL closes the timeline for the looms in its snapshot."""
        self.db.execute.side_effect = [
            _res(fetchone=_event()),                                  # C1
            _res(fetchone=_row({"modification_id": 100, "title": "Beat-up cam replaced",
                                "mod_date": date(2026, 7, 1), "mod_to_date": None,
                                "mod_kind": "INITIAL"})),             # C2
            _res(fetchall=[_row({"machine_id": 30, "machine_name": "S293", "mech_code": "S293",
                                 "reverted_on": date(2026, 7, 15), "title": "Undo cam"})]),  # C3
        ]
        r = client.post(
            f"{BASE}/modification_create",
            json=_create_payload(mod_kind="RECTIFY", parent_modification_id=100, mod_date="2026-07-20"),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.REVERTED_LOOM_MSG.format(name="S293", d=date(2026, 7, 15), title="Undo cam")
        assert self.db.execute.call_count == 3
        self.db.commit.assert_not_called()

    def test_per_loom_overlap_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchall=_TWO_LOOMS),
            _res(fetchall=[_row({"machine_id": 30, "machine_name": "S293", "modification_id": 7,
                                 "title": "Old cam", "mod_kind": "INITIAL",
                                 "d_m": date(2026, 6, 25), "e_m": date(2026, 7, 2)})]),
        ]
        r = client.post(f"{BASE}/modification_create", json=_create_payload())
        assert r.status_code == 400
        assert r.json()["detail"] == "loom S293 already has 'Old cam' on 2026-06-25 to 2026-07-02 — the dates overlap"
        assert self._params_of(1)["exclude_id"] is None
        self.db.commit.assert_not_called()

    def test_adjacent_span_is_not_an_overlap(self):
        """Other event ended 30 Jun, this one starts 1 Jul: allowed."""
        self.db.execute.side_effect = [
            _res(fetchall=_TWO_LOOMS),
            _res(fetchall=[_row({"machine_id": 30, "machine_name": "S293", "modification_id": 7,
                                 "title": "Old cam", "mod_kind": "INITIAL",
                                 "d_m": date(2026, 6, 25), "e_m": date(2026, 6, 30)})]),
            _res(lastrowid=100), _res(), _res(), _res(), _dc(),
        ]
        r = client.post(f"{BASE}/modification_create", json=_create_payload())
        assert r.status_code == 200, r.text

    def test_done_date_outside_span_returns_400_without_db(self):
        r = client.post(
            f"{BASE}/modification_create",
            json=_create_payload(members=[{"machine_id": 30, "done_date": "2026-07-05"}]),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.DONE_DATE_RANGE_MSG.format(name="30")
        self.db.execute.assert_not_called()

    def test_done_to_date_before_done_date_returns_400(self):
        r = client.post(
            f"{BASE}/modification_create",
            json=_create_payload(
                mod_to_date="2026-07-10",
                members=[{"machine_id": 30, "done_date": "2026-07-05", "done_to_date": "2026-07-04"}],
            ),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.DONE_TO_ORDER_MSG.format(name="30")

    def test_per_loom_dates_inside_span_are_bound(self):
        self.db.execute.side_effect = [
            _res(fetchall=_TWO_LOOMS), _res(fetchall=[]), _res(lastrowid=100), _res(),
            _res(), _res(), _dc(),
        ]
        r = client.post(
            f"{BASE}/modification_create",
            json=_create_payload(
                mod_to_date="2026-07-10",
                members=[{"machine_id": 30, "done_date": "2026-07-02", "done_to_date": "2026-07-03"},
                         {"machine_id": 31, "done_date": "2026-07-08"}],
            ),
        )
        assert r.status_code == 200, r.text
        assert self._params_of(4)["done_date"] == date(2026, 7, 2)
        assert self._params_of(4)["done_to_date"] == date(2026, 7, 3)
        assert self._params_of(5)["done_to_date"] is None

    def test_future_mod_date_returns_400(self):
        r = client.post(f"{BASE}/modification_create", json=_create_payload(mod_date="2099-01-01"))
        assert r.status_code == 400
        assert r.json()["detail"] == lm.FUTURE_DATE_MSG
        self.db.execute.assert_not_called()

    def test_mod_to_date_before_mod_date_returns_400(self):
        r = client.post(f"{BASE}/modification_create", json=_create_payload(mod_to_date="2026-06-30"))
        assert r.status_code == 400
        assert r.json()["detail"] == lm.DATE_ORDER_MSG

    def test_vendor_without_party_returns_400(self):
        r = client.post(f"{BASE}/modification_create", json=_create_payload(done_by="VENDOR"))
        assert r.status_code == 400
        assert r.json()["detail"] == lm.VENDOR_PARTY_MSG
        self.db.execute.assert_not_called()

    def test_vendor_unknown_party_returns_400(self):
        self.db.execute.side_effect = [_res(fetchone=None)]  # P1
        r = client.post(
            f"{BASE}/modification_create", json=_create_payload(done_by="VENDOR", party_id=77)
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.UNKNOWN_PARTY_MSG
        assert self._params_of(0) == {"party_id": 77, "co_id": 9}

    def test_vendor_with_party_binds_party_id(self):
        self.db.execute.side_effect = [
            _res(fetchone=_row({"party_id": 77})),
            _res(fetchall=_TWO_LOOMS), _res(fetchall=[]), _res(lastrowid=100), _res(),
            _res(), _res(), _dc(),
        ]
        r = client.post(
            f"{BASE}/modification_create", json=_create_payload(done_by="VENDOR", party_id=77)
        )
        assert r.status_code == 200, r.text
        assert self._params_of(3)["party_id"] == 77 and self._params_of(3)["done_by"] == "VENDOR"

    def test_reversal_subset_happy_path(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event()),           # C1 parent (INITIAL)
            _res(fetchall=_ids(30, 31)),       # C2 parent snapshot
            _res(fetchall=[_loom(30, "S293")]),
            _res(fetchall=[]),
            _res(lastrowid=101),
            _res(),
            _dc(),
        ]
        r = client.post(
            f"{BASE}/modification_create",
            json=_create_payload(mod_kind="REVERSAL", parent_modification_id=100,
                                 mod_date="2026-07-15", members=[{"machine_id": 30}]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["data"]["root_modification_id"] == 100
        assert self._params_of(4)["mod_kind"] == "REVERSAL"
        assert self._params_of(4)["root_modification_id"] == 100
        assert self.db.execute.call_count == 7

    def test_reversal_with_loom_outside_parent_snapshot_returns_400(self):
        self.db.execute.side_effect = [_res(fetchone=_event()), _res(fetchall=_ids(30, 31))]
        r = client.post(
            f"{BASE}/modification_create",
            json=_create_payload(mod_kind="REVERSAL", parent_modification_id=100,
                                 mod_date="2026-07-15", members=[{"machine_id": 30}, {"machine_id": 32}]),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.REVERSAL_SUBSET_MSG.format(name=32)
        self.db.commit.assert_not_called()

    def test_parent_that_is_a_reversal_returns_400(self):
        self.db.execute.side_effect = [_res(fetchone=_event(modification_id=101, mod_kind="REVERSAL"))]
        r = client.post(
            f"{BASE}/modification_create",
            json=_create_payload(mod_kind="FOLLOWUP", parent_modification_id=101, mod_date="2026-07-20"),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.PARENT_IS_REVERSAL_MSG

    def test_unknown_parent_returns_400(self):
        self.db.execute.side_effect = [_res(fetchone=None)]
        r = client.post(
            f"{BASE}/modification_create",
            json=_create_payload(mod_kind="RECTIFY", parent_modification_id=999, mod_date="2026-07-20"),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.PARENT_NOT_FOUND_MSG

    def test_initial_with_parent_returns_400(self):
        r = client.post(f"{BASE}/modification_create", json=_create_payload(parent_modification_id=100))
        assert r.status_code == 400
        assert r.json()["detail"] == lm.INITIAL_HAS_PARENT_MSG

    def test_followup_without_parent_returns_400(self):
        r = client.post(f"{BASE}/modification_create", json=_create_payload(mod_kind="FOLLOWUP"))
        assert r.status_code == 400
        assert r.json()["detail"] == lm.PARENT_REQUIRED_MSG

    def test_bad_category_kind_done_by_and_title(self):
        for over, msg in (
            ({"mod_category": "PAINT"}, lm.BAD_CATEGORY_MSG),
            ({"mod_kind": "UNDO"}, lm.BAD_KIND_MSG),
            ({"done_by": "SELF"}, lm.BAD_DONE_BY_MSG),
            ({"title": "  "}, lm.TITLE_BLANK_MSG),
            ({"settle_days": -1}, lm.SETTLE_MSG),
            ({"members": []}, lm.NO_MEMBERS_MSG),
        ):
            r = client.post(f"{BASE}/modification_create", json=_create_payload(**over))
            assert r.status_code == 400, over
            assert r.json()["detail"] == msg
        self.db.execute.assert_not_called()

    def test_non_loom_member_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchall=[_loom(30, "S293"), _loom(31, "SPIN-1", is_loom=0)]),
        ]
        r = client.post(f"{BASE}/modification_create", json=_create_payload())
        assert r.status_code == 400
        assert r.json()["detail"] == "machine 31 is not a loom"

    def test_group_of_another_branch_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=_row({"loom_group_id": 3, "co_id": 9, "branch_id": 99})),  # G1
            _res(fetchall=_TWO_LOOMS),                                              # L1
        ]
        r = client.post(f"{BASE}/modification_create", json=_create_payload(loom_group_id=3))
        assert r.status_code == 400
        assert r.json()["detail"] == lm.BRANCH_MISMATCH_MSG

    def test_unknown_group_returns_400(self):
        self.db.execute.side_effect = [_res(fetchone=None)]
        r = client.post(f"{BASE}/modification_create", json=_create_payload(loom_group_id=3))
        assert r.status_code == 400
        assert r.json()["detail"] == lm.UNKNOWN_GROUP_MSG

    def test_save_as_group_creates_group_first_and_stamps_it(self):
        self.db.execute.side_effect = [
            _res(fetchone=None),                 # group dup check
            _res(fetchall=_TWO_LOOMS),           # group loom context
            _res(lastrowid=7),                   # group insert
            _res(), _res(),                      # group members x2
            _res(fetchone=_row({"loom_group_id": 7, "co_id": 9, "branch_id": 12})),  # G1
            _res(fetchall=_TWO_LOOMS),           # L1
            _res(fetchall=[]),                   # O1
            _res(lastrowid=103),                 # W1
            _res(),                              # W2
            _res(), _res(),                      # W4
            _dc(),                               # D1
        ]
        r = client.post(
            f"{BASE}/modification_create",
            json=_create_payload(save_as_group={"group_code": "NEW", "group_name": "New set"}),
        )
        assert r.status_code == 200, r.text
        assert r.json()["data"]["loom_group_id"] == 7
        assert self._params_of(8)["loom_group_id"] == 7
        self.db.commit.assert_called_once()


# =============================================================================
# Modification edit
# =============================================================================


class TestModificationEdit(_Base):
    def test_edit_happy_path_replaces_snapshot(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event()),                       # E1
            _res(fetchone=_row({"cnt": 0, "min_mod_date": None})),  # E2
            _res(fetchall=_ids(30, 31)),                   # E3
            _res(fetchall=_TWO_LOOMS),                     # L1
            _res(fetchall=[]),                             # O1
            _res(),                                        # W1 update
            _res(),                                        # W3 soft-delete snapshot
            _res(), _res(),                                # W4
            _dc(),                                         # D1
        ]
        r = client.put(f"{BASE}/modification_edit/100", json=_create_payload(title="Cam v2"))
        assert r.status_code == 200, r.text
        assert r.json()["data"]["modification_id"] == 100
        upd = self._params_of(5)
        assert upd["modification_id"] == 100 and upd["root_modification_id"] == 100
        assert upd["title"] == "Cam v2"
        assert self._params_of(4)["exclude_id"] == 100
        assert self._params_of(6) == {"modification_id": 100}
        self.db.commit.assert_called_once()

    def test_edit_keeps_retired_loom_from_old_snapshot(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event()),
            _res(fetchone=_row({"cnt": 0, "min_mod_date": None})),
            _res(fetchall=_ids(30)),
            _res(fetchall=[_loom(30, "S293", active=0), _loom(31, "S294")]),
            _res(fetchall=[]),
            _res(), _res(), _res(), _res(), _dc(),
        ]
        r = client.put(f"{BASE}/modification_edit/100", json=_create_payload())
        assert r.status_code == 200, r.text

    def test_edit_changing_kind_with_children_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event()),
            _res(fetchone=_row({"cnt": 1, "min_mod_date": date(2026, 7, 20)})),
        ]
        r = client.put(
            f"{BASE}/modification_edit/100",
            json=_create_payload(mod_kind="FOLLOWUP", parent_modification_id=5),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.IMMUTABLE_MSG
        self.db.commit.assert_not_called()

    def test_edit_end_past_child_start_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event()),
            _res(fetchone=_row({"cnt": 1, "min_mod_date": date(2026, 7, 20)})),
        ]
        r = client.put(f"{BASE}/modification_edit/100", json=_create_payload(mod_to_date="2026-07-25"))
        assert r.status_code == 400
        assert r.json()["detail"] == lm.CHILD_STARTS_MSG.format(d=date(2026, 7, 20))

    def test_edit_with_children_same_kind_and_parent_is_allowed(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event()),
            _res(fetchone=_row({"cnt": 1, "min_mod_date": date(2026, 7, 20)})),
            _res(fetchall=_ids(30, 31)),
            _res(fetchall=[]),                                # E4 child REVERSAL snapshot (none)
            _res(fetchall=_TWO_LOOMS), _res(fetchall=[]),
            _res(), _res(), _res(), _res(), _dc(),
        ]
        r = client.put(f"{BASE}/modification_edit/100", json=_create_payload(title="Renamed"))
        assert r.status_code == 200, r.text
        assert self._params_of(3) == {"modification_id": 100}

    def test_edit_middle_followup_with_children_is_free_edit(self):
        """Chain 100 INITIAL -> 101 FOLLOWUP -> 102 FOLLOWUP: renaming 101 keeps its
        (frozen) parent, so the latest-stage check (C2) is skipped (D9 free edit)."""
        self.db.execute.side_effect = [
            _res(fetchone=_event(modification_id=101, mod_kind="FOLLOWUP", parent_modification_id=100,
                                 mod_date=date(2026, 7, 10), stage_no=2, stage_count=3)),  # E1
            _res(fetchone=_row({"cnt": 1, "min_mod_date": date(2026, 7, 20)})),           # E2
            _res(fetchall=_ids(30, 31)),                                                    # E3
            _res(fetchall=[]),                                                              # E4
            _res(fetchone=_event()),                                                        # C1 parent 100
            _res(fetchall=[]),                                                              # C3
            _res(fetchall=_TWO_LOOMS), _res(fetchall=[]),                                   # L1, O1
            _res(), _res(), _res(), _res(), _dc(),                                          # W1, W3, W4 x2, D1
        ]
        r = client.put(
            f"{BASE}/modification_edit/101",
            json=_create_payload(mod_kind="FOLLOWUP", parent_modification_id=100,
                                 mod_date="2026-07-10", title="Renamed"),
        )
        assert r.status_code == 200, r.text
        assert self.db.execute.call_count == 13
        upd = self._params_of(8)
        assert upd["modification_id"] == 101 and upd["title"] == "Renamed"
        assert upd["parent_modification_id"] == 100 and upd["root_modification_id"] == 100
        assert all("exclude_id" not in self._params_of(i) or i == 7 for i in range(13))
        self.db.commit.assert_called_once()

    def test_edit_terminal_followup_keeps_latest_stage_check(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event(modification_id=102, mod_kind="FOLLOWUP", parent_modification_id=101,
                                 mod_date=date(2026, 7, 20))),                              # E1
            _res(fetchone=_row({"cnt": 0, "min_mod_date": None})),                          # E2
            _res(fetchall=_ids(30, 31)),                                                    # E3
            _res(fetchone=_event(modification_id=101, mod_kind="FOLLOWUP", parent_modification_id=100,
                                 mod_date=date(2026, 7, 10))),                              # C1
            _res(fetchone=_row({"modification_id": 103, "title": "Later", "mod_date": date(2026, 7, 25),
                                "mod_to_date": None, "mod_kind": "FOLLOWUP"})),             # C2
        ]
        r = client.put(
            f"{BASE}/modification_edit/102",
            json=_create_payload(mod_kind="FOLLOWUP", parent_modification_id=101, mod_date="2026-07-20"),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.NOT_LATEST_MSG.format(title="Later", mod_date=date(2026, 7, 25))
        assert self._params_of(4)["exclude_id"] == 102
        self.db.commit.assert_not_called()

    def test_edit_unticking_loom_carried_by_child_reversal_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event()),
            _res(fetchone=_row({"cnt": 1, "min_mod_date": date(2026, 7, 20)})),
            _res(fetchall=_ids(30, 31)),
            _res(fetchall=[_row({"machine_id": 31, "machine_name": "S294", "mech_code": "S294",
                                 "title": "Undo cam"})]),                                   # E4
        ]
        r = client.put(f"{BASE}/modification_edit/100", json=_create_payload(members=[{"machine_id": 30}]))
        assert r.status_code == 400
        assert r.json()["detail"] == lm.UNTICK_REVERSED_MSG.format(name="S294", title="Undo cam")
        self.db.commit.assert_not_called()

    def test_edit_not_found_returns_404(self):
        self.db.execute.side_effect = [_res(fetchone=None)]
        r = client.put(f"{BASE}/modification_edit/100", json=_create_payload())
        assert r.status_code == 404
        self.db.commit.assert_not_called()


# =============================================================================
# Delete / delete chain
# =============================================================================


class TestModificationDelete(_Base):
    def test_delete_refused_while_children_exist(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event()),
            _res(fetchone=_row({"cnt": 2, "min_mod_date": date(2026, 7, 20)})),
        ]
        r = client.delete(f"{BASE}/modification_delete/100?co_id=9")
        assert r.status_code == 400
        assert r.json()["detail"] == "has 2 follow-up(s) — delete the follow-ups first"
        self.db.commit.assert_not_called()

    def test_delete_chain_on_root_soft_deletes_snapshots_and_events(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event(stage_count=3)),
            _res(fetchone=_row({"cnt": 1, "min_mod_date": date(2026, 7, 20)})),
            _res(),
            _res(),
        ]
        r = client.delete(f"{BASE}/modification_delete/100?co_id=9&chain=1")
        assert r.status_code == 200
        assert r.json()["data"]["deleted_stages"] == 3
        assert self._params_of(2) == {"root_modification_id": 100}
        assert self._params_of(3) == {"root_modification_id": 100, "updated_by": 1}
        self.db.commit.assert_called_once()

    def test_delete_chain_on_non_root_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event(modification_id=101, parent_modification_id=100, mod_kind="FOLLOWUP")),
            _res(fetchone=_row({"cnt": 0, "min_mod_date": None})),
        ]
        r = client.delete(f"{BASE}/modification_delete/101?co_id=9&chain=1")
        assert r.status_code == 400
        assert r.json()["detail"] == lm.CHAIN_ON_ROOT_ONLY_MSG

    def test_delete_plain_soft_deletes_snapshot_and_event(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event()),
            _res(fetchone=_row({"cnt": 0, "min_mod_date": None})),
            _res(),
            _res(),
        ]
        r = client.delete(f"{BASE}/modification_delete/100?co_id=9")
        assert r.status_code == 200
        assert self._params_of(2) == {"modification_id": 100}
        assert self._params_of(3) == {"modification_id": 100, "updated_by": 1}
        self.db.commit.assert_called_once()

    def test_delete_not_found_returns_404(self):
        self.db.execute.side_effect = [_res(fetchone=None)]
        r = client.delete(f"{BASE}/modification_delete/100?co_id=9")
        assert r.status_code == 404

    def test_delete_missing_co_id_returns_400(self):
        r = client.delete(f"{BASE}/modification_delete/100")
        assert r.status_code == 400


# =============================================================================
# Insert earlier stage
# =============================================================================


class TestInsertEarlier(_Base):
    def test_insert_earlier_reparents_old_root(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event()),              # old root (mod_date 2026-07-01)
            _res(fetchall=_TWO_LOOMS),            # L1
            _res(fetchall=[]),                    # O1
            _res(lastrowid=200),                  # W1
            _res(),                               # W2 stamp
            _res(), _res(),                       # W4
            _dc(),                                # D1
            _res(),                               # rewrite chain root
            _res(),                               # re-parent old root
        ]
        r = client.post(
            f"{BASE}/modification_insert_earlier/100",
            json=_create_payload(mod_date="2026-06-01", title="Earlier cam", old_root_kind="RECTIFY",
                                 mod_kind="FOLLOWUP", parent_modification_id=100),
        )
        assert r.status_code == 200, r.text
        assert r.json()["data"]["modification_id"] == 200
        ins = self._params_of(3)
        assert ins["mod_kind"] == "INITIAL" and ins["parent_modification_id"] is None
        assert ins["branch_id"] == 12
        assert self._params_of(8) == {"new_root": 200, "old_root": 100}
        assert self._params_of(9) == {"new_root": 200, "old_root": 100, "mod_kind": "RECTIFY", "updated_by": 1}
        self.db.commit.assert_called_once()

    def test_insert_earlier_must_end_before_old_root(self):
        self.db.execute.side_effect = [_res(fetchone=_event())]
        r = client.post(
            f"{BASE}/modification_insert_earlier/100",
            json=_create_payload(mod_date="2026-06-20", mod_to_date="2026-07-01"),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "the earlier stage must end before 2026-07-01"
        self.db.commit.assert_not_called()

    def test_insert_earlier_body_branch_differs_from_chain_returns_400(self):
        self.db.execute.side_effect = [_res(fetchone=_event())]
        r = client.post(
            f"{BASE}/modification_insert_earlier/100",
            json=_create_payload(mod_date="2026-06-01", branch_id=13),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.INSERT_EARLIER_BRANCH_MSG.format(branch=12)
        assert self.db.execute.call_count == 1
        self.db.commit.assert_not_called()

    def test_insert_earlier_looms_of_another_branch_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event()),
            _res(fetchall=[_loom(40, "T1", branch_id=13), _loom(41, "T2", branch_id=13)]),  # L1
        ]
        r = client.post(
            f"{BASE}/modification_insert_earlier/100",
            json=_create_payload(mod_date="2026-06-01", members=[{"machine_id": 40}, {"machine_id": 41}]),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.BRANCH_MISMATCH_MSG
        self.db.commit.assert_not_called()

    def test_insert_earlier_on_non_root_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event(modification_id=101, parent_modification_id=100, mod_kind="FOLLOWUP")),
        ]
        r = client.post(
            f"{BASE}/modification_insert_earlier/101", json=_create_payload(mod_date="2026-06-01")
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.INSERT_ROOT_ONLY_MSG

    def test_insert_earlier_bad_old_root_kind_returns_400(self):
        r = client.post(
            f"{BASE}/modification_insert_earlier/100",
            json=_create_payload(mod_date="2026-06-01", old_root_kind="REVERSAL"),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.BAD_OLD_ROOT_KIND_MSG
        self.db.execute.assert_not_called()


# =============================================================================
# Add looms to this stage
# =============================================================================


class TestAddMembers(_Base):
    def test_add_members_happy_path(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event(mod_to_date=date(2026, 7, 10))),
            _res(fetchall=_ids(30, 31)),
            _res(fetchall=[_loom(32, "S295")]),
            _res(fetchall=[]),
            _res(),
        ]
        r = client.post(
            f"{BASE}/modification_add_members/100",
            json={"co_id": 9, "members": [{"machine_id": 32, "done_date": "2026-07-08"}]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["data"]["added"] == 1
        ins = self._params_of(4)
        assert ins["modification_id"] == 100 and ins["machine_id"] == 32
        assert ins["done_date"] == date(2026, 7, 8)
        assert self._params_of(3)["exclude_id"] == 100
        self.db.commit.assert_called_once()

    def test_add_members_already_in_stage_returns_400(self):
        self.db.execute.side_effect = [_res(fetchone=_event()), _res(fetchall=_ids(30, 31))]
        r = client.post(
            f"{BASE}/modification_add_members/100", json={"co_id": 9, "members": [{"machine_id": 31}]}
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.ALREADY_MEMBER_MSG.format(name=31)
        self.db.commit.assert_not_called()

    def test_add_members_done_date_outside_event_returns_400(self):
        self.db.execute.side_effect = [_res(fetchone=_event())]
        r = client.post(
            f"{BASE}/modification_add_members/100",
            json={"co_id": 9, "members": [{"machine_id": 32, "done_date": "2026-07-08"}]},
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.DONE_DATE_RANGE_MSG.format(name="32")

    def test_add_members_reversal_subset_rule(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event(modification_id=101, mod_kind="REVERSAL", parent_modification_id=100,
                                 mod_date=date(2026, 7, 15))),
            _res(fetchall=_ids(30)),      # current snapshot
            _res(fetchall=_ids(30, 31)),  # parent snapshot
        ]
        r = client.post(
            f"{BASE}/modification_add_members/101", json={"co_id": 9, "members": [{"machine_id": 32}]}
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.REVERSAL_SUBSET_MSG.format(name=32)

    def test_add_members_on_followup_with_reverted_loom_returns_400(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event(modification_id=101, mod_kind="FOLLOWUP", parent_modification_id=100,
                                 mod_date=date(2026, 7, 20))),
            _res(fetchall=_ids(30)),                                            # current snapshot
            _res(fetchall=[_row({"machine_id": 32, "machine_name": "S295", "mech_code": "S295",
                                 "reverted_on": date(2026, 7, 15), "title": "Undo cam"})]),  # C3
        ]
        r = client.post(
            f"{BASE}/modification_add_members/101", json={"co_id": 9, "members": [{"machine_id": 32}]}
        )
        assert r.status_code == 400
        assert r.json()["detail"] == lm.REVERTED_LOOM_MSG.format(name="S295", d=date(2026, 7, 15), title="Undo cam")
        assert self._params_of(2) == {"root_modification_id": 100, "mod_date": date(2026, 7, 20), "machine_ids": [32]}
        self.db.commit.assert_not_called()

    def test_add_members_on_followup_passes_reverted_check(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event(modification_id=101, mod_kind="FOLLOWUP", parent_modification_id=100,
                                 mod_date=date(2026, 7, 20))),
            _res(fetchall=_ids(30)),
            _res(fetchall=[]),                                                  # C3 none
            _res(fetchall=[_loom(32, "S295")]),
            _res(fetchall=[]),
            _res(),
        ]
        r = client.post(
            f"{BASE}/modification_add_members/101", json={"co_id": 9, "members": [{"machine_id": 32}]}
        )
        assert r.status_code == 200, r.text
        assert self.db.execute.call_count == 6
        self.db.commit.assert_called_once()

    def test_add_members_not_found_returns_404(self):
        self.db.execute.side_effect = [_res(fetchone=None)]
        r = client.post(
            f"{BASE}/modification_add_members/100", json={"co_id": 9, "members": [{"machine_id": 32}]}
        )
        assert r.status_code == 404


# =============================================================================
# Reads
# =============================================================================


class TestReads(_Base):
    def test_table_returns_stage_label_scope_and_data_check(self):
        self.db.execute.side_effect = [
            _res(fetchone=_row({"total": 2})),
            _res(fetchall=[
                _event(modification_id=101, mod_kind="FOLLOWUP", parent_modification_id=100,
                       mod_date=date(2026, 7, 20), stage_no=2, stage_count=2, loom_count=1,
                       group_code="EJM-L4", loom_group_id=3),
                _event(stage_no=1, stage_count=2),
            ]),
            _res(fetchall=[_row({"modification_id": 100, "before_days": 40, "after_days": 12})]),
        ]
        r = client.get(f"{BASE}/modification_table?co_id=9&branch_id=12&page=1&limit=500")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 2 and body["page"] == 1 and body["page_size"] == 200
        first, second = body["data"]
        assert first["stage_label"] == "2 of 2" and first["mod_kind_label"] == "Follow-up"
        assert first["scope_label"] == "Group EJM-L4 — 1 looms"
        assert first["data_check"] == {"before_days": 0, "after_days": 0}
        assert second["scope_label"] == "2 looms picked"
        assert second["data_check"] == {"before_days": 40, "after_days": 12}
        assert second["mod_date"] == "2026-07-01" and second["total_cost"] == 1500.0
        assert self._params_of(2)["modification_ids"] == [101, 100]

    def test_table_filters_bind_and_empty_page_skips_data_check(self):
        self.db.execute.side_effect = [_res(fetchone=_row({"total": 0})), _res(fetchall=[])]
        r = client.get(
            f"{BASE}/modification_table?co_id=9&machine_id=30&loom_group_id=3"
            "&date_from=2026-07-01&date_to=2026-07-31&mod_kind=initial"
        )
        assert r.status_code == 200
        assert r.json()["data"] == []
        p = self._params_of(0)
        assert p["machine_id"] == 30 and p["loom_group_id"] == 3 and p["mod_kind"] == "INITIAL"
        assert p["date_from"] == date(2026, 7, 1)
        assert self.db.execute.call_count == 2

    def test_table_bad_mod_kind_returns_400(self):
        r = client.get(f"{BASE}/modification_table?co_id=9&mod_kind=UNDO")
        assert r.status_code == 400
        assert r.json()["detail"] == lm.BAD_KIND_MSG

    def test_by_id_returns_members_parent_and_children(self):
        self.db.execute.side_effect = [
            _res(fetchone=_event(modification_id=101, mod_kind="FOLLOWUP", parent_modification_id=100,
                                 mod_date=date(2026, 7, 20))),
            _res(fetchall=[_row({"modification_mc_id": 1, "modification_id": 101, "machine_id": 30,
                                 "machine_name": "S293", "mech_code": "S293", "line_no": "11",
                                 "machine_active": 1, "done_date": None, "done_to_date": None,
                                 "remarks": None, "d_m": date(2026, 7, 20), "e_m": date(2026, 7, 20)})]),
            _res(fetchone=_event()),
            _res(fetchall=[]),
        ]
        r = client.get(f"{BASE}/modification_by_id/101?co_id=9")
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["members"][0]["label"] == "S293" and d["members"][0]["d_m"] == "2026-07-20"
        assert d["parent"]["modification_id"] == 100 and "description" not in d["parent"]
        assert d["children"] == [] and d["description"] == "cam"

    def test_by_id_not_found_returns_404(self):
        self.db.execute.side_effect = [_res(fetchone=None)]
        r = client.get(f"{BASE}/modification_by_id/101?co_id=9")
        assert r.status_code == 404

    def test_chain_groups_snapshots_per_event(self):
        self.db.execute.side_effect = [
            _res(fetchall=[_event(), _event(modification_id=101, mod_kind="RECTIFY",
                                            parent_modification_id=100, mod_date=date(2026, 7, 20))]),
            _res(fetchall=[
                _row({"modification_mc_id": 1, "modification_id": 100, "machine_id": 30,
                      "machine_name": "S293", "mech_code": "S293", "machine_active": 1,
                      "done_date": None, "done_to_date": None, "remarks": None,
                      "d_m": date(2026, 7, 1), "e_m": date(2026, 7, 1)}),
                _row({"modification_mc_id": 2, "modification_id": 101, "machine_id": 30,
                      "machine_name": "S293", "mech_code": "S293", "machine_active": 1,
                      "done_date": None, "done_to_date": None, "remarks": None,
                      "d_m": date(2026, 7, 20), "e_m": date(2026, 7, 20)}),
            ]),
        ]
        r = client.get(f"{BASE}/modification_chain/100?co_id=9")
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["root_modification_id"] == 100
        assert [len(e["members"]) for e in d["events"]] == [1, 1]
        assert d["events"][1]["mod_kind_label"] == "Rectification"

    def test_chain_not_found_returns_404(self):
        self.db.execute.side_effect = [_res(fetchall=[])]
        r = client.get(f"{BASE}/modification_chain/100?co_id=9")
        assert r.status_code == 404

    def test_machine_timeline_requires_machine_id(self):
        r = client.get(f"{BASE}/machine_timeline?co_id=9")
        assert r.status_code == 400
        assert "machine_id" in r.json()["detail"]

    def test_machine_timeline_returns_rows_in_order(self):
        self.db.execute.side_effect = [
            _res(fetchall=[_row({"modification_mc_id": 1, "modification_id": 100,
                                 "root_modification_id": 100, "parent_modification_id": None,
                                 "mod_kind": "INITIAL", "mod_category": "MECHANICAL", "title": "Cam",
                                 "mod_date": date(2026, 7, 1), "mod_to_date": None, "settle_days": 3,
                                 "loom_group_id": None, "group_code": None, "done_date": None,
                                 "done_to_date": None, "remarks": None,
                                 "d_m": date(2026, 7, 1), "e_m": date(2026, 7, 1)})]),
        ]
        r = client.get(f"{BASE}/machine_timeline?co_id=9&machine_id=30")
        assert r.status_code == 200
        row = r.json()["data"][0]
        assert row["d_m"] == "2026-07-01" and row["mod_category_label"] == "Mechanical"
        assert self._params_of(0) == {"co_id": 9, "machine_id": 30}
