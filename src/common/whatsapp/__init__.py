"""WhatsApp Cloud API (Meta Graph API) integration — deliberately simple, test-grade.

client.py         send text / PDF document / template messages
party_pdf.py      one-page "Party Details" PDF (test payload)
daily_reports.py  subscription-driven daily report sender (+ CLI)
send_test.py      CLI for a quick text / party-PDF test without logging in
router.py         /api/whatsapp endpoints
query.py          SQL for whatsapp_report_subscription / whatsapp_message_log
"""
