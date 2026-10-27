"""Tests for the WhatsApp Cloud API integration (src/common/whatsapp).

No real network: httpx is driven through httpx.MockTransport. No real DB: the
daily-report job runs against a fake session and monkeypatched helpers.
"""

import json
from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.common.whatsapp import client as wa
from src.common.whatsapp import daily_reports as dr
from src.common.whatsapp import party_pdf
from src.config.db import get_tenant_db
from src.main import app

TOKEN = "SECRET-TOKEN-VALUE"


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture
def wa_env(monkeypatch, tmp_path):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1234567890")
    monkeypatch.delenv("WHATSAPP_API_VERSION", raising=False)
    monkeypatch.setenv("WHATSAPP_ENV_FILE", str(tmp_path / "absent.env"))


@pytest.fixture
def no_wa_env(monkeypatch, tmp_path):
    for k in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_API_VERSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WHATSAPP_ENV_FILE", str(tmp_path / "absent.env"))
    return tmp_path


def _mock_http(monkeypatch, handler):
    """Route the client's httpx calls to `handler`; returns the recorded requests."""
    calls = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(wa, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(_wrapped)))
    return calls


def _ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/media"):
        return httpx.Response(200, json={"id": "MEDIA123"})
    return httpx.Response(200, json={"messages": [{"id": "wamid.TEST"}]})


# ── normalize_phone ──────────────────────────────────────────────────────────

class TestNormalizePhone:
    @pytest.mark.parametrize("raw,expected", [
        ("+91 84201 10041", "918420110041"),
        ("8420110041", "918420110041"),
        ("08420110041", "918420110041"),
        ("918420110041", "918420110041"),
        ("0091-84201-10041", "918420110041"),
        ("+1 (415) 555-2671", "14155552671"),
        (8420110041, "918420110041"),
    ])
    def test_valid(self, raw, expected):
        assert wa.normalize_phone(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "12345", "abc", "1234567890123456"])
    def test_invalid(self, raw):
        with pytest.raises(ValueError):
            wa.normalize_phone(raw)


# ── config ───────────────────────────────────────────────────────────────────

class TestConfig:
    def test_missing_both(self, no_wa_env):
        with pytest.raises(wa.WhatsAppConfigError) as exc:
            wa.send_text("8420110041", "hi")
        assert "WHATSAPP_ACCESS_TOKEN" in str(exc.value)
        assert "WHATSAPP_PHONE_NUMBER_ID" in str(exc.value)

    def test_missing_one_names_only_that(self, no_wa_env, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", TOKEN)
        with pytest.raises(wa.WhatsAppConfigError) as exc:
            wa.send_text("8420110041", "hi")
        assert "WHATSAPP_PHONE_NUMBER_ID" in str(exc.value)
        assert "WHATSAPP_ACCESS_TOKEN" not in str(exc.value)
        assert TOKEN not in str(exc.value)

    def test_env_file_fallback_read_at_call_time(self, no_wa_env, monkeypatch):
        env_file = no_wa_env / "whatsapp.env"
        monkeypatch.setenv("WHATSAPP_ENV_FILE", str(env_file))
        assert wa.is_configured() is False
        env_file.write_text("WHATSAPP_ACCESS_TOKEN=tok1\nWHATSAPP_PHONE_NUMBER_ID=555\nWHATSAPP_API_VERSION=v99.0\n")
        calls = _mock_http(monkeypatch, _ok_handler)
        wa.send_text("8420110041", "hi")
        assert calls[0].url.path == "/v99.0/555/messages"
        assert calls[0].headers["authorization"] == "Bearer tok1"
        # rotated token is picked up without a restart
        env_file.write_text("WHATSAPP_ACCESS_TOKEN=tok2\nWHATSAPP_PHONE_NUMBER_ID=555\n")
        wa.send_text("8420110041", "hi")
        assert calls[1].headers["authorization"] == "Bearer tok2"
        assert calls[1].url.path == "/v23.0/555/messages"


# ── payload shapes ───────────────────────────────────────────────────────────

class TestSend:
    def test_send_text(self, wa_env, monkeypatch):
        calls = _mock_http(monkeypatch, _ok_handler)
        resp = wa.send_text("+91 84201 10041", "hello")
        assert wa.message_id(resp) == "wamid.TEST"
        assert len(calls) == 1
        assert str(calls[0].url) == "https://graph.facebook.com/v23.0/1234567890/messages"
        assert calls[0].headers["authorization"] == f"Bearer {TOKEN}"
        body = json.loads(calls[0].content)
        assert body["messaging_product"] == "whatsapp"
        assert body["to"] == "918420110041"
        assert body["type"] == "text"
        assert body["text"]["body"] == "hello"

    def test_send_document_uploads_then_sends(self, wa_env, monkeypatch):
        calls = _mock_http(monkeypatch, _ok_handler)
        resp = wa.send_document("8420110041", b"%PDF-1.4 fake", "DPR.pdf", caption="Daily report")
        assert wa.message_id(resp) == "wamid.TEST"
        assert [c.url.path for c in calls] == ["/v23.0/1234567890/media", "/v23.0/1234567890/messages"]

        upload = calls[0]
        assert upload.headers["content-type"].startswith("multipart/form-data")
        raw = upload.content
        assert b'name="messaging_product"' in raw and b"whatsapp" in raw
        assert b'filename="DPR.pdf"' in raw
        assert b"application/pdf" in raw
        assert b"%PDF-1.4 fake" in raw

        msg = json.loads(calls[1].content)
        assert msg["type"] == "document"
        assert msg["to"] == "918420110041"
        assert msg["document"] == {"id": "MEDIA123", "filename": "DPR.pdf", "caption": "Daily report"}

    def test_send_document_without_caption(self, wa_env, monkeypatch):
        calls = _mock_http(monkeypatch, _ok_handler)
        wa.send_document("8420110041", b"%PDF", "a.pdf")
        assert "caption" not in json.loads(calls[1].content)["document"]

    def test_send_template_document_header(self, wa_env, monkeypatch):
        calls = _mock_http(monkeypatch, _ok_handler)
        wa.send_template("8420110041", "daily_report", "en_US",
                         body_params=["FACTORY", "30-Sep-2026"], document=(b"%PDF", "DPR.pdf"))
        assert [c.url.path.rsplit("/", 1)[1] for c in calls] == ["media", "messages"]
        msg = json.loads(calls[1].content)
        assert msg["type"] == "template"
        tpl = msg["template"]
        assert tpl["name"] == "daily_report"
        assert tpl["language"] == {"code": "en_US"}
        assert tpl["components"][0] == {
            "type": "header",
            "parameters": [{"type": "document", "document": {"id": "MEDIA123", "filename": "DPR.pdf"}}],
        }
        assert tpl["components"][1] == {
            "type": "body",
            "parameters": [{"type": "text", "text": "FACTORY"}, {"type": "text", "text": "30-Sep-2026"}],
        }

    def test_send_template_plain(self, wa_env, monkeypatch):
        calls = _mock_http(monkeypatch, _ok_handler)
        wa.send_template("8420110041", "hello_world")
        assert len(calls) == 1
        tpl = json.loads(calls[0].content)["template"]
        assert tpl == {"name": "hello_world", "language": {"code": "en"}}


# ── errors ───────────────────────────────────────────────────────────────────

class TestApiErrors:
    def test_meta_error_surfaced(self, wa_env, monkeypatch):
        err = {"message": "(#131030) Recipient phone number not in allowed list", "type": "OAuthException",
               "code": 131030, "error_subcode": 2655007, "fbtrace_id": "TRACE1",
               "error_data": {"details": "Add the number to the allowed list"}}
        _mock_http(monkeypatch, lambda r: httpx.Response(400, json={"error": err}))
        with pytest.raises(wa.WhatsAppApiError) as exc:
            wa.send_text("8420110041", "hi")
        e = exc.value
        assert e.status_code == 400
        assert e.code == 131030
        assert e.error_subcode == 2655007
        assert e.fbtrace_id == "TRACE1"
        assert "not in allowed list" in e.meta_message
        assert "131030" in str(e) and "TRACE1" in str(e) and "allowed list" in str(e)
        assert TOKEN not in str(e)

    def test_non_json_error(self, wa_env, monkeypatch):
        _mock_http(monkeypatch, lambda r: httpx.Response(500, text="upstream down"))
        with pytest.raises(wa.WhatsAppApiError) as exc:
            wa.send_text("8420110041", "hi")
        assert exc.value.status_code == 500
        assert "upstream down" in str(exc.value)

    def test_upload_failure_stops_before_message(self, wa_env, monkeypatch):
        calls = _mock_http(monkeypatch, lambda r: httpx.Response(401, json={"error": {"message": "bad token", "code": 190}}))
        with pytest.raises(wa.WhatsAppApiError) as exc:
            wa.send_document("8420110041", b"%PDF", "a.pdf")
        assert exc.value.code == 190
        assert len(calls) == 1

    def test_network_error_wrapped_without_token(self, wa_env, monkeypatch):
        def boom(request):
            raise httpx.ConnectError("connection refused")
        _mock_http(monkeypatch, boom)
        with pytest.raises(wa.WhatsAppApiError) as exc:
            wa.send_text("8420110041", "hi")
        assert exc.value.status_code is None
        assert TOKEN not in str(exc.value)


# ── party PDF ────────────────────────────────────────────────────────────────

PARTY = {
    "party_id": 77, "active": 1, "supp_name": "ACME JUTE & CO <PVT> LTD", "supp_code": "ACM01",
    "phone_no": "8420110041", "cin": None, "supp_contact_person": "R. Kumar",
    "supp_contact_designation": "Manager", "supp_email_id": "a@b.co", "party_pan_no": "ABCDE1234F",
    "entity_type_id": 1, "entity_type_name": "Company", "msme_certified": 1, "co_id": 1,
    "country": "India", "party_type_id": "1",
}
BRANCHES = [
    {"active": 1, "party_mst_branch_id": 1, "gst_no": "19ABCDE1234F1Z5", "address": "12 Strand Road",
     "address_additional": "2nd Floor", "zip_code": 700001, "state_id": 19, "state": "West Bengal",
     "contact_no": "9830000000", "contact_person": "S. Das"},
    {"active": 0, "party_mst_branch_id": 2, "gst_no": None, "address": None,
     "address_additional": None, "zip_code": None, "state_id": None, "state": None,
     "contact_no": None, "contact_person": None},
]


def _row(mapping):
    row = MagicMock()
    row._mapping = mapping
    return row


class TestPartyPdf:
    def test_render_from_synthetic_rows(self):
        pdf = party_pdf.render_party_pdf(PARTY, BRANCHES)
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 1000

    def test_render_without_branches(self):
        assert party_pdf.render_party_pdf(PARTY, []).startswith(b"%PDF")

    def test_build_uses_queries(self):
        db = MagicMock()
        db.execute.side_effect = [
            MagicMock(fetchone=lambda: _row(PARTY)),
            MagicMock(fetchall=lambda: [_row(b) for b in BRANCHES]),
        ]
        pdf, filename = party_pdf.build_party_details_pdf(db, 77, co_id=1)
        assert pdf.startswith(b"%PDF")
        assert filename == "Party_77_ACME_JUTE_CO_PVT_LTD.pdf"

    def test_legacy_branch_fallback(self):
        db = MagicMock()
        db.execute.side_effect = [
            MagicMock(fetchone=lambda: _row(PARTY)),
            Exception("(1054, \"Unknown column 'pbm.state_id' in 'field list'\")"),
            MagicMock(fetchall=lambda: [_row(BRANCHES[0])]),
        ]
        party, branches = party_pdf.fetch_party(db, 77)
        assert len(branches) == 1
        assert db.execute.call_count == 3

    def test_party_not_found(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None
        with pytest.raises(party_pdf.PartyNotFoundError, match="Party 5 not found"):
            party_pdf.build_party_details_pdf(db, 5)

    def test_party_of_other_company(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _row(PARTY)
        with pytest.raises(party_pdf.PartyNotFoundError):
            party_pdf.build_party_details_pdf(db, 77, co_id=9)


# ── daily reports ────────────────────────────────────────────────────────────

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=dr.IST)   # 09:00 IST, report date = 30-Sep-2026
REPORT_D = date(2026, 9, 30)


def _sub(**over):
    sub = {
        "whatsapp_report_subscription_id": 1, "report_code": "DPR", "co_id": 2, "branch_id": 29,
        "recipient_name": "Owner", "phone_no": "918420110041", "send_time": timedelta(hours=8),
        "template_name": None, "template_language": None, "last_sent_date": None,
        "branch_name": "FACTORY",
    }
    sub.update(over)
    return sub


class TestIsDue:
    def test_due_when_never_sent_and_time_passed(self):
        assert dr.is_due(_sub(), NOW, REPORT_D) == (True, "")

    def test_not_due_before_send_time(self):
        due, reason = dr.is_due(_sub(send_time=timedelta(hours=10)), NOW, REPORT_D)
        assert due is False and "10:00" in reason

    def test_not_due_when_already_sent(self):
        due, reason = dr.is_due(_sub(last_sent_date=REPORT_D), NOW, REPORT_D)
        assert due is False and "already sent" in reason

    def test_due_when_last_sent_is_older(self):
        assert dr.is_due(_sub(last_sent_date=date(2026, 9, 29)), NOW, REPORT_D)[0] is True

    def test_force_skips_both_checks(self):
        sub = _sub(send_time=timedelta(hours=23), last_sent_date=REPORT_D)
        assert dr.is_due(sub, NOW, REPORT_D, force=True)[0] is True

    def test_send_time_types(self):
        assert dr._as_time(timedelta(hours=8, minutes=30)) == time(8, 30)
        assert dr._as_time("07:15:00") == time(7, 15)
        assert dr._as_time(time(6, 0)) == time(6, 0)

    def test_default_report_date_is_yesterday_ist(self):
        assert dr.default_report_date("DPR", NOW) == REPORT_D

    def test_caption(self):
        assert dr.report_caption("DPR", "FACTORY", REPORT_D) == "Daily Production Report – FACTORY – 30-Sep-2026"


@pytest.fixture
def job(monkeypatch):
    """send_due_reports wired to fakes; returns a namespace to inspect."""
    state = MagicMock()
    state.db = MagicMock()
    state.subs = []
    state.builds = []
    state.sent = []
    state.logs = []
    state.marked = []

    def fake_build(db, code, co_id, branch_id, report_d):
        state.builds.append((code, co_id, branch_id, report_d))
        return b"%PDF-fake", f"DPR_{branch_id}_{report_d.isoformat()}.pdf"

    def fake_send_document(to, pdf_bytes, filename, caption=None, db=None):
        if to == "919999999999":
            raise wa.WhatsAppApiError("WhatsApp API error (HTTP 400, code 131047): re-engagement", 400, {"code": 131047})
        state.sent.append(("document", to, filename, caption))
        return {"messages": [{"id": f"wamid.{to}"}]}

    def fake_send_template(to, template_name, language_code="en", body_params=None, document=None, db=None):
        state.sent.append(("template", to, template_name, language_code, document[1]))
        return {"messages": [{"id": f"wamid.T.{to}"}]}

    def fake_record(db, sub, phone, message_type, file_name, status, wa_id, error_text, report_d):
        state.logs.append((phone, message_type, status, wa_id, error_text))
        if status == "SENT":
            state.marked.append((sub["whatsapp_report_subscription_id"], report_d))

    monkeypatch.setattr(dr, "now_ist", lambda: NOW)
    monkeypatch.setattr(dr, "_open_session", lambda tenant: state.db)
    monkeypatch.setattr(dr, "_load_subscriptions", lambda db, code: [s for s in state.subs if not code or s["report_code"] == code])
    monkeypatch.setattr(dr, "build_report_pdf", fake_build)
    monkeypatch.setattr(dr, "_record", fake_record)
    monkeypatch.setattr(dr.client, "send_document", fake_send_document)
    monkeypatch.setattr(dr.client, "send_template", fake_send_template)
    return state


class TestSendDueReports:
    def test_due_row_is_sent_logged_and_marked(self, job):
        job.subs = [_sub()]
        res = dr.send_due_reports("sls")
        assert [r["status"] for r in res] == ["SENT"]
        assert res[0]["wa_message_id"] == "wamid.918420110041"
        assert job.builds == [("DPR", 2, 29, REPORT_D)]
        assert job.sent == [("document", "918420110041", "DPR_29_2026-09-30.pdf",
                             "Daily Production Report – FACTORY – 30-Sep-2026")]
        assert job.logs == [("918420110041", "document", "SENT", "wamid.918420110041", None)]
        assert job.marked == [(1, REPORT_D)]
        job.db.close.assert_called_once()

    def test_not_due_rows_are_skipped(self, job):
        job.subs = [
            _sub(send_time=timedelta(hours=10)),
            _sub(whatsapp_report_subscription_id=2, last_sent_date=REPORT_D),
        ]
        res = dr.send_due_reports("sls")
        assert [r["status"] for r in res] == ["SKIPPED", "SKIPPED"]
        assert job.builds == [] and job.sent == [] and job.logs == []

    def test_force_sends_not_due_rows(self, job):
        job.subs = [_sub(send_time=timedelta(hours=23), last_sent_date=REPORT_D)]
        res = dr.send_due_reports("sls", force=True)
        assert [r["status"] for r in res] == ["SENT"]

    def test_pdf_built_once_per_branch_and_failure_does_not_stop_others(self, job):
        job.subs = [
            _sub(whatsapp_report_subscription_id=1, phone_no="919999999999"),   # Meta rejects
            _sub(whatsapp_report_subscription_id=2, phone_no="8420110041"),
            _sub(whatsapp_report_subscription_id=3, phone_no="918420110042", branch_id=30),
        ]
        res = dr.send_due_reports("sls")
        assert [r["status"] for r in res] == ["FAILED", "SENT", "SENT"]
        assert "131047" in res[0]["error"]
        assert len(job.builds) == 2                      # branch 29 once, branch 30 once
        assert [l[2] for l in job.logs] == ["FAILED", "SENT", "SENT"]
        assert job.marked == [(2, REPORT_D), (3, REPORT_D)]

    def test_template_subscription_uses_send_template(self, job):
        job.subs = [_sub(template_name="daily_report", template_language="en_US")]
        res = dr.send_due_reports("sls")
        assert res[0]["status"] == "SENT"
        assert job.sent == [("template", "918420110041", "daily_report", "en_US", "DPR_29_2026-09-30.pdf")]
        assert job.logs[0][1] == "template"

    def test_only_phone_and_report_date(self, job):
        job.subs = [_sub(), _sub(whatsapp_report_subscription_id=2, phone_no="918420110042")]
        res = dr.send_due_reports("sls", only_phone="+91 84201 10042", report_date=date(2026, 9, 28), force=True)
        assert len(res) == 1 and res[0]["phone_no"] == "918420110042"
        assert job.builds == [("DPR", 2, 29, date(2026, 9, 28))]

    def test_report_code_filter(self, job):
        job.subs = [_sub(), _sub(whatsapp_report_subscription_id=2, report_code="OTHER")]
        res = dr.send_due_reports("sls", report_code="DPR")
        assert len(res) == 1

    def test_build_failure_is_reported_per_recipient(self, job, monkeypatch):
        def bad_build(*a, **k):
            raise RuntimeError("no production data")
        monkeypatch.setattr(dr, "build_report_pdf", bad_build)
        job.subs = [_sub(), _sub(whatsapp_report_subscription_id=2, phone_no="918420110042")]
        res = dr.send_due_reports("sls")
        assert [r["status"] for r in res] == ["FAILED", "FAILED"]
        assert "no production data" in res[0]["error"]
        assert job.sent == []

    def test_dry_run_sends_nothing_and_writes_nothing(self, job, tmp_path, monkeypatch):
        monkeypatch.setattr(dr, "DRY_RUN_DIR", str(tmp_path))
        job.subs = [_sub()]
        res = dr.send_due_reports("sls", force=True, dry_run=True)
        assert res[0]["status"] == "DRY_RUN"
        assert open(res[0]["pdf_path"], "rb").read() == b"%PDF-fake"
        assert job.sent == [] and job.logs == []

    def test_cli_exit_code(self, job, capsys):
        job.subs = [_sub(phone_no="919999999999")]
        assert dr.main(["--tenant", "sls"]) == 1
        job.subs = [_sub()]
        assert dr.main(["--tenant", "sls", "--report", "DPR", "--force"]) == 0
        assert "SENT" in capsys.readouterr().out


# ── router ───────────────────────────────────────────────────────────────────

@pytest.fixture
def api():
    db = MagicMock()
    # no whatsapp_config row -> the client falls back to the environment
    db.execute.return_value.mappings.return_value.first.return_value = None
    app.dependency_overrides[get_tenant_db] = lambda: db
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
    yield TestClient(app), db
    app.dependency_overrides.pop(get_tenant_db, None)
    app.dependency_overrides.pop(get_current_user_with_refresh, None)


class TestRouter:
    def test_send_test_text_ok(self, api, wa_env, monkeypatch):
        http, db = api
        _mock_http(monkeypatch, _ok_handler)
        r = http.post("/api/whatsapp/send_test_text", json={"to": "8420110041", "message": "hi"})
        assert r.status_code == 200
        assert r.json()["data"]["wa_message_id"] == "wamid.TEST"
        assert r.json()["data"]["to"] == "918420110041"
        params = db.execute.call_args[0][1]
        assert params["status"] == "SENT" and params["message_type"] == "text"
        db.commit.assert_called()

    def test_missing_config_is_503(self, api, no_wa_env):
        http, _db = api
        r = http.post("/api/whatsapp/send_test_text", json={"to": "8420110041", "message": "hi"})
        assert r.status_code == 503
        assert "WHATSAPP_ACCESS_TOKEN" in r.json()["detail"]

    def test_meta_error_is_502_and_logged(self, api, wa_env, monkeypatch):
        http, db = api
        _mock_http(monkeypatch, lambda req: httpx.Response(400, json={"error": {"message": "Bad recipient", "code": 131030}}))
        r = http.post("/api/whatsapp/send_test_text", json={"to": "8420110041", "message": "hi"})
        assert r.status_code == 502
        assert "Bad recipient" in r.json()["detail"]
        assert db.execute.call_args[0][1]["status"] == "FAILED"

    def test_bad_phone_is_400(self, api, wa_env):
        http, _db = api
        r = http.post("/api/whatsapp/send_test_text", json={"to": "123", "message": "hi"})
        assert r.status_code == 400

    def test_send_party_details_defaults_to_party_phone(self, api, wa_env, monkeypatch):
        http, _db = api
        calls = _mock_http(monkeypatch, _ok_handler)
        from src.common.whatsapp import router as wa_router
        monkeypatch.setattr(wa_router, "fetch_party", lambda db, pid: (PARTY, BRANCHES))
        r = http.post("/api/whatsapp/send_party_details", json={"party_id": 77})
        assert r.status_code == 200
        assert r.json()["data"]["file_name"] == "Party_77_ACME_JUTE_CO_PVT_LTD.pdf"
        assert json.loads(calls[1].content)["to"] == "918420110041"

    def test_send_party_details_not_found(self, api, wa_env, monkeypatch):
        http, db = api
        db.execute.return_value.fetchone.return_value = None
        r = http.post("/api/whatsapp/send_party_details", json={"party_id": 5})
        assert r.status_code == 404

    def test_send_report_now(self, api, wa_env, monkeypatch):
        http, db = api
        calls = _mock_http(monkeypatch, _ok_handler)
        from src.common.whatsapp import router as wa_router
        monkeypatch.setattr(wa_router, "build_report_pdf", lambda db, code, co, br, d: (b"%PDF", f"DPR_{d.isoformat()}.pdf"))
        db.execute.return_value.scalar.return_value = "FACTORY"
        r = http.post("/api/whatsapp/send_report_now", json={
            "report_code": "dpr", "co_id": 2, "branch_id": 29, "to": "8420110041", "report_date": "2026-09-30"})
        assert r.status_code == 200, r.text
        doc = json.loads(calls[1].content)["document"]
        assert doc["filename"] == "DPR_2026-09-30.pdf"
        assert doc["caption"] == "Daily Production Report – FACTORY – 30-Sep-2026"

    def test_send_report_now_unknown_report(self, api, wa_env):
        http, _db = api
        r = http.post("/api/whatsapp/send_report_now", json={
            "report_code": "XYZ", "co_id": 2, "branch_id": 29, "to": "8420110041"})
        assert r.status_code == 400


class TestDbConfig:
    def _db(self, row):
        db = MagicMock()
        db.execute.return_value.mappings.return_value.first.return_value = row
        return db

    def test_db_row_wins_over_environment(self, wa_env):
        db = self._db({"phone_number_id": " 999 ", "access_token": " DBTOKEN ", "api_version": None})
        cfg = wa._load_config(db)
        assert cfg == {"token": "DBTOKEN", "phone_number_id": "999", "api_version": wa.DEFAULT_API_VERSION}

    def test_no_row_falls_back_to_environment(self, wa_env):
        cfg = wa._load_config(self._db(None))
        assert cfg["token"] and cfg["token"] != "DBTOKEN"

    def test_empty_token_in_row_is_config_error(self):
        db = self._db({"phone_number_id": "999", "access_token": "", "api_version": None})
        with pytest.raises(wa.WhatsAppConfigError, match="access_token"):
            wa._load_config(db)
