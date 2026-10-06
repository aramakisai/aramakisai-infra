import gzip
import io
import json
import unittest
import zipfile
from email.message import EmailMessage

import report_ingest as ri
from helpers import FIXED_NOW, temp_store

DMARC_XML = b"""<?xml version="1.0" encoding="UTF-8" ?>
<feedback>
  <report_metadata><org_name>google.com</org_name><report_id>1234</report_id>
    <date_range><begin>1780272000</begin><end>1780358399</end></date_range></report_metadata>
  <policy_published><domain>aramakisai.com</domain></policy_published>
  <record><row><source_ip>192.0.2.1</source_ip><count>3</count>
    <policy_evaluated><disposition>none</disposition><dkim>pass</dkim><spf>fail</spf></policy_evaluated></row>
    <identifiers><header_from>aramakisai.com</header_from></identifiers></record>
  <record><row><source_ip>198.51.100.7</source_ip><count>2</count>
    <policy_evaluated><disposition>reject</disposition><dkim>fail</dkim><spf>fail</spf></policy_evaluated></row>
    <identifiers><header_from>aramakisai.com</header_from></identifiers></record>
</feedback>"""

TLS = {"organization-name": "Google Inc.", "date-range": {"start-datetime": "2026-06-01T00:00:00Z",
       "end-datetime": "2026-06-01T23:59:59Z"}, "report-id": "r-1",
       "policies": [{"policy": {"policy-type": "sts", "policy-domain": "aramakisai.com"},
                     "summary": {"total-successful-session-count": 10, "total-failure-session-count": 2}},
                    {"policy": {"policy-domain": "aramakisai.com"},
                     "summary": {"total-successful-session-count": 5, "total-failure-session-count": 0}}]}


def mail(payload, name, ctype=("application", "octet-stream")):
    m = EmailMessage()
    m["Subject"] = "Report"
    m.set_content("see attachment")
    m.add_attachment(payload, maintype=ctype[0], subtype=ctype[1], filename=name)
    return m.as_bytes()


def zipped(data, name="r.xml"):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(name, data)
    return b.getvalue()


class Parse(unittest.TestCase):
    def check_dmarc(self, raw):
        reports, errors = ri.parse_message(raw)
        self.assertEqual(errors, [])
        (kind, r), = reports
        self.assertEqual(kind, "dmarc")
        self.assertEqual((r["org_name"], r["report_id"]), ("google.com", "1234"))
        self.assertEqual(r["begin"], "2026-06-01T00:00:00Z")
        self.assertEqual([(x["source_ip"], x["count"], x["dkim"], x["spf"], x["disposition"]) for x in r["records"]],
                         [("192.0.2.1", 3, "pass", "fail", "none"), ("198.51.100.7", 2, "fail", "fail", "reject")])

    def test_dmarc_zip(self):
        self.check_dmarc(mail(zipped(DMARC_XML), "r.zip", ("application", "zip")))

    def test_dmarc_gzip(self):
        self.check_dmarc(mail(gzip.compress(DMARC_XML), "r.xml.gz", ("application", "gzip")))

    def test_dmarc_plain_xml(self):
        self.check_dmarc(mail(DMARC_XML, "r.xml", ("text", "xml")))

    def test_tlsrpt_gzip_json(self):
        reports, errors = ri.parse_message(mail(gzip.compress(json.dumps(TLS).encode()), "r.json.gz",
                                                ("application", "tlsrpt+gzip")))
        self.assertEqual(errors, [])
        (kind, r), = reports
        self.assertEqual(kind, "tlsrpt")
        self.assertEqual((r["org_name"], r["report_id"], r["policy_domain"], r["success"], r["failure"]),
                         ("Google Inc.", "r-1", "aramakisai.com", 15, 2))
        self.assertEqual(r["end"], "2026-06-01T23:59:59Z")

    def test_corrupt_gzip_is_error(self):
        reports, errors = ri.parse_message(mail(b"\x1f\x8bnot-gzip", "r.gz", ("application", "gzip")))
        self.assertEqual(reports, [])
        self.assertEqual(len(errors), 1)

    def test_broken_xml_is_error(self):
        reports, errors = ri.parse_message(mail(b"<feedback><oops>", "r.xml", ("text", "xml")))
        self.assertEqual((reports, len(errors)), ([], 1))

    def test_doctype_rejected(self):
        evil = b'<!DOCTYPE feedback [<!ENTITY a "x">]><feedback><report_metadata/></feedback>'
        reports, errors = ri.parse_message(mail(evil, "r.xml", ("text", "xml")))
        self.assertEqual((reports, len(errors)), ([], 1))

    def test_oversized_gzip_rejected(self):
        reports, errors = ri.parse_message(mail(gzip.compress(b"<feedback>" + b" " * (ri.MAX_BYTES + 10)), "r.gz",
                                                ("application", "gzip")))
        self.assertEqual((reports, len(errors)), ([], 1))

    def test_non_report_mail_is_ignored(self):
        self.assertEqual(ri.parse_message(mail(b"%PDF-1.4", "x.pdf", ("application", "pdf"))), ([], []))


class Save(unittest.TestCase):
    def setUp(self):
        self.d, self.st = temp_store()

    def tearDown(self):
        self.st.close()
        self.d.cleanup()

    def ingest(self, key, raw):
        return ri.ingest_message(self.st, key, raw, FIXED_NOW)

    def count(self, t):
        return self.st.query(f"SELECT count(*) FROM {t}")[0][0]

    def test_dedup_by_org_and_report_id(self):
        raw = mail(DMARC_XML, "r.xml", ("text", "xml"))
        self.assertEqual(self.ingest("k1", raw), "dmarc")
        self.assertEqual(self.ingest("k2", raw), "dmarc")
        self.assertEqual((self.count("dmarc_reports"), self.count("dmarc_records"), self.count("report_messages")),
                         (1, 2, 2))

    def test_failure_recorded_and_message_marked(self):
        self.assertEqual(self.ingest("bad", mail(b"<feedback><x>", "r.xml", ("text", "xml"))), "other")
        self.assertEqual(self.count("ingest_failures"), 1)
        self.assertEqual(self.st.query("SELECT kind FROM report_messages")[0][0], "other")

    def test_tlsrpt_saved(self):
        self.ingest("t", mail(json.dumps(TLS).encode(), "r.json", ("application", "tlsrpt+json")))
        row = self.st.query("SELECT success, failure FROM tlsrpt_reports")[0]
        self.assertEqual((row[0], row[1]), (15, 2))


if __name__ == "__main__":
    unittest.main()
