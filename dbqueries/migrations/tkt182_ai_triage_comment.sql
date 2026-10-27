-- TKT-000182 -- internal [AI-TRIAGE] verdict comment (vowconsole3).
-- Written as a script because the auto-mode permission classifier denied the
-- shared-resource write during triage. Nothing else on the ticket changes:
-- no status_id, no priority, no assignee; is_internal = 1 (never reporter-visible).
-- Text refreshed after: (a) the owner's mill-use-only revision, (b) the jute-issue
-- KG fix, (c) the spinning-production row from doff weights. Slightly over the
-- ~1500-char norm because it now covers three changes; trim (3) if that matters.
-- Rollback: DELETE FROM support_ticket_comment WHERE comment_id = <inserted id>;

INSERT INTO support_ticket_comment
  (ticket_id, author_type, author_user_id, author_name, comment_text, is_internal)
VALUES (182, 'vow', NULL, 'AI Triage', '[AI-TRIAGE] type=new feature (additive) + 1 bug fixed on the same report | route=self | tier=opus
(1) FEATURE, per owner review: "Yarn for Mill Use" prints every counted line, so a quality repeats per package and per place and its whole mill-use position appears nowhere (sls br29, 18-09-2026: 21 lines over 8 qualities). Added a consolidated quality-wise table ABOVE that block; the detail table now renders minimized and expands when pressed. Other four blocks untouched, print keeps every line. build_bucket_quality_summary(), winding_reports.py:722, returned as stock_summary; FE windingWeavingDifference/page.tsx. Ties to the MILL_USE subtotal (11.581 MT), not the 13.676 grand total; prvs to 17-09''s 7.428 MT.
(2) ALSO per owner: "Spinning Production (MT)" row added to Production Reconciliation, day + MTD, from weighed doffs ("From doff wt" = daily_doff_tbl.net_weight) via get_spinning_doff_weight_query(), doff_analysis_query.py:55, sharing the Doff Analysis scope rule incl. weight_type IN (''SPG1'',''Auto''). sls br29 18-09: 20.138 MT vs winding 21.160; Sept MTD 355.411 vs 353.184 -- spinning and winding agree within ~1%. Context only, like jute issue: never in should_be_closing_mt or difference_mt. FINDING for the mill: the planning snapshot jute_prod_spinning_daily.act_prod_doff read 0.0 on 12 of September''s 18 days and matched the doffs exactly when filled -- unfilled data entry, not a second measurement.
(3) BUG: "Jute Issue (MT)" read 100x high -- jute_issue.weight is KG but the query aliased it issue_qtl and the caller divided by 10. Now /1000 (issue_kg_to_mt(), winding_reports.py:917; alias and docstring fixed in winding_stock_query.py:542). 14-09-2026: 1927.400 -> 19.274 MT; Sept MTD 23522.700 -> 235.227 MT. Context only, so the difference does not move. WARN THE MILL: this number drops 100x.
Tests: 40 BE (test_winding_stock_report.py) + 6 FE (page.test.tsx) pass.
Action: fix on branch claudehost (uncommitted, awaiting owner verification)', 1);

UPDATE support_ticket SET updated_date_time = NOW() WHERE ticket_id = 182;
