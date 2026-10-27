"""
Seed tenant `tally_link_mst` from the legacy Tally name mapping (EMPMILL12.tbl_tally_link_file).

WHY
---
The new Tally export needs, per company, the exact Tally ledger / stock-item / godown name
for each ERP record (dbqueries/migrations/create_tally_link_mst.sql). The legacy portal kept
that mapping in EMPMILL12.tbl_tally_link_file; this one-off copies the sales-side rows so
accounts do not re-key ~200 names. Exporters fall back to the ERP name when no row exists.

SOURCE ROWS (is_active = 1; S / P / C = purchase side are ignored)
    link_for  M -> link_type PARTY   keyed by vow_name (customer name); vowid is NULL
    link_for  Q -> link_type ITEM    vowid = legacy item id, vow_name = legacy item name
    link_for  G -> link_type GODOWN  company-level godown name, ref_id 0
    company_id = tenant co_mst.co_id (every company is verified to exist; absent ones are
    reported as NO_COMPANY and skipped).

MAPPING RULES (verified, never guessed)
    PARTY   tenant party_mst WHERE co_id = company_id AND active = 1, matched on the
            normalised name: tier 1 UPPER/trim/collapse-spaces, tier 2 also without '.' and
            ',', tier 3 also PRIVATE LIMITED -> PVT LTD and LIMITED -> LTD. The first tier
            with any hit decides: exactly one -> mapped; several -> AMBIGUOUS. No hit ->
            UNMATCHED. No fuzzy auto-mapping; the CSV lists closest candidates for review.
    ITEM    vowid is the legacy item id, which the vowsls->sls load kept in
            item_mst.item_code (item_mst has no co_id; company = item_grp_mst.co_id, raw
            jute = item_grp_mst.item_type_id 2). Raw-jute items were later renamed / moved /
            merged, so the CURRENT item is resolved through the cleanup ledger first:
              vowsls_to_sls_transfer.jute_cleanup_map (entity item, co_id, old_code = vowid)
                keep/rename/move_group -> old_id,  merge_into -> target_id (chain followed),
                retype_waste -> no longer raw jute -> UNRESOLVED
            and only when the ledger has no row for (co, vowid) by a direct
            item_code match on active raw-jute items of that company. The resolved item must
            be active, in the same company and raw jute. (_map_items is keyed by vowsls ids
            and has no hit for these ids, so it is not used.)
    GODOWN  one row per company, ref_id 0.
    DUPLICATES  two legacy rows landing on the same (co_id, link_type, ref_id): the higher
            tally_link_file_id wins, the other is listed as DUP_DROPPED.
    tally_name is copied byte-for-byte (Tally matches exactly); leading / trailing / double
    spaces and NBSP are only flagged in the CSV note column.

USAGE
    python tools/seed_tally_link_from_legacy.py <tenant_db> [--out CSV] [--since 2026-04-01]
        DRY RUN (default): the session is READ ONLY, nothing is written to any database.
        Prints a summary per link type and per company, writes <CSV> (every proposed and
        unmatched row) and <CSV stem>_gaps.csv (raw-jute invoice parties / items without a
        mapping after seeding), and reports invoice coverage since --since.
    python tools/seed_tally_link_from_legacy.py <tenant_db> --apply [--out CSV]
        Refuses to run when tally_link_mst does not exist (this tool never creates it).
        One transaction: INSERT ... ON DUPLICATE KEY UPDATE tally_name = VALUES(tally_name),
        active = 1. Writes <CSV stem>_applied.csv (tally_link_id + INSERTED/UPDATED + the
        previous values) and <CSV stem>_rollback.sql.
    python tools/seed_tally_link_from_legacy.py --self-test

ROLLBACK (after --apply; the *_rollback.sql file is generated from the applied CSV)
    DELETE FROM tally_link_mst WHERE tally_link_id IN (<ids with apply_action = INSERTED>);
    UPDATE tally_link_mst SET tally_name = <prev_tally_name>, active = <prev_active>
        WHERE tally_link_id = <id>;            -- one per apply_action = UPDATED row
    Never rollback with a bare `DELETE ... WHERE co_id IN (...)`: it would also drop names
    users added through the master page.
"""
import argparse
import csv
import difflib
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import pymysql
from dotenv import dotenv_values

# One-off tool, not app code: legacy / ledger DB names are deliberately constants here.
LEGACY_TABLE = "EMPMILL12.tbl_tally_link_file"
CLEANUP_TABLE = "vowsls_to_sls_transfer.jute_cleanup_map"
TARGET = "tally_link_mst"
LINK_TYPE = {"M": "PARTY", "Q": "ITEM", "G": "GODOWN"}
RAW_JUTE_ITEM_TYPE = 2
RAW_JUTE_INVOICE_TYPE = 5  # sales invoice_type_mst: Raw Jute
APPROVED_STATUS = 3
REPO = Path(__file__).resolve().parents[1]

CSV_COLS = ["status", "tally_link_file_id", "co_id", "link_type", "link_for", "vowid", "vow_name",
            "ref_id", "ref_name", "tally_name", "resolution_path", "candidates", "note"]


# --------------------------------------------------------------------------- name rules
def _ws(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip().upper()


def name_tiers(name):
    """Three progressively looser normal forms of a company / party name."""
    t1 = _ws(name)
    t2 = _ws(t1.replace(".", "").replace(",", ""))
    t3 = _ws(t2.replace("PRIVATE LIMITED", "PVT LTD").replace("LIMITED", "LTD"))
    return [t1, t2, t3]


def _squash(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def ws_flags(name):
    f = []
    if name != name.strip():
        f.append("leading/trailing space")
    if "\xa0" in name:
        f.append("NBSP")
    if "  " in name:
        f.append("double space")
    return f


def self_test():
    assert name_tiers("Lahoty Brothers Pvt. Ltd.")[2] == name_tiers("LAHOTY BROTHERS PRIVATE LIMITED")[2]
    assert name_tiers("MBG  Commodities Private Limited")[2] == name_tiers("MBG COMMODITIES PVT LTD")[2]
    assert name_tiers("Siddhartha Electrotec & Trading Pvt. Ltd.")[2] == "SIDDHARTHA ELECTROTEC & TRADING PVT LTD"
    assert name_tiers("A\xa0B")[0] == "A B"
    assert ws_flags(" x\xa0y  z") == ["leading/trailing space", "NBSP", "double space"]
    rows = [{"status": "PROPOSED", "tally_link_file_id": i, "co_id": 1, "link_type": "ITEM",
             "ref_id": 7, "note": ""} for i in (5, 9, 6)]
    dedupe(rows)
    assert [r["status"] for r in rows] == ["DUP_DROPPED", "PROPOSED", "DUP_DROPPED"], rows
    print("self-test OK")


# --------------------------------------------------------------------------- db helpers
def connect(env, db, read_only):
    conn = pymysql.connect(
        host=env["DATABASE_HOST"], port=int(env["DATABASE_PORT"]), user=env["DATABASE_USER"],
        password=env["DATABASE_PASSWORD"], database=db, charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, autocommit=False,
        connect_timeout=30, read_timeout=600,
    )
    if read_only:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute("SELECT @@session.transaction_read_only AS ro")
            if cur.fetchone()["ro"] != 1:
                sys.exit("REFUSED: could not make the session READ ONLY")
    return conn


def q(cur, sql, args=None):
    cur.execute(sql, args)
    return cur.fetchall()


# --------------------------------------------------------------------------- resolution
class Resolver:
    def __init__(self, cur, companies):
        self.cur = cur
        self.companies = companies
        self._rj_items = {}
        # PARTY index: tier -> (co, key) -> [party rows]
        self.pidx = [defaultdict(list) for _ in range(3)]
        self.party_by_co = defaultdict(list)
        self.party_canon_cos = defaultdict(list)
        for p in q(cur, "SELECT party_id, co_id, supp_name FROM party_mst "
                        "WHERE active = 1 AND co_id IN %s", (tuple(companies) or (0,),)):
            self.party_by_co[p["co_id"]].append(p)
        for p in q(cur, "SELECT party_id, co_id, supp_name FROM party_mst WHERE active = 1"):
            self.party_canon_cos[name_tiers(p["supp_name"])[2]].append((p["co_id"], p["party_id"]))
        for co, plist in self.party_by_co.items():
            for p in plist:
                for k, key in enumerate(name_tiers(p["supp_name"])):
                    self.pidx[k][(co, key)].append(p)

    # ---- PARTY
    def party(self, co, name):
        """-> (party_id, ref_name, path, candidates, reason)"""
        tiers = name_tiers(name)
        for k in range(3):
            hits = self.pidx[k].get((co, tiers[k]), [])
            if len(hits) == 1:
                return hits[0]["party_id"], hits[0]["supp_name"], f"name tier {k + 1}", "", ""
            if len(hits) > 1:
                cands = "; ".join(f"{h['party_id']}:{h['supp_name']}" for h in hits)
                return None, "", f"name tier {k + 1}", cands, "AMBIGUOUS"
        canon = {_squash(p["supp_name"]): p for p in self.party_by_co[co]}
        close = difflib.get_close_matches(_squash(name), list(canon), n=3, cutoff=0.8)
        cands = "; ".join(f"{canon[c]['party_id']}:{canon[c]['supp_name']}" for c in close)
        elsewhere = [f"co{c}/party{pid}" for c, pid in self.party_canon_cos.get(tiers[2], [])][:6]
        if elsewhere:
            cands = (cands + " | " if cands else "") + "same name in other co: " + ", ".join(elsewhere)
        return None, "", "", cands, "NO_PARTY_IN_CO"

    # ---- ITEM
    def _item(self, item_id):
        rows = q(self.cur,
                 "SELECT i.item_id, i.item_code, i.item_name, i.active, g.co_id, g.item_type_id "
                 "FROM item_mst i JOIN item_grp_mst g ON g.item_grp_id = i.item_grp_id "
                 "WHERE i.item_id = %s", (item_id,))
        return rows[0] if rows else None

    def _raw_jute_items(self, co):
        if co not in self._rj_items:
            self._rj_items[co] = q(
                self.cur,
                "SELECT i.item_id, i.item_code, i.item_name FROM item_mst i "
                "JOIN item_grp_mst g ON g.item_grp_id = i.item_grp_id "
                "WHERE g.co_id = %s AND g.item_type_id = %s AND i.active = 1",
                (co, RAW_JUTE_ITEM_TYPE))
        return self._rj_items[co]

    def _follow(self, co, m):
        """Cleanup-ledger row -> (current item_id or None, path, reason)."""
        act, path, item_id = m["action"], f"cleanup#{m['map_id']} {m['action']}", m["old_id"]
        for _ in range(6):
            if act == "merge_into":
                item_id = m["target_id"]
                path += f" -> {item_id}"
                nxt = q(self.cur,
                        f"SELECT map_id, old_id, action, target_id FROM {CLEANUP_TABLE} "
                        "WHERE entity = 'item' AND co_id = %s AND old_id = %s AND status = 'applied'",
                        (co, item_id))
                if nxt and nxt[0]["action"] in ("merge_into", "retype_waste"):
                    m, act = nxt[0], nxt[0]["action"]
                    path += f" [cleanup#{m['map_id']} {act}]"
                    continue
            elif act == "retype_waste":
                return None, path, "RETYPED_WASTE"
            return item_id, path, ""
        return None, path, "MERGE_CHAIN_TOO_LONG"

    def item(self, co, vowid):
        """-> (item_id, ref_name, path, candidates, reason)"""
        code = str(vowid)
        maps = q(self.cur,
                 f"SELECT map_id, old_id, action, target_id FROM {CLEANUP_TABLE} "
                 "WHERE entity = 'item' AND co_id = %s AND old_code = %s AND status = 'applied'",
                 (co, code))
        found, why = {}, []
        for m in maps:
            item_id, path, reason = self._follow(co, m)
            if reason:
                why.append(f"{path}: {reason}")
                continue
            it = self._item(item_id)
            if not it:
                why.append(f"{path}: item {item_id} missing")
            elif not it["active"]:
                why.append(f"{path}: item {item_id} inactive")
            elif it["co_id"] != co or it["item_type_id"] != RAW_JUTE_ITEM_TYPE:
                why.append(f"{path}: item {item_id} is co {it['co_id']} type {it['item_type_id']}")
            else:
                found[it["item_id"]] = (it["item_name"], path)
        if not maps:
            for it in self._raw_jute_items(co):
                if it["item_code"] == code:
                    found[it["item_id"]] = (it["item_name"], f"item_code={code}")
        if len(found) == 1:
            (iid, (nm, path)), = found.items()
            return iid, nm, path, "", ""
        if len(found) > 1:
            return None, "", "", "; ".join(f"{i}:{v[0]}" for i, v in found.items()), "AMBIGUOUS"
        return None, "", " | ".join(why), "", "RESOLVES_TO_NON_RAW_JUTE" if why else "NO_ITEM_CODE_MATCH"

    def item_candidates(self, co, vow_name):
        items = {_squash(i["item_name"]): i for i in self._raw_jute_items(co)}
        close = difflib.get_close_matches(_squash(vow_name), list(items), n=3, cutoff=0.5)
        return "; ".join(f"{items[c]['item_id']}:{items[c]['item_name']}" for c in close)


def dedupe(rows):
    """Same (co, type, ref): keep the highest tally_link_file_id, mark the rest DUP_DROPPED."""
    groups = defaultdict(list)
    for r in rows:
        if r["status"] == "PROPOSED":
            groups[(r["co_id"], r["link_type"], r["ref_id"])].append(r)
    for g in groups.values():
        g.sort(key=lambda r: r["tally_link_file_id"])
        for r in g[:-1]:
            r["status"] = "DUP_DROPPED"
            r["note"] = (r["note"] + f" superseded by id {g[-1]['tally_link_file_id']}").strip()


def seed(cur):
    src = q(cur, "SELECT tally_link_file_id, vowid, tally_name, link_for, vow_name, company_id "
                 f"FROM {LEGACY_TABLE} WHERE is_active = 1 AND link_for IN ('M','Q','G') "
                 "ORDER BY tally_link_file_id")
    cos = {r["co_id"]: r["co_name"] for r in q(cur, "SELECT co_id, co_name FROM co_mst")}
    res = Resolver(cur, {s["company_id"] for s in src if s["company_id"] in cos})
    rows = []
    for s in src:
        co, lf = s["company_id"], s["link_for"]
        r = {"status": "PROPOSED", "tally_link_file_id": s["tally_link_file_id"], "co_id": co,
             "link_type": LINK_TYPE[lf], "link_for": lf, "vowid": s["vowid"], "vow_name": s["vow_name"],
             "ref_id": 0, "ref_name": "", "tally_name": s["tally_name"], "resolution_path": "",
             "candidates": "", "note": ""}
        flags = ws_flags(s["tally_name"])
        r["note"] = ("tally_name has " + ", ".join(flags)) if flags else ""
        if co not in cos:
            r["status"], r["note"] = "NO_COMPANY", "company_id not in co_mst"
        elif lf == "M":
            ref, nm, path, cands, reason = res.party(co, s["vow_name"])
            r.update(ref_id=ref or 0, ref_name=nm, resolution_path=path, candidates=cands)
            if reason:
                r["status"], r["note"] = "UNMATCHED", "; ".join(x for x in (reason, r["note"]) if x)
        elif lf == "Q":
            ref, nm, path, cands, reason = res.item(co, s["vowid"])
            r.update(ref_id=ref or 0, ref_name=nm, resolution_path=path, candidates=cands)
            if reason:
                r["status"], r["note"] = "UNMATCHED", "; ".join(x for x in (reason, r["note"]) if x)
                if not cands:
                    r["candidates"] = res.item_candidates(co, s["vow_name"])
        else:
            r["resolution_path"] = "company godown"
            if re.search(r"\d.*,", s["tally_name"] or ""):
                r["note"] = (r["note"] + " looks like an address, not a godown name").strip()
        rows.append(r)
    dedupe(rows)
    return rows, cos


# --------------------------------------------------------------------------- reporting
def write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def print_summary(rows):
    print("\n== Summary per link type ==")
    print(f"{'type':8}{'source':>8}{'mapped':>8}{'unmatched':>10}{'dup_drop':>9}{'no_co':>7}")
    for lt in ("PARTY", "ITEM", "GODOWN"):
        rs = [r for r in rows if r["link_type"] == lt]
        c = lambda st: sum(1 for r in rs if r["status"] == st)
        print(f"{lt:8}{len(rs):>8}{c('PROPOSED'):>8}{c('UNMATCHED'):>10}{c('DUP_DROPPED'):>9}{c('NO_COMPANY'):>7}")
    print(f"{'TOTAL':8}{len(rows):>8}{sum(r['status'] == 'PROPOSED' for r in rows):>8}"
          f"{sum(r['status'] == 'UNMATCHED' for r in rows):>10}"
          f"{sum(r['status'] == 'DUP_DROPPED' for r in rows):>9}"
          f"{sum(r['status'] == 'NO_COMPANY' for r in rows):>7}")
    print("\n== Per company (source->mapped) ==")
    per = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in rows:
        per[r["co_id"]][r["link_type"]][0] += 1
        per[r["co_id"]][r["link_type"]][1] += r["status"] == "PROPOSED"
    for co in sorted(per):
        p = per[co]
        print(f"  co {co:<4} PARTY {p['PARTY'][0]:>2}->{p['PARTY'][1]:<2}  ITEM {p['ITEM'][0]:>2}->{p['ITEM'][1]:<2}"
              f"  GODOWN {p['GODOWN'][0]}->{p['GODOWN'][1]}")


def print_unmatched(rows):
    for st in ("UNMATCHED", "DUP_DROPPED", "NO_COMPANY"):
        rs = [r for r in rows if r["status"] == st]
        if not rs:
            continue
        print(f"\n== {st} ({len(rs)}) ==")
        for r in rs:
            print(f"  [{r['link_type']}] co {r['co_id']} id {r['tally_link_file_id']} "
                  f"vowid={r['vowid']} {r['vow_name']!r} -> tally {r['tally_name']!r} | {r['note']}"
                  + (f" | cand: {r['candidates']}" if r["candidates"] else "")
                  + (f" | via {r['resolution_path']}" if r["resolution_path"] and st == "UNMATCHED" else ""))


def coverage(cur, rows, since, gaps_path):
    keys = {(r["co_id"], r["link_type"], r["ref_id"]) for r in rows if r["status"] == "PROPOSED"}
    inv = q(cur,
            "SELECT bm.co_id, ih.invoice_id, ih.party_id, d.item_id, pm.supp_name, im.item_name "
            "FROM sales_invoice ih JOIN branch_mst bm ON bm.branch_id = ih.branch_id "
            "JOIN sales_invoice_dtl d ON d.invoice_id = ih.invoice_id "
            "LEFT JOIN party_mst pm ON pm.party_id = ih.party_id "
            "LEFT JOIN item_mst im ON im.item_id = d.item_id "
            "WHERE ih.invoice_type = %s AND ih.status_id = %s AND ih.invoice_date >= %s",
            (RAW_JUTE_INVOICE_TYPE, APPROVED_STATUS, since))
    pairs = {"PARTY": {}, "ITEM": {}}
    inv_ok = defaultdict(lambda: True)
    inv_co = {}
    for x in inv:
        co = x["co_id"]
        inv_co[x["invoice_id"]] = co
        for lt, ref, nm in (("PARTY", x["party_id"], x["supp_name"]), ("ITEM", x["item_id"], x["item_name"])):
            d = pairs[lt].setdefault((co, ref), {"name": nm, "invoices": set(), "mapped": (co, lt, ref) in keys})
            d["invoices"].add(x["invoice_id"])
            if not d["mapped"]:
                inv_ok[x["invoice_id"]] = False
        if (co, "GODOWN", 0) not in keys:
            inv_ok[x["invoice_id"]] = False
    print(f"\n== Coverage: approved raw-jute sales invoices dated >= {since} "
          f"({len(inv_co)} invoices, companies {sorted(set(inv_co.values()))}) ==")
    for lt in ("PARTY", "ITEM"):
        tot = len(pairs[lt])
        ok = sum(1 for d in pairs[lt].values() if d["mapped"])
        print(f"  (co, {lt.lower()}) pairs mapped: {ok}/{tot} = {100 * ok / tot:.1f}%" if tot else f"  no {lt} pairs")
    full = sum(1 for i in inv_co if inv_ok[i])
    print(f"  invoices with party + every item + godown mapped: {full}/{len(inv_co)}"
          f" = {100 * full / len(inv_co):.1f}%" if inv_co else "  no invoices")
    print("  per selling company:")
    for co in sorted(set(inv_co.values())):
        have = {lt: sum(1 for r in rows if r["status"] == "PROPOSED" and r["co_id"] == co and r["link_type"] == lt)
                for lt in ("PARTY", "ITEM", "GODOWN")}
        n_inv = sum(1 for c in inv_co.values() if c == co)
        pm = [d["mapped"] for (c, _), d in pairs["PARTY"].items() if c == co]
        im = [d["mapped"] for (c, _), d in pairs["ITEM"].items() if c == co]
        print(f"    co {co:<4} invoices {n_inv:>3}  party {sum(pm)}/{len(pm)}  item {sum(im)}/{len(im)}"
              f"  seeded rows PARTY {have['PARTY']} ITEM {have['ITEM']} GODOWN {have['GODOWN']}"
              + ("   <-- NO GODOWN" if not have["GODOWN"] else "")
              + ("   <-- NO ITEM rows" if not have["ITEM"] else ""))
    gaps = [{"link_type": lt, "co_id": co, "ref_id": ref, "name": d["name"], "invoices": len(d["invoices"])}
            for lt in ("PARTY", "ITEM") for (co, ref), d in sorted(pairs[lt].items()) if not d["mapped"]]
    gaps += [{"link_type": "GODOWN", "co_id": co, "ref_id": 0, "name": "(company godown)",
              "invoices": sum(1 for c in inv_co.values() if c == co)}
             for co in sorted(set(inv_co.values())) if (co, "GODOWN", 0) not in keys]
    write_csv(gaps_path, ["link_type", "co_id", "ref_id", "name", "invoices"], gaps)
    print(f"  {len(gaps)} unmapped (co, ref) pairs -> {gaps_path}")


# --------------------------------------------------------------------------- apply
def apply(conn, cur, rows, out):
    prop = [r for r in rows if r["status"] == "PROPOSED"]
    before = {(r["co_id"], r["link_type"], r["ref_id"]): r for r in q(
        cur, f"SELECT tally_link_id, co_id, link_type, ref_id, tally_name, active FROM {TARGET} "
             "WHERE co_id IN %s", (tuple({r["co_id"] for r in prop}) or (0,),))}
    cur.executemany(
        f"INSERT INTO {TARGET} (co_id, link_type, ref_id, tally_name, active) VALUES (%s,%s,%s,%s,1) "
        "ON DUPLICATE KEY UPDATE tally_name = VALUES(tally_name), active = 1",
        [(r["co_id"], r["link_type"], r["ref_id"], r["tally_name"]) for r in prop])
    after = {(r["co_id"], r["link_type"], r["ref_id"]): r["tally_link_id"] for r in q(
        cur, f"SELECT tally_link_id, co_id, link_type, ref_id FROM {TARGET} WHERE co_id IN %s",
        (tuple({r["co_id"] for r in prop}) or (0,),))}
    applied, rb = [], []
    for r in prop:
        k = (r["co_id"], r["link_type"], r["ref_id"])
        old = before.get(k)
        tid = after[k]
        applied.append({"apply_action": "UPDATED" if old else "INSERTED", "tally_link_id": tid, "co_id": k[0],
                        "link_type": k[1], "ref_id": k[2], "tally_name": r["tally_name"],
                        "prev_tally_name": old["tally_name"] if old else "",
                        "prev_active": old["active"] if old else ""})
        if old:
            rb.append("UPDATE %s SET tally_name = %s, active = %d WHERE tally_link_id = %d;" % (
                TARGET, conn.escape(old["tally_name"]), old["active"], tid))
    ins = [str(a["tally_link_id"]) for a in applied if a["apply_action"] == "INSERTED"]
    stem = out.with_suffix("")
    write_csv(f"{stem}_applied.csv", list(applied[0]) if applied else ["apply_action"], applied)
    Path(f"{stem}_rollback.sql").write_text(
        (f"DELETE FROM {TARGET} WHERE tally_link_id IN ({','.join(ins)});\n" if ins else "") + "\n".join(rb) + "\n",
        encoding="utf-8")
    conn.commit()
    print(f"\nCOMMITTED: {len(ins)} inserted, {len(applied) - len(ins)} updated in {TARGET}; "
          f"rollback -> {stem}_rollback.sql")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("tenant_db", nargs="?")
    ap.add_argument("--apply", action="store_true", help="write to tally_link_mst (default: dry run)")
    ap.add_argument("--out", help="CSV path (default: system temp dir)")
    ap.add_argument("--since", default="2026-04-01", help="coverage start date; '' to skip")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.tenant_db or not re.fullmatch(r"[A-Za-z0-9_]+", a.tenant_db):
        ap.error("tenant_db is required (letters, digits, underscore)")
    out = Path(a.out) if a.out else Path(tempfile.gettempdir()) / f"tally_link_seed_{a.tenant_db}.csv"
    env = dotenv_values(REPO / "env" / "database.env")
    conn = connect(env, a.tenant_db, read_only=not a.apply)
    cur = conn.cursor()
    exists = bool(q(cur, "SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() "
                         "AND table_name = %s", (TARGET,)))
    if a.apply and not exists:
        sys.exit(f"REFUSED: {a.tenant_db}.{TARGET} does not exist. Run create_tally_link_mst.sql first.")
    print(f"tenant={a.tenant_db}  mode={'APPLY' if a.apply else 'DRY RUN (READ ONLY session)'}  "
          f"{TARGET} {'exists' if exists else 'MISSING (--apply would refuse)'}")
    rows, cos = seed(cur)
    write_csv(out, CSV_COLS, rows)
    print_summary(rows)
    print_unmatched(rows)
    if exists:
        have = {(r["co_id"], r["link_type"], r["ref_id"]) for r in q(
            cur, f"SELECT co_id, link_type, ref_id FROM {TARGET}")}
        prop = [r for r in rows if r["status"] == "PROPOSED"]
        print(f"\n{TARGET} already holds {len(have)} rows; this run would UPDATE "
              f"{sum((r['co_id'], r['link_type'], r['ref_id']) in have for r in prop)} and INSERT the rest.")
    print(f"\nCSV (every proposed + unmatched row): {out}")
    if a.since:
        coverage(cur, rows, a.since, f"{out.with_suffix('')}_gaps.csv")
    if a.apply:
        try:
            apply(conn, cur, rows, out)
        except Exception:
            conn.rollback()
            print("ROLLED BACK")
            raise
    conn.close()


if __name__ == "__main__":
    main()
