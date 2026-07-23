from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import deal_tracker.intake as intake_module
from deal_tracker.db import SCHEMA
from deal_tracker.intake import create_import, normalize_domain, rows_from_csv, rows_from_paste, valid_domain


class IntakeTests(unittest.TestCase):
    def test_normalizes_urls_emails_and_domains(self) -> None:
        self.assertEqual(normalize_domain("https://www.Example.com.au/advertise?q=1"), "example.com.au")
        self.assertEqual(normalize_domain("editor@example.com.au"), "example.com.au")
        self.assertEqual(normalize_domain("example.com.au"), "example.com.au")
        self.assertTrue(valid_domain("example.com.au"))
        self.assertFalse(valid_domain("not-a-domain"))

    def test_csv_column_mapping_handles_common_exports(self) -> None:
        rows = rows_from_csv(
            b"referring_domain,best_contact_email,industry,placement_type\nExample.com.au,editor@example.com.au,Travel,guest_post\n"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].root_domain, "example.com.au")
        self.assertEqual(rows[0].contact_email, "editor@example.com.au")
        self.assertEqual(rows[0].niche, "Travel")
        self.assertEqual(rows[0].opportunity_type, "guest_post")

    def test_paste_parser_accepts_mixed_inputs(self) -> None:
        rows = rows_from_paste("example.com.au\nhttps://second.com.au/write-for-us;hello@third.com.au")
        self.assertEqual([row.root_domain for row in rows], ["example.com.au", "second.com.au", "third.com.au"])

    def test_import_audits_duplicates_and_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intake.db"

            def connect() -> sqlite3.Connection:
                connection = sqlite3.connect(path)
                connection.row_factory = sqlite3.Row
                return connection

            def init_db() -> None:
                with connect() as connection:
                    connection.executescript(SCHEMA)
                    connection.commit()

            original_connect = intake_module.connect
            original_init = intake_module.init_db
            intake_module.connect = connect
            intake_module.init_db = init_db
            try:
                result = create_import(
                    rows_from_paste("example.com.au example.com.au invalid"),
                    source_type="paste",
                    source_name="test",
                    queue=False,
                )
                self.assertEqual(result["accepted_rows"], 1)
                self.assertEqual(result["duplicate_rows"], 1)
                self.assertEqual(result["rejected_rows"], 1)
                with connect() as connection:
                    self.assertEqual(connection.execute("select count(*) from prospect_records").fetchone()[0], 1)
                    self.assertEqual(connection.execute("select count(*) from prospect_import_rows").fetchone()[0], 3)
            finally:
                intake_module.connect = original_connect
                intake_module.init_db = original_init


if __name__ == "__main__":
    unittest.main()
