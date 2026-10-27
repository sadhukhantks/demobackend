"""Regression guard: /hrms/pay_roll_upload resolves emp_code within one branch.

emp_code is unique per branch, not per tenant — on sls ~2.8k codes are shared by
a branch-4 and a branch-87 employee. When the lookup was unscoped, whichever
twin the query returned last won, and a payroll Excel silently posted 8 of its
690 rows to employees on another branch. Those employees then priced out at 0 in
the pay register because their pay_components_custom rows never existed.
"""

import inspect

from src.hrms.payRoll import pay_roll_upload


def test_upload_requires_branch_id():
    assert "branch_id" in inspect.signature(pay_roll_upload).parameters


def test_emp_code_lookup_is_branch_scoped():
    src = inspect.getsource(pay_roll_upload)
    lookup = src[src.index("FROM hrms_ed_official_details"):]
    where = lookup[:lookup.index('"""')]
    assert "branch_id = :branch_id" in where
