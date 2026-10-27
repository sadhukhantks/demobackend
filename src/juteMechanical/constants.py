"""Constants for the Jute Mechanical & Maintenance module.

App-level enums only. Anything the mill maintains itself (reason codes, failure
components, PM checklist tasks) lives in a master TABLE, not here -- the mill's
reason list runs to ~60 codes and grows, so hardcoding it would be wrong.
"""

# --- Discipline -------------------------------------------------------------
# Which trade owns the fault. ELECTRICAL is a first-class discipline (not a
# mechanical reason code) so electrical downtime is separable in reports -- in the
# mill's existing workbook it hides inside "MT / Motor Trouble".
DISCIPLINE_MECHANICAL = "MECHANICAL"
DISCIPLINE_ELECTRICAL = "ELECTRICAL"
DISCIPLINE_UTILITY = "UTILITY"
DISCIPLINE_OPERATIONAL = "OPERATIONAL"

DISCIPLINES = (
    DISCIPLINE_MECHANICAL,
    DISCIPLINE_ELECTRICAL,
    DISCIPLINE_UTILITY,
    DISCIPLINE_OPERATIONAL,
)

DISCIPLINE_LABELS = {
    DISCIPLINE_MECHANICAL: "Mechanical",
    DISCIPLINE_ELECTRICAL: "Electrical",
    DISCIPLINE_UTILITY: "Utility / Services",
    DISCIPLINE_OPERATIONAL: "Operational",
}

# --- Downtime type ----------------------------------------------------------
# Separates unplanned machine failure from everything else. Only BREAKDOWN rows
# feed MTBF / MTTR / breakdown-count metrics; the rest are downtime but not
# failures, and mixing them would flatter or wreck the reliability numbers.
DOWNTIME_BREAKDOWN = "BREAKDOWN"
DOWNTIME_PLANNED_PM = "PLANNED_PM"
DOWNTIME_OVERHAUL = "OVERHAUL"
DOWNTIME_NO_MATERIAL = "NO_MATERIAL"
DOWNTIME_NO_POWER = "NO_POWER"
DOWNTIME_CHANGEOVER = "CHANGEOVER"

DOWNTIME_TYPES = (
    DOWNTIME_BREAKDOWN,
    DOWNTIME_PLANNED_PM,
    DOWNTIME_OVERHAUL,
    DOWNTIME_NO_MATERIAL,
    DOWNTIME_NO_POWER,
    DOWNTIME_CHANGEOVER,
)

DOWNTIME_TYPE_LABELS = {
    DOWNTIME_BREAKDOWN: "Breakdown",
    DOWNTIME_PLANNED_PM: "Planned Maintenance",
    DOWNTIME_OVERHAUL: "Overhaul",
    DOWNTIME_NO_MATERIAL: "No Material",
    DOWNTIME_NO_POWER: "No Power",
    DOWNTIME_CHANGEOVER: "Changeover",
}

# Only these count as failures for MTBF / MTTR / breakdown-frequency reporting.
FAILURE_DOWNTIME_TYPES = (DOWNTIME_BREAKDOWN,)

# --- Breakdown status -------------------------------------------------------
# There is no approval workflow on this module (breakdown logging must be fast),
# so `status` tracks the repair, not a document lifecycle.
STATUS_OPEN = "OPEN"              # machine stopped, nobody on it yet
STATUS_UNDER_REPAIR = "UNDER_REPAIR"  # fitter attending
STATUS_CLOSED = "CLOSED"          # machine restarted

BREAKDOWN_STATUSES = (STATUS_OPEN, STATUS_UNDER_REPAIR, STATUS_CLOSED)

BREAKDOWN_STATUS_LABELS = {
    STATUS_OPEN: "Open",
    STATUS_UNDER_REPAIR: "Under Repair",
    STATUS_CLOSED: "Closed",
}

# Production's optional reconfirmation of a CLOSED incident. NULL on the row means
# production has not looked at it yet.
PROD_CONFIRM_CONFIRMED = "CONFIRMED"  # production agrees with the fix as entered
PROD_CONFIRM_DISPUTED = "DISPUTED"    # production gave a remark / a different time

# --- Overhaul ---------------------------------------------------------------
OVERHAUL_MAJOR = "MAJOR"
OVERHAUL_MINOR = "MINOR"
OVERHAUL_TYPES = (OVERHAUL_MAJOR, OVERHAUL_MINOR)
OVERHAUL_TYPE_LABELS = {OVERHAUL_MAJOR: "Major", OVERHAUL_MINOR: "Minor"}

DONE_BY_INHOUSE = "INHOUSE"
DONE_BY_VENDOR = "VENDOR"
DONE_BY_OPTIONS = (DONE_BY_INHOUSE, DONE_BY_VENDOR)
DONE_BY_LABELS = {DONE_BY_INHOUSE: "In-house", DONE_BY_VENDOR: "Vendor"}

# --- Preventive maintenance -------------------------------------------------
FREQ_DAILY = "DAILY"
FREQ_WEEKLY = "WEEKLY"
FREQ_MONTHLY = "MONTHLY"
FREQ_QUARTERLY = "QUARTERLY"
FREQ_HALF_YEARLY = "HALF_YEARLY"
FREQ_ANNUAL = "ANNUAL"

PM_FREQUENCIES = (
    FREQ_DAILY,
    FREQ_WEEKLY,
    FREQ_MONTHLY,
    FREQ_QUARTERLY,
    FREQ_HALF_YEARLY,
    FREQ_ANNUAL,
)

PM_FREQUENCY_LABELS = {
    FREQ_DAILY: "Daily",
    FREQ_WEEKLY: "Weekly",
    FREQ_MONTHLY: "Monthly",
    FREQ_QUARTERLY: "Quarterly",
    FREQ_HALF_YEARLY: "Half-Yearly",
    FREQ_ANNUAL: "Annual",
}

# Day count each frequency resolves to, for computing next_pm_due.
PM_FREQUENCY_DAYS = {
    FREQ_DAILY: 1,
    FREQ_WEEKLY: 7,
    FREQ_MONTHLY: 30,
    FREQ_QUARTERLY: 91,
    FREQ_HALF_YEARLY: 182,
    FREQ_ANNUAL: 365,
}

PM_RESULT_OK = "OK"
PM_RESULT_NOT_OK = "NOT_OK"
PM_RESULT_NA = "NA"
PM_RESULTS = (PM_RESULT_OK, PM_RESULT_NOT_OK, PM_RESULT_NA)
PM_RESULT_LABELS = {PM_RESULT_OK: "OK", PM_RESULT_NOT_OK: "Not OK", PM_RESULT_NA: "N/A"}

# --- Machine criticality ----------------------------------------------------
# Drives the "which overdue PM actually matters" ordering in the due reports.
CRITICALITY_A = "A"
CRITICALITY_B = "B"
CRITICALITY_C = "C"
CRITICALITIES = (CRITICALITY_A, CRITICALITY_B, CRITICALITY_C)
CRITICALITY_LABELS = {
    CRITICALITY_A: "A - Critical",
    CRITICALITY_B: "B - Important",
    CRITICALITY_C: "C - Normal",
}

# --- Analytics tuning -------------------------------------------------------
# A breakdown on the same machine + component within this many days of a previous
# one is flagged as a repeat failure -- the single most useful signal for telling
# "we fixed it" from "we patched it".
REPEAT_FAILURE_WINDOW_DAYS = 30

# "No Reason mentioned" -- a real code the mill already uses, seeded as a global
# reason. Downtime sitting on it is genuine downtime with no diagnosis, so every
# summary report returns the share of total downtime carrying this code rather
# than letting it quietly absorb real causes.
NR_REASON_CODE = "NR"

# Fallback available hours per machine per day when computing availability %,
# used only where no spell/shift rows resolve for the branch.
DEFAULT_AVAILABLE_HOURS_PER_DAY = 24.0

# --- Power / utilities ------------------------------------------------------
# ITM-004: daily power readings (jute_power_meter_mst + jute_power_reading,
# router src/juteMechanical/utility.py). Meter types are an app-level enum; the
# meters themselves (MAIN, DG1, DG2, SOLAR...) are a master table per branch.
METER_TYPE_GRID = "GRID"
METER_TYPE_DG = "DG"
METER_TYPE_SOLAR = "SOLAR"

METER_TYPES = (METER_TYPE_GRID, METER_TYPE_DG, METER_TYPE_SOLAR)

METER_TYPE_LABELS = {
    METER_TYPE_GRID: "Grid",
    METER_TYPE_DG: "DG set",
    METER_TYPE_SOLAR: "Solar",
}

# jute_power_reading.spell_id is INT NOT NULL with this sentinel for a whole-day
# reading: MySQL treats NULLs as distinct inside a UNIQUE key, so a nullable
# spell_id would let two whole-day rows through uq_jpr_scope. The API still
# exposes and accepts null for "whole day".
WHOLE_DAY_SPELL_ID = 0

# Readings older than this many days (compared against today in IST) are
# locked. The lock is overridden by CAPABILITY, never by role id: roles_mst is
# per tenant DB, so the spec's sls ids (13 superadmin / 14 MIS ADMIN) are
# whatever rows carry those ids on dev3 or a future tenant. A user may change
# a locked day when a role they hold on the branch grants Edit
# (access_type_id >= POWER_LOCK_OVERRIDE_ACCESS_LEVEL) on the entry page's
# menu -- resolved through common.portal.query.get_user_menu_access_level_query,
# the same ordinal the sidebar's hasMenuAccess uses (1 Read, 2 Print, 3 Write,
# 4 Edit). Keying inside the window needs Write (3); the menu migration
# (20261018_add_power_menus.sql) grants the levels by role NAME.
POWER_LOCK_DAYS = 7
POWER_ENTRY_MENU_PATH = "juteMechanical/powerReading"
POWER_LOCK_OVERRIDE_ACCESS_LEVEL = 4

# A day's kWh above this multiple of the meter's 30-day median is a WARNING on
# save (never a rejection) -- the usual sign of a mis-keyed closing reading.
POWER_SPIKE_FACTOR = 3.0
POWER_MEDIAN_WINDOW_DAYS = 30
