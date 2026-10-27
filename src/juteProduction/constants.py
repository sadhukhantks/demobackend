"""Constants for the Jute Production (spreader) module."""

# Spreader machine type lookup — resolved against machine_type_mst.machine_type_name at runtime.
SPREADER_MACHINE_TYPE_NAME = "Spreader"

# Drawing machine type lookup — resolved against machine_type_mst.machine_type_name at runtime.
DRAWING_MACHINE_TYPE_NAME = "Drawing"

# Drawing efficiency base: const_meter is defined as meters per 8-hour shift at 100% eff.
EFF_BASE_HOURS = 8

# Default odometer wrap for drawing meter counters (legacy +10000 rollover).
DEFAULT_METER_WRAP = 10000

# item_grp_mst.item_type_id values that classify an item as jute or jute-waste
# (i.e. eligible to be produced on a spreader).
JUTE_ITEM_TYPE_IDS = (2, 3)

# Spell / shift codes (must match values stored in spell_mst).
SPELLS = ("A1", "A2", "B1", "B2", "C")
SHIFT_BUCKETS = ("A1", "A2", "B1", "B2", "C")
A_SHIFT = ("A1", "A2")
B_SHIFT = ("B1", "B2")

# 4-hour reuse window for entry_id_grp within a bin.
GROUP_WINDOW_HOURS = 4

# Default maturity (hrs) when no item_maturity_mst row exists for an item.
DEFAULT_MATURITY_HOURS = 48

# Roll Stock Reports > Stock As At: target vs actual (TKT-000170). A quality
# whose actual roll stock is within +/- this % of the Issue Planner target is
# "on target"; below is "short", above is "excess".
ROLL_STOCK_TARGET_TOLERANCE_PCT = 10.0

# --- Spinning / Doff production ---------------------------------------------

# Spinning machine type lookup — resolved against machine_type_mst.machine_type_name
# at runtime (legacy type_of_mechine=36).
SPINNING_MACHINE_TYPE_NAME = "Spinning"

# Default working hours per shift used in the spinning daily roll-up.
SPIN_HRS_A, SPIN_HRS_B, SPIN_HRS_C = 8.0, 8.0, 7.5

# quality_type stamped on jute_prod_spinning_act_count rows.
SPIN_QUALITY_TYPE = 2

# Valid net-weight band (kg) for a single doff entry.
DOFF_NET_MIN, DOFF_NET_MAX = 5.0, 60.0

# Shift buckets for production roll-up: A=A1/A2, B=B1/B2, C=C
# (reuse A_SHIFT / B_SHIFT already defined above).
C_SHIFT = ("C",)

# --- Spinning planning refactor (time-versioned standards/targets) ----------

# 100% production formula constants (per-frame, per-spell):
#   p100prod = (std_speed * minutes * act_count * spindles)
#              / (SPNG_PROD_C1 * SPNG_PROD_C2 * SPNG_PROD_C3 * std_tpi)
# C3 kept at 2.2046 (matches existing p100_x; doc says 2.204 — negligible).
SPNG_PROD_C1 = 36
SPNG_PROD_C2 = 14400
SPNG_PROD_C3 = 2.2046

# Fallback shift minutes when spell_mst.working_hours is unavailable.
SPELL_MINUTES = {"A1": 300, "A2": 180}

# jute_prod_spng_target_map.id_type discriminator values.
ID_TYPE_MC = "mcid"
ID_TYPE_QLTY = "qid"

# jute_prod_spng_target_map.value_role discriminator values.
VALUE_ROLE_STANDARD = "standard"
VALUE_ROLE_TARGET = "target"
VALUE_ROLE_ACTUAL = "actual"

# jute_prod_spng_target_map.param discriminator values.
# Quality (qid) params: tpi, eff. Machine (mcid) params: speed, spindles, dc, tc, bobbin_wt.
PARAM_SPEED = "speed"
PARAM_TPI = "tpi"
PARAM_EFF = "eff"
PARAM_SPINDLES = "spindles"
PARAM_DC = "dc"
PARAM_TC = "tc"
PARAM_BOBBIN_WT = "bobbin_wt"

# --- Winding production -----------------------------------------------------

# Winding machine type lookup — resolved against machine_type_mst.machine_type_name
# at runtime (legacy type_of_mechine=39, dept_id=53).
WINDING_MACHINE_TYPE_NAME = "Winding"

# Valid per-machine net-weight band (kg) for a winding doff entry.
# Legacy view gate: 1 <= mc1netwt <= 500 (winding_doff_data.php).
WINDING_NET_MIN, WINDING_NET_MAX = 1, 500

# Jugar (spindle leftover) weight band (kg): 0 < weight <= 100.
# Legacy validation: 0 < jugarwt <= 100 (winding_jugar_entry.php).
JUGAR_MIN, JUGAR_MAX = 0, 100

# Spindle-count band per machine/spell on the winding quality entry.
# Legacy view gate is 1..30 (alert text says "1 to 16" — design resolves to 1..30).
SPINDLE_MIN, SPINDLE_MAX = 1, 30

# kg -> bundle divisor (14 kg/bundle). Kept as a constant only; reconciled
# production stays in KG (no per-quality UOM applied) per the locked design.
BUNDLE_KG = 14

# --- Beaming ----------------------------------------------------------------

# Beaming machine type — resolved against machine_type_mst.machine_type_name.
# APPLIED in dev3: machine_type_id 12 (§0.1).
BEAMING_MACHINE_TYPE_NAME = "Beaming"

# Jute-cloth item type for the Beaming item dropdown.
# APPLIED in dev3: item_type_master 'Jute Cloth' = 5 (§0.1); groups 625/626/640/642/1266/1716.
BEAMING_ITEM_TYPE_IDS = (5,)

# kg/cut formula constants (own values to avoid the 2.2046 vs 2.20462 drift).
# NB: "spyndle" here is the jute COUNT unit (14400 yd per spyndle, per the screenshot's
# "spnydle (constant) 14400") — NOT machine spindles. Beaming's grain is machine+quality (no spindles).
BEAMING_SPYNDLE_YDS = 14400       # jute spyndle = 14400 yards (count-system constant)
BEAMING_KG_TO_LB    = 2.20462     # kg -> lb (count-system conversion)

# Target-map discriminators. Beaming is now TWO-DIMENSIONAL (mcid + qid), mirroring
# spinning: machine-linked physical params live under id_type='mcid' (ref_id=machine_id),
# while quality-linked production params live under id_type='qid'
# (ref_id=jute_prod_bm_quality.bm_quality_id). This re-adds the 'qid' dimension the
# beaming target-map was originally stripped of.
BEAMING_ID_TYPE_MC      = "mcid"
BEAMING_ID_TYPE_QLTY    = "qid"
BEAMING_VALUE_ROLES     = ("standard", "target", "actual")

# --- MACHINE-linked (mcid, ref_id = machine_id) -----------------------------
# NB: the 'speed' param is RPM (rotations/min), NOT surface speed. Beaming surface
# speed (yd/min) = rpm × dia × π / 36 — use beaming_rules.act_speed(rpm, dia) for the
# std/target/act surface speeds. p100prod capacity uses the STD SURFACE speed.
# 'dia' = starch-roller diameter (machine-linked standard, fixed).
# 'meter_wrap' = reading at which the machine's yard counter rolls over to 0 (per
# machine, like Drawing's meter_wrap_limit). Unset ⇒ a closing below the opening is
# refused instead of being read as a wrap.
BEAMING_MC_PARAMS_STD      = ("speed", "dia", "meter_wrap")
BEAMING_MC_PARAMS_TARGET   = ("speed",)
# 'speed' = actual RPM captured on the Beaming SQC page; value_role=actual (mcid).
BEAMING_PARAMS_ACTUAL      = ("speed",)

# --- QUALITY-linked (qid, ref_id = bm_quality_id) ---------------------------
# laid_length / cuts_per_beam / eff are properties of the beaming QUALITY (the warp
# being beamed), NOT the machine, so they key off the Beaming Quality Master.
BEAMING_QID_PARAMS_STD     = ("laid_length", "cuts_per_beam", "eff")
BEAMING_QID_PARAMS_TARGET  = ("eff",)

# --- Weaving ----------------------------------------------------------------

# Loom machine type — resolved against machine_type_mst.machine_type_name at runtime
# (case-insensitive under MySQL's default collation, so 'Loom' = 'LOOM').
# dev3 REALITY (verified 2026-06-23): machine_type 'LOOM' = machine_type_id 6 (4 looms);
# SPEC Q1 said id 7 (from the code3i type_of_mechine=7 source DB) — that id does NOT
# exist in dev3. Resolution is by NAME, so the id is informational only.
WEAVING_MACHINE_TYPE_NAME = "Loom"
WEAVING_MACHINE_TYPE_ID = 6  # dev3 'LOOM' (SPEC said 7 from code3i; dev3 reality is 6)

# Woven jute-cloth item type for the Weaving quality item dropdown (reuse beaming's, Q2).
WEAVING_ITEM_TYPE_IDS = (5,)  # 'Jute Cloth'

# oz -> kg constant (Q13 RESOLVED): production_kg = production_yds * ozs_yds * 28.35 / 1000
# (28.35 g/oz; effective divisor 35.273). Confirmed by both authoritative loom calculators
# + code3i. Do NOT use the old 35.2 placeholder or the 4408/125=35.264 target variant.
WEAVING_GRAMS_PER_OZ = 28.35
# Jugar chain gap cap (2026-09-16): a loom whose previous entry (same quality) is
# older than this many days opens at 0 - the counter state after such an idle
# stretch is unknown, and it keeps the chain probes from scanning years of history.
WEAVING_CHAIN_MAX_GAP_DAYS = 45
# /weavingProd/sync_range is an ops endpoint (no FE caller): one synchronous request,
# one commit per unit, measured ~1-2 s per unit with the attendance indexes. 7 days
# of a 3-shift branch is ~21 units, inside a 60 s gateway timeout; call it per week.
# A timeout loses nothing (per-unit commits) - re-run the same range.
WEAVING_SYNC_RANGE_MAX_DAYS = 7
WEAVING_OZ_TO_KG = 35.273  # = 1000 / 28.35 (reference/inverse only)
WEAVING_OZ_PER_LB = 16  # cut-weight (lbs) reference: finished_length * ozs_yds / 16
WEAVING_YARD_FACTOR = 36  # picks -> standard-yards conversion (std_prod_yds denominator)

# Target-map discriminators. Weaving is now TWO-DIMENSIONAL (mcid + qid), mirroring
# beaming/spinning: machine-linked physical params (speed) live under id_type='mcid'
# (ref_id = machine_id, a LOOM), while quality-linked production params (picks/eff)
# live under id_type='qid' (ref_id = jute_prod_weaving_quality.weaving_quality_id).
WEAVING_ID_TYPE_MC   = "mcid"
WEAVING_ID_TYPE_QLTY = "qid"
WEAVING_VALUE_ROLES = ("standard", "target", "actual")

# --- MACHINE-linked (mcid, ref_id = machine_id, a LOOM) ---------------------
# Loom refs come from get_weaving_entry_machines_query() bound :loom_type =
# WEAVING_MACHINE_TYPE_NAME. 'speed' is the loom speed; the actual loom speed is
# captured on the Weaving SQC "Actual Speed" tab (value_role=actual, mcid).
WEAVING_MC_PARAMS_STD    = ("speed",)
WEAVING_MC_PARAMS_TARGET = ("speed",)
WEAVING_MC_PARAMS_ACTUAL = ("speed",)

# --- QUALITY-linked (qid, ref_id = weaving_quality_id) ----------------------
# picks / eff are properties of the weaving QUALITY (the cloth being woven), NOT the
# loom, so they key off the Weaving Quality Master. There is NO qid actual param:
# actual picks are owned by vw_weaving_pick_act (Weaving Pick-SQC page) and actual
# speed is the mcid dimension above.
WEAVING_QID_PARAMS_STD    = ("picks", "eff")
WEAVING_QID_PARAMS_TARGET = ("eff",)

# Weaving spells in carry-forward order A1 -> B1 -> A2 -> B2 -> C (then day boundary).
# (SPELLS tuple above is ('A1','A2','B1','B2','C'); this is the jugar roll-forward order.)
WEAVING_SPELL_ORDER = ("A1", "B1", "A2", "B2", "C")

# --- Stoppage Hours ---------------------------------------------------------

# Fixed stoppage reasons (NOT a master table). reason_code stored on the row.
STOPPAGE_REASONS = ("mechanical", "electrical", "labor", "other")

# Optional display labels for the FE dropdown.
STOPPAGE_REASON_LABELS = {
    "mechanical": "Mechanical",
    "electrical": "Electrical",
    "labor": "Labor",
    "other": "Other",
}

# --- Finishing --------------------------------------------------------------
#
# The Finishing department is the post-weaving line that turns grey cloth into
# hessian cloth (rolls) and jute bags (bales) across NINE sub-processes. It reuses
# the Beaming/Spinning target-map EAV contract (id_type 'mcid'|'qid', value_role
# 'standard'|'target'|'actual') and adds ONE new dimension: `process`.
#
# Machine LINKING is intentionally unused: the per-process machine types exist for
# the future only, so id_type 'mcid' carries NO params for any process. SQC is
# dropped for now, so value_role 'actual' is EMPTY for every process — only a small
# set of qid 'standard'/'target' params remain (see FINISHING_PARAMS).

# Process tokens (= jute_prod_finishing_*.process column values). LOCKED CONTRACT —
# the FE and the spec sheet both key off these EXACT tokens.
FINISHING_PROCESSES = ("damping", "calendering", "lapping", "rolling", "cutting", "hemming", "herackle", "sacksewing", "balepress")

# id_type discriminators: 'mcid' (ref_id = machine_id) | 'qid' (ref_id = finishing_quality_id).
FINISHING_ID_TYPE_MC = "mcid"
FINISHING_ID_TYPE_QLTY = "qid"

# value_role discriminators. 'actual' is captured on the Finishing SQC page.
FINISHING_VALUE_ROLES = ("standard", "target", "actual")

# finishing_quality.quality_type values.
FINISHING_QUALITY_TYPE_CLOTH = 1   # Hessian qualities (Damping/Calendering/Lapping/Rolling)
FINISHING_QUALITY_TYPE_BAG = 2     # Sacking qualities (Cutting/Hemming/Herackle/Sack Sewing)

# Item-picker filter for the Finishing Quality Master: only 'Jute Cloth' items
# (item_grp_mst.item_type_id = 5), matching Beaming/Weaving (BEAMING/WEAVING_ITEM_TYPE_IDS).
FINISHING_ITEM_TYPE_ID = 5

# Quality applicability per process — filters qid (finishing_quality) refs by quality_type.
# Hessian-only processes (quality_type 1): damping/calendering/lapping/rolling.
# Sacking-only processes (quality_type 2): cutting/hemming/herackle/sacksewing.
# balepress is in NEITHER tuple, so quality_type_for_process('balepress') -> None -> BOTH
# qualities apply (no filter) via the existing code path.
FINISHING_CLOTH_PROCESSES = ("damping", "calendering", "lapping", "rolling")
FINISHING_BAG_PROCESSES = ("cutting", "hemming", "herackle", "sacksewing")

# Processes that accept a machine and/or a worker on the SAME entry (at least one).
# Machine-only, EB-only and machine+EB rows all coexist — the row key is the NULL-safe
# (machine_id, eb_id) PAIR, so a machine's unattributed total and a named worker's
# output on that machine are two separate rows, never a silent merge.
MACHINE_OR_LABOUR_PROCESSES = ("hemming", "herackle")

# Attendance designation gate for /entry_sync_eb, per process. Matched as a PREFIX
# against LOWER(designation_mst.desig) (weaving's WEAVER_DESIG_PREFIX pattern).
# NOTE the spelling: the process token is 'herackle' but the shop floor / designation
# vocabulary is HERACLE (no k) — verified on sls. Never derive one from the other.
FINISHING_SYNC_DESIG_PREFIX = {"hemming": "hemming%", "herackle": "heracle%"}

# Per-process applicable params — the single source of truth for grid_params_for()
# (SPEC §5.1). The grid renders exactly the params returned for a (process, id_type,
# value_role) combination; adding/removing a param is a one-line change here.
# Machine ('mcid') is unused -> NO mcid key for any process. SQC is dropped -> every
# 'actual' role is EMPTY. Each process's qid params are available under BOTH 'standard'
# and 'target' (the user sets a standard value and, optionally, a target value per param).
FINISHING_PARAMS = {
    "damping": {},
    "calendering": {},
    "lapping": {
        "qid": {"standard": ("std_prod_yds",), "target": ("std_prod_yds",), "actual": ()},
    },
    "rolling": {},
    "cutting": {
        "qid": {"standard": ("target_pcs",), "target": ("target_pcs",), "actual": ()},
    },
    "hemming": {
        "qid": {"standard": ("target_pcs",), "target": ("target_pcs",), "actual": ()},
    },
    "herackle": {
        "qid": {"standard": ("target_pcs",), "target": ("target_pcs",), "actual": ()},
    },
    "sacksewing": {
        "qid": {"standard": ("pcs_per_bundle", "bundles"), "target": ("pcs_per_bundle", "bundles"), "actual": ()},
    },
    "balepress": {
        "qid": {"standard": ("no_of_bales",), "target": ("no_of_bales",), "actual": ()},
    },
}

# Per-process machine-type name — resolved against machine_type_mst.machine_type_name
# at runtime (created by create_finishing_machine_types.sql), exactly like Beaming.
# Present for the future only; machine LINKING is intentionally unused (no mcid params).
# Values are NAME TUPLES (union of tenant vocabularies — dev3 'Rolling' vs sls
# 'Rolling Machine'; ci collation handles case) bound via the expanding
# :machine_types of get_finishing_machines_query.
FINISHING_MACHINE_TYPE_NAMES = {
    "damping": ("Damping",),
    "calendering": ("Calendering",),
    "lapping": ("Lapping",),
    "rolling": ("Rolling", "Rolling Machine"),
    "cutting": ("Cutting",),
    "hemming": ("Hemming",),
    "herackle": ("Herackle",),
    "sacksewing": ("Sack Sewing",),
    "balepress": ("Bale Press",),
}

# Cloth production weight (F1) constant: total_yards × oz_per_yd / 35.2, where
# 35.2 = 16 oz/lb × 2.2046 lb/kg (carried verbatim from legacy Weaving/Beaming).
FINISHING_OZ_PER_KG = 35.2            # 16 × 2.2046
FINISHING_M_TO_YD = 1.09361           # metres -> yards (F1)
FINISHING_M_TO_IN = 39.3701           # metres -> inches (F5 cutting yield)

# --- Bale Production (Bale Press) -------------------------------------------
#
# One row per bale weighed at the bale press (jute_prod_bale_entry) plus a small
# running-counter master (jute_prod_bale_series). See
# docs/bale-production-spec-sheet.md §4 / §5 / §6 and decisions D1–D4.

# jute_prod_bale_entry.entry_type: PROD = fresh press output (the sheet's BALE tab),
# REPACK = an already-counted bale opened and re-pressed (the REPACK tab). REPACK rows
# take the next series number as their own bale_no and carry original_bale_no (D2);
# they are excluded from production counts / HY-LT (Q4).
BALE_ENTRY_TYPES = ("PROD", "REPACK")
BALE_ENTRY_TYPE_PROD = "PROD"
BALE_ENTRY_TYPE_REPACK = "REPACK"

# Scale-reading band (kg) for a single bale — the sheet runs 280–453 kg; the bounds
# only catch a slipped keystroke. net_wt = gross − trolly − packsheet must stay > 0 (F0).
BALE_GROSS_MIN, BALE_GROSS_MAX = 1, 2000

# Ceiling for bale_no / original_bale_no / last_bale_no (INT column, 7 digits).
BALE_NO_MAX = 9_999_999

# Live-preview soft band: deviation of net vs std_bale_weight beyond ±this % is tinted
# on the FE; it never blocks a save (§8.1). Mirrored in utils/baleCalc.ts.
BALE_DEVIATION_WARN_PCT = 3.0

# The finishing process token whose jute_prod_finishing_daily row the bale saves
# maintain (F10 / D4): prod_qty = active PROD bales, prod_uom = 'bale', prod_wt_kg = Σ net.
BALE_PRESS_PROCESS = "balepress"
BALE_PRESS_PROD_UOM = "bale"

# --- Production Planning ----------------------------------------------------
#
# The seven material-chain stages of the daily production plan, in FLOW order
# (stage_seq 1..7). The backward explosion cascades requirement from WEAVING up to
# BATCHING: each stage's input_kg_day = output_kg_day × (1 + waste_pct/100), and the
# upstream stage's output_kg_day defaults to the downstream stage's input_kg_day.
# BATCHING's input_kg_day is the headline raw-jute-per-day figure.
#
# Tuple shape: (stage_seq, stage_code, label, machine_type_name).
# machine_type_name resolves against machine_type_mst.machine_type_name by NAME,
# case-insensitively, exactly like every other stage. A type that does not exist in a
# tenant (e.g. 'Carding' is absent in dev3) simply yields machines_installed = 0 —
# that is expected, not an error. WINDING's "machines" are winders (winding is
# person-keyed in this ERP), so the FE labels that row "Machines / Workers".
PLANNING_STAGES = (
    (1, "BATCHING", "Batching (Spreader)", "Spreader"),
    (2, "CARDING", "Carding", "Carding"),
    (3, "DRAWING", "Drawing", "Drawing"),
    (4, "SPINNING", "Spinning", "Spinning"),
    (5, "WINDING", "Winding", "Winding"),
    (6, "BEAMING", "Beaming", "Beaming"),
    (7, "WEAVING", "Weaving", "Loom"),
)

# jute_prod_planning_norm.id_type discriminators.
PLANNING_ID_TYPE_QLTY = "qid"      # ref_id = cloth quality item_id (item_type_id=5)
PLANNING_ID_TYPE_YARN = "yid"      # ref_id = yarn item_id (item_type_id=4)
PLANNING_ID_TYPE_STAGE = "stage"   # ref_id = stage_seq (1..7)

# jute_prod_planning_norm.value_role — fixed 'standard' in v1 (shape parity with the
# *_target_map tables, which carry standard/target/actual).
PLANNING_VALUE_ROLE = "standard"

# Applicable norm params per id_type — the single source of truth for norms_grid
# columns and for norm-entry validation in planning.py (SPEC §2.5).
PLANNING_NORM_PARAMS = {
    PLANNING_ID_TYPE_QLTY: (
        "kg_per_loom_100",   # per loom per shift @100% eff
        "warp_pct",
        "weft_pct",
        "warp_yarn_item",    # value = yarn item_mst.item_id
        "weft_yarn_item",    # value = yarn item_mst.item_id
        "std_eff",
    ),
    PLANNING_ID_TYPE_YARN: (
        "kg_per_frame_100",  # per frame per shift @100% eff
        "std_eff",
    ),
    PLANNING_ID_TYPE_STAGE: (
        "kg_per_machine",    # per machine per shift
        "waste_pct",
    ),
}

# item_grp_mst.item_type_id for the planning pickers: cloth qualities (Jute Cloth = 5,
# same as Beaming/Weaving/Finishing) and yarns (= 4, same as spinning).
PLANNING_CLOTH_ITEM_TYPE_ID = 5
PLANNING_YARN_ITEM_TYPE_ID = 4

# Product groups on a loom plan line, and section tokens on a yarn line.
PLANNING_PRODUCT_GROUPS = ("SACKING", "HESSIAN", "PACKSHEET", "OTHER")
PLANNING_YARN_LINE_KINDS = ("CLOTH", "SALE")
PLANNING_YARN_SECTIONS = ("SACKING", "HESSIAN", "PACKSHEET", "TWINE", "SALE", "OTHER")

# Default status for a saved plan — no approval workflow in v1 (1 = Open).
PLANNING_STATUS_OPEN = 1
# --- Waste Entry ------------------------------------------------------------
#
# Waste type IS an item: jute_prod_waste_entry.item_id points at item_mst, and the
# picker/validation are restricted to items whose group carries this item type
# (item_type_master row 3 = 'jute wastage'). Company scope rides on
# item_grp_mst.co_id (item_mst has no co_id).
JUTE_WASTAGE_ITEM_TYPE_ID = 3

# Column width guard: waste_qty is DECIMAL(10,3) — the API returns 400 rather
# than letting MySQL truncate/error on an over-wide value (SPEC §2).
WASTE_QTY_MAX = 99999.999

# =============================================================================
# Winding stock (daily counted snapshot) — see
# docs/superpowers/specs/2026-08-19-winding-stock-difference-design.md
# =============================================================================

WINDING_STOCK_BUCKET_MILL_USE = "MILL_USE"
WINDING_STOCK_BUCKET_PURCHASE = "PURCHASE"
WINDING_STOCK_BUCKET_SALE_YARN = "SALE_YARN"
WINDING_STOCK_BUCKET_BALL = "BALL"
WINDING_STOCK_BUCKET_WASTE = "WASTE"

# Report print order. PURCHASE here is COUNTED purchased yarn sitting in stock
# (the sheet's "OUT SIDE PURCHASE MACKROL FOR S4"), NOT the purchase-yarn inflow
# figure in the production block — those are unrelated numbers sharing a word.
WINDING_STOCK_BUCKETS = (
    WINDING_STOCK_BUCKET_MILL_USE,
    WINDING_STOCK_BUCKET_PURCHASE,
    WINDING_STOCK_BUCKET_SALE_YARN,
    WINDING_STOCK_BUCKET_BALL,
    WINDING_STOCK_BUCKET_WASTE,
)

WINDING_STOCK_MENU_PATH = "juteProduction/windingStock"

# Where a counted line physically sits (jute_prod_winding_stock.stock_loc,
# added 2026-09-25 for the purchased-yarn ledger). GODOWN lines check the
# yarn purchase book; FLOOR lines (per dept_id) give the derived consumption.
# The daily total the difference report reads sums BOTH locations.
WINDING_STOCK_LOC_GODOWN = "GODOWN"
WINDING_STOCK_LOC_FLOOR = "FLOOR"
WINDING_STOCK_LOCATIONS = (WINDING_STOCK_LOC_GODOWN, WINDING_STOCK_LOC_FLOOR)

# -----------------------------------------------------------------------------
# Purchased Yarn Ledger (phase 5 of the 2026-09-25 purchased-yarn design, §3.5)
# — per yarn item and month: godown book vs godown count, floor counts per
# department and the CONSUMPTION derived from them (owner rulings 3 and 4).
# -----------------------------------------------------------------------------

# Godown count vs book: flagged when |count - book| exceeds the LARGER of a
# fixed floor and a share of what passed through the godown (opening +
# purchases). 50 kg is one or two spools; 1 % keeps a 60-tonne month from
# tripping on rounding of package weights.
YARN_LEDGER_GODOWN_TOL_KG = 50.0
YARN_LEDGER_GODOWN_TOL_PCT = 1.0

# Floor stock above this many days of average consumption is flagged as yarn
# lying on a department floor rather than being used.
YARN_LEDGER_FLOOR_DAYS_MAX = 15.0

# Exception codes the ledger response carries (each with a plain message).
YARN_LEDGER_EXC_GODOWN_BOOK_NEGATIVE = "GODOWN_BOOK_NEGATIVE"
YARN_LEDGER_EXC_GODOWN_DIFF = "GODOWN_DIFF"
YARN_LEDGER_EXC_DEPT_NO_FLOOR_COUNT = "DEPT_NO_FLOOR_COUNT"
YARN_LEDGER_EXC_FLOOR_DAYS_OVER = "FLOOR_DAYS_OVER"
YARN_LEDGER_EXC_LEGACY_STORES_RECEIPTS = "LEGACY_STORES_RECEIPTS"
YARN_LEDGER_EXC_ITEM_WITHOUT_YARN_MASTER = "ITEM_WITHOUT_YARN_MASTER"
# Count-basis opening: the item has a document-book balance before the month
# but was not on the godown count the opening is taken from.
YARN_LEDGER_EXC_ITEM_IN_BOOK_NOT_COUNTED = "ITEM_IN_BOOK_NOT_COUNTED"

# Ledger opening basis: a godown count before the period, else the document book.
YARN_LEDGER_OPENING_COUNT = "count"
YARN_LEDGER_OPENING_BOOK = "book"

# jute_prod_finishing_quality.quality_type — the existing hessian/sacking split.
FINISHING_QUALITY_TYPE_HESSIAN = 1
FINISHING_QUALITY_TYPE_SACKING = 2

CLOTH_SPLIT_HESSIAN = "HESSIAN"
CLOTH_SPLIT_SACKING = "SACKING"
CLOTH_SPLIT_OTHER = "OTHER"

# Max kg per counted piece — a beam is the heaviest real package on the sheet
# at 420 kg; anything past this is a typo, not a package.
WINDING_STOCK_WT_MAX = 2000.0
# Max pieces on one counted line. The largest real line on the reference sheet
# is 1,563 spools; the ceiling only has to catch a slipped keystroke.
WINDING_STOCK_SAMPLES_MAX = 1000000.0

# =============================================================================
# Finishing stock (daily physical count of bales + loose WIP) — see
# docs/superpowers/specs/2026-09-17-finishing-stock-design.md
# =============================================================================

FINISHING_STOCK_BUCKET_BALE = "BALE"
FINISHING_STOCK_BUCKET_LOOSE = "LOOSE"
FINISHING_STOCK_BUCKETS = (FINISHING_STOCK_BUCKET_BALE, FINISHING_STOCK_BUCKET_LOOSE)
# (code, label) — ordered as the shop-floor sheet lists them
FINISHING_STOCK_STAGES = (
    ("IN_LINE", "Cuts in line (from weaving)"),
    ("S4_CUTS", "S4 cuts"),
    ("CAL_CUTS", "Calendered cuts"),
    ("CUTTING", "Cutting"),
    ("HEMMING", "Hemming"),
    ("HERACKLE", "Herackle"),
    ("BRANDING", "Branded pressing"),
    ("AP", "Branded AP"),
    ("BALE_ARRANGING", "Bale arranging"),
    ("OTHER", "Other"),
)
FINISHING_STOCK_STAGE_CODES = tuple(c for c, _ in FINISHING_STOCK_STAGES)
FINISHING_STOCK_QTY_UOM_BALE = "BALE"
FINISHING_STOCK_LOOSE_QTY_UOMS = ("CUTS", "BDL", "PCS")
FINISHING_STOCK_QTY_MAX = 999_999
FINISHING_STOCK_WT_MAX = 999_999          # kg, per line (loose total or per-bale)
FINISHING_STOCK_WORK_BY = (0, 1)          # 0 Dept, 1 Contract

# Purchased-yarn UOM handling. MTR is METRE and MT / MTS are METRIC TONNE —
# genuinely different units that look alike, and sls carries both labels on the
# same item. The report converts only what it recognises and reports the rest as
# an unknown-unit warning; silently dividing an unrecognised unit by 1000 is the
# bug this set exists to prevent.
PURCHASE_UOM_MT = ("MT", "MTS", "M.TON", "METRIC TON", "METRIC TONNE")
PURCHASE_UOM_KG = ("KG", "KGS", "KGM", "KILOGRAM")

# =============================================================================
# Jute Gain/Loss Sheet (/api/juteGainLoss)
# =============================================================================

# Statement codes: FNG = packed production, WVG = weaving production,
# WDG = winding-to-weaving difference. Quality-wise production is CAPTURED for
# FNG and WVG; WDG renders the WVG totals (the Excel does the same).
GL_STATEMENT_FNG = "FNG"
GL_STATEMENT_WVG = "WVG"
GL_STATEMENT_WDG = "WDG"
GL_STATEMENTS = (GL_STATEMENT_FNG, GL_STATEMENT_WVG, GL_STATEMENT_WDG)
GL_PROD_STATEMENTS = (GL_STATEMENT_FNG, GL_STATEMENT_WVG)

GL_STATEMENT_TITLES = {
    GL_STATEMENT_FNG: "JUTE GAIN / LOSS ON PACKED PRODUCTION",
    GL_STATEMENT_WVG: "JUTE GAIN / LOSS ON WEAVING PRODUCTION",
    GL_STATEMENT_WDG: "WINDING TO WEAVING PRODUCTION DIFFERENCE",
}

# The To-Date column runs from the start of the Indian financial year (April)
# to the selected month — computed by summing saved sheets, never hardcoded
# month literals like the Excel did.
GL_FY_START_MONTH = 4

# Sanity ceiling for any single MT figure on the sheet (monthly mill output is
# in the hundreds of MT; this only catches a slipped keystroke).
GL_QTY_ABS_MAX = 100000.0

# =============================================================================
# Wastage Report (/api/juteProductionReports/wastage_report)
# =============================================================================

# Percentage denominators a jute_waste_report_map row can name. day_denom and
# total_denom are separate because the mill's format divides daily rows and
# the monthly total row by different bases for some groups (user decision
# 2026-08-25: replicate as-is).
WASTE_REPORT_DENOM_WEAVING = "WEAVING"
WASTE_REPORT_DENOM_SPINNING = "SPINNING"
WASTE_REPORT_DENOM_BREAKER_FEED = "BREAKER_FEED"
WASTE_REPORT_DENOM_SELECTION = "SELECTION"
# TKT-000183 (owner 2026-10-05): LINE WASTE divides by winding production —
# reconciled winding kg (doff - opening jugar + closing jugar) of the day/month.
WASTE_REPORT_DENOM_WINDING = "WINDING"
WASTE_REPORT_DENOM_NONE = "NONE"
WASTE_REPORT_DENOMS = (
    WASTE_REPORT_DENOM_WEAVING,
    WASTE_REPORT_DENOM_SPINNING,
    WASTE_REPORT_DENOM_BREAKER_FEED,
    WASTE_REPORT_DENOM_SELECTION,
    WASTE_REPORT_DENOM_WINDING,
    WASTE_REPORT_DENOM_NONE,
)
# Human labels for the denominators — the mapping master's dropdown and the
# report's Total Wastage base picker both read these.
WASTE_REPORT_DENOM_LABELS = {
    WASTE_REPORT_DENOM_WEAVING: "Weaving production",
    WASTE_REPORT_DENOM_SPINNING: "Spinning production",
    WASTE_REPORT_DENOM_BREAKER_FEED: "Breaker feed",
    WASTE_REPORT_DENOM_SELECTION: "Jute issue (selection)",
    WASTE_REPORT_DENOM_WINDING: "Winding production",
    WASTE_REPORT_DENOM_NONE: "No %",
}

# Bucket for waste entries no mapping row claims — rendered last so nothing
# recorded on the entry page can silently vanish from the report.
WASTE_REPORT_OTHER_GROUP = "OTHER"

# =============================================================================
# Selection Trolley Weight (/api/selectionProd, report /selection_vs_issue) —
# ../vowerp3ui/docs/claude/modules/jute-production/selection-trolley-weight-spec.md
# =============================================================================

# Stage tag for the trolley picker (D1): trolly_mst.machine_type_id ->
# machine_type_mst.machine_type_name = 'Selection' (seeded idempotently by
# create_jute_prod_selection_trolly_wt.sql). Resolved by NAME like every other stage.
SELECTION_MACHINE_TYPE_NAME = "Selection"

# Quality / gate-entry picker window (D4 / D7): jute issued to the branch in the
# 7 days ending on the entry date (issue_date > tran_date - 7 AND issue_date <= tran_date).
SELECTION_QUALITY_WINDOW_DAYS = 7

# Scale-reading band (kg) for one loaded trolley — the bounds only catch a slipped
# keystroke. net_wt = gross_wt - trolly_wt (tare only, D2) must stay > 0.
SELECTION_GROSS_MIN, SELECTION_GROSS_MAX = 1, 5000

# =============================================================================
# Loom Modification register + impact report (/api/loomModification,
# report /loom_modification_impact) —
# docs/superpowers/specs/2026-09-26-loom-modification-impact-design.md
# =============================================================================

# Event kinds of one modification chain (§5): INITIAL is the modification itself,
# FOLLOWUP a later small change, RECTIFY a rectification of the previous stage,
# REVERSAL an undo (per loom).
MOD_KINDS = ("INITIAL", "FOLLOWUP", "RECTIFY", "REVERSAL")
MOD_KIND_LABELS = {
    "INITIAL": "Initial",
    "FOLLOWUP": "Follow-up",
    "RECTIFY": "Rectification",
    "REVERSAL": "Reversal",
}

# Fixed category list (D13); promote to a master only if it grows past ~10.
MOD_CATEGORIES = ("MECHANICAL", "ELECTRICAL", "SPEED_CHANGE", "ACCESSORY", "SETTING", "OTHER")
MOD_CATEGORY_LABELS = {
    "MECHANICAL": "Mechanical",
    "ELECTRICAL": "Electrical",
    "SPEED_CHANGE": "Speed change",
    "ACCESSORY": "Accessory",
    "SETTING": "Setting",
    "OTHER": "Other",
}

# Who did the work — same codes as juteMechanical/constants.py, re-declared here to
# avoid a cross-module import.
MOD_DONE_BY = ("INHOUSE", "VENDOR")
MOD_DONE_BY_LABELS = {"INHOUSE": "In-house", "VENDOR": "Vendor"}

# Loom group purpose: STUDY = looms under study, CONTROL = a saved control set (§4.1).
LOOM_GROUP_PURPOSES = ("STUDY", "CONTROL")

# Default settling-in days excluded after the work (D4).
MOD_DEFAULT_SETTLE_DAYS = 3

# Impact report thresholds (§7.3, D14) — published in meta.thresholds.
IMPACT_WINDOW_PRESETS = (30, 60, 90, 180)
IMPACT_DEFAULT_WINDOW_DAYS = 60
IMPACT_MAX_WINDOW_DAYS = 180
IMPACT_MAX_SPAN_DAYS = 200      # before + excluded + after, all stages (§8 cap)
IMPACT_MAX_STUDY_LOOMS = 100
IMPACT_MAX_LOOMS = 120          # study + control together (§8 cap)
IMPACT_DATA_HOLE_DAYS = 7       # >= 7 consecutive zero-row days for the branch = data hole
MIN_LOOM_DAYS_GROUP = 30
MIN_LOOM_DAYS_LOOM = 10
MIN_DAYS_PER_SIDE_GROUP = 10
MIN_DAYS_PER_SIDE_LOOM = 5
MIN_EFF_DAYS = 5
VERDICT_THRESHOLD_PCT = 3.0     # relative change (%) needed for IMPROVED / WORSE, per metric
SE_MULTIPLIER = 2.0             # |delta| must exceed SE_MULTIPLIER x SE of the day-level series
COVERAGE_MIN_PCT = 80.0         # coverage needed on both sides for a metric to be Auto headline
CONTROL_SPEED_BAND_PCT = 10.0   # per-study-loom control band = baseline std speed +/- 10 %
CONTROL_MIN_LOOMS = 5

# =============================================================================
# Bale Stock & Inspection (bale-level stock DERIVED from Bale Production,
# inspection marking, bale -> sales-invoice despatch link) — see
# docs/superpowers/specs/2026-10-12-bale-stock-inspection-design.md
# =============================================================================

# Derived status of one pressed bale as on a date (spec §3 CASE, in priority order).
BALE_STOCK_STATUS_IN_STOCK = "IN_STOCK"      # pressed, not inspected, not out
BALE_STOCK_STATUS_INSPECTED = "INSPECTED"    # in stock AND inspected
BALE_STOCK_STATUS_DESPATCHED = "DESPATCHED"  # linked to a live (not Rejected/Cancelled) invoice,
                                             # or despatched without an invoice (§7b)
BALE_STOCK_STATUS_REPACKED = "REPACKED"      # opened for a REPACK
BALE_STOCK_STATUSES = (
    BALE_STOCK_STATUS_IN_STOCK,
    BALE_STOCK_STATUS_INSPECTED,
    BALE_STOCK_STATUS_DESPATCHED,
    BALE_STOCK_STATUS_REPACKED,
)
BALE_STOCK_IN_STOCK_STATUSES = (BALE_STOCK_STATUS_IN_STOCK, BALE_STOCK_STATUS_INSPECTED)
# Grid filter codes: STOCK = both in-stock statuses; ALL = no filter.
BALE_STOCK_FILTERS = ("STOCK", "INSPECTED", "NOT_INSPECTED", "DESPATCHED", "REPACKED", "ALL")
# Rows returned by GET /bales before the response is flagged ``truncated``.
BALE_STOCK_ROW_CAP = 3000
# Invoice types a bale may be despatched against (keys of sales.constants.INVOICE_TYPE_IDS).
BALE_DESPATCH_INVOICE_TYPE_KEYS = ("REGULAR", "HESSIAN", "GOVT_SKG")
BALE_INSPECTION_REF_MAX = 100
BALE_INSPECTION_INSPECTOR_MAX = 100
# Where a branch's bale figures come from: BALE_PROD once any bale entry exists
# (derived, D7), MANUAL before that (Finishing Stock's counted BALE lines).
BALE_STOCK_SOURCE_BALE_PROD = "BALE_PROD"
BALE_STOCK_SOURCE_MANUAL = "MANUAL"
# Max bale ids one inspect / despatch / undo call may carry.
BALE_STOCK_MAX_IDS = 500
# GET /invoices default look-back window (days before as_on).
BALE_STOCK_INVOICE_DAYS_DEFAULT = 120
# Why a bale left WITHOUT a sales invoice (spec §7b, owner option 2 2026-10-12):
# jute_prod_bale_despatch.despatch_reason. Required by POST /despatch when
# invoice_id is NULL, optional (but validated) when an invoice is given. A
# no-invoice despatch is never void; "Link to invoice" later fills invoice_id in
# on the same row. (code, label) in the order the picker shows them.
BALE_DESPATCH_REASONS = (
    ("CHALLAN", "Delivery challan"),
    ("DELIVERY_ORDER", "Delivery order"),
    ("TRANSFER", "Transfer to another unit"),
    ("SAMPLE", "Sample"),
    ("OTHER", "Other"),
)
BALE_DESPATCH_REASON_CODES = tuple(code for code, _ in BALE_DESPATCH_REASONS)
BALE_DESPATCH_REASON_LABELS = dict(BALE_DESPATCH_REASONS)
# jute_prod_bale_despatch.despatch_ref (challan / delivery-order / gate-pass no) width.
BALE_DESPATCH_REF_MAX = 100


# Owner ruling 2026-10-12 (spec §7d): every date a user types in the bale /
# finishing-stock entries may be backdated up to this many days and no further
# (inspection, despatch, finishing-stock count, bale-press entry). Views are free.
BACKDATE_DAYS_MAX = 60
