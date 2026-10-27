"""SQL for whatsapp_config / whatsapp_report_subscription / whatsapp_message_log (tenant DB)."""

from sqlalchemy import text


def get_active_config():
    """Sender credentials: the newest active row wins."""
    return text("""
SELECT phone_number_id, access_token, api_version
FROM whatsapp_config
WHERE active = 1
ORDER BY whatsapp_config_id DESC
LIMIT 1
""")


def get_active_subscriptions(report_code: str | None = None):
    code_filter = "AND s.report_code = :report_code" if report_code else ""
    return text(f"""
SELECT
  s.whatsapp_report_subscription_id,
  s.report_code,
  s.co_id,
  s.branch_id,
  s.recipient_name,
  s.phone_no,
  s.send_time,
  s.template_name,
  s.template_language,
  s.last_sent_date,
  bm.branch_name
FROM whatsapp_report_subscription s
LEFT JOIN branch_mst bm ON bm.branch_id = s.branch_id
WHERE s.active = 1 {code_filter}
ORDER BY s.whatsapp_report_subscription_id
""")


def get_branch_name():
    return text("SELECT branch_name FROM branch_mst WHERE branch_id = :branch_id")


def mark_subscription_sent():
    """Never moves last_sent_date backwards (a forced resend of an older date)."""
    return text("""
UPDATE whatsapp_report_subscription
   SET last_sent_date = GREATEST(COALESCE(last_sent_date, :report_date), :report_date)
 WHERE whatsapp_report_subscription_id = :subscription_id
""")


def insert_message_log():
    return text("""
INSERT INTO whatsapp_message_log
  (whatsapp_report_subscription_id, report_code, phone_no, message_type,
   file_name, status, wa_message_id, error_text)
VALUES
  (:subscription_id, :report_code, :phone_no, :message_type,
   :file_name, :status, :wa_message_id, :error_text)
""")


def log_message(
    db,
    *,
    phone_no: str,
    message_type: str,
    status: str,
    subscription_id: int | None = None,
    report_code: str | None = None,
    file_name: str | None = None,
    wa_message_id: str | None = None,
    error_text: str | None = None,
) -> None:
    """Insert one whatsapp_message_log row (caller commits)."""
    db.execute(insert_message_log(), {
        "subscription_id": subscription_id,
        "report_code": report_code,
        "phone_no": str(phone_no)[:25],
        "message_type": message_type,
        "file_name": (file_name or None) and str(file_name)[:255],
        "status": status,
        "wa_message_id": wa_message_id,
        "error_text": error_text,
    })
