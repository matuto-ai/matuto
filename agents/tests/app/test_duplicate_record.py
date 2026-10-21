"""Record duplication through the store API and its normal save validation."""

from agents.tests.app.test_record_validation import RecordTestCase, NOW

from matuto import store as store_module


class DuplicateRecordTest(RecordTestCase):
    def save(self, record, **kwargs):
        return store_module.save_record(self.store, record, **kwargs)

    def test_language_duplicate_keeps_values_without_publication_or_history(self):
        self.insert_language_profile()
        self.connection.execute(
            "UPDATE model_profiles SET published = 1 WHERE profile_id = 'prof-coder'")
        self.connection.executescript(f"""
            INSERT INTO runs VALUES ('run-accepted', 'language', 'prof-coder', 'Coder', 'fp', NULL,
                'smoke', 'test', NULL, 'completed', 0, '', '', 0, '{NOW}', NULL);
            INSERT INTO qualification_records VALUES ('qual-accepted', 'language', 'prof-coder',
                'Coder', 'fp', NULL, '{NOW}');
            INSERT INTO acceptances VALUES ('accept-accepted', 'qual-accepted', 'scope', '',
                'operator', '{NOW}', NULL);
        """)
        original_before = store_module.read_record(self.store, "language", "prof-coder")
        before_counts = {
            table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("runs", "qualification_records", "acceptances")
        }

        copied, warnings = store_module.duplicate_record(
            self.store, "language", "prof-coder", "prof-copy", "Coder copy",
            alias="qwen3-copy", help_text=self.help_text())

        expected = {key: value for key, value in original_before.items()
                    if key != "qualification"}
        expected.update(profile_id="prof-copy", name="Coder copy",
                        alias="qwen3-copy", published=False)
        expected["created_at"] = copied["created_at"]
        self.assertEqual(copied, expected)
        self.assertNotEqual(copied["created_at"], original_before["created_at"])
        self.assertEqual(warnings, [])
        self.assertEqual(store_module.read_record(self.store, "language", "prof-coder"),
                         original_before)
        self.assertEqual(self.connection.execute(
            "SELECT published FROM model_profiles WHERE profile_id = 'prof-coder'").fetchone()[0], 1)
        self.assertEqual({table: self.connection.execute(
            f"SELECT count(*) FROM {table}").fetchone()[0] for table in before_counts}, before_counts)
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM runs WHERE record_id = 'prof-copy'").fetchone()[0], 0)
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM qualification_records WHERE record_id = 'prof-copy'").fetchone()[0], 0)
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM acceptance_runs ar JOIN acceptances a USING (acceptance_id)"
            " JOIN qualification_records q USING (qualification_id)"
            " WHERE q.record_id = 'prof-copy'").fetchone()[0], 0)

    def test_language_alias_collisions_are_refused_without_changes(self):
        self.insert_language_profile()
        other = dict(self.language(), profile_id="prof-published", alias="published-alias")
        self.save(other, help_text=self.help_text())
        self.connection.execute(
            "UPDATE model_profiles SET published = 1 WHERE profile_id = 'prof-coder'")
        self.connection.execute(
            "UPDATE model_profiles SET published = 1 WHERE profile_id = 'prof-published'")
        before = {table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("model_profiles", "profile_arguments", "profile_artifacts")}

        for alias, holder in (("qwen3-coder", "prof-coder"),
                              ("published-alias", "prof-published")):
            with self.subTest(alias=alias):
                with self.assertRaises(store_module.RecordRefusal) as refusal:
                    store_module.duplicate_record(
                        self.store, "language", "prof-coder", "prof-copy", "Copy",
                        alias=alias, help_text=self.help_text())
                self.assertIn("alias", str(refusal.exception))
                self.assertIn(holder, str(refusal.exception))
                self.assertEqual({table: self.connection.execute(
                    f"SELECT count(*) FROM {table}").fetchone()[0] for table in before}, before)
                self.assertIsNone(self.connection.execute(
                    "SELECT 1 FROM model_profiles WHERE profile_id = 'prof-copy'").fetchone())

    def test_other_record_kinds_duplicate_through_save_without_an_alias(self):
        self.insert_language_profile()
        self.save(self.bundle())
        for kind, source, new_id in (("synthesis", "prof-breeze", "prof-synth-copy"),
                                     ("recognition", "prof-whisper", "prof-rec-copy"),
                                     ("bundle", "bundle-voice", "bundle-copy")):
            with self.subTest(kind=kind):
                before = store_module.read_record(self.store, kind, source)
                copied, _warnings = store_module.duplicate_record(
                    self.store, kind, source, new_id, f"{kind} copy", help_text=self.help_text())
                id_field = store_module.ID_FIELDS[kind]
                expected = dict(before, **{id_field: new_id}, name=f"{kind} copy")
                expected["created_at"] = copied["created_at"]
                self.assertEqual(copied, expected)
                self.assertEqual(store_module.read_record(self.store, kind, source), before)

    def test_retained_language_history_prevents_reusing_a_removed_profiles_id(self):
        self.insert_language_profile()
        original = store_module.read_record(self.store, "language", "prof-coder")
        fingerprint = store_module.fingerprint(self.store, original)
        self.connection.executescript(f"""
            INSERT INTO runs VALUES ('run-old-language', 'language', 'prof-coder',
                '{original['name']}', '{fingerprint}', NULL, 'smoke', 'test', NULL,
                'completed', 0, '', '', 0, '{NOW}', NULL);
            INSERT INTO qualification_records VALUES ('qual-old-language', 'language',
                'prof-coder', '{original['name']}', '{fingerprint}', NULL, '{NOW}');
            INSERT INTO acceptances VALUES ('accept-old-language', 'qual-old-language',
                'scope', '', 'operator', '{NOW}', NULL);
        """)
        intermediate, _warnings = store_module.duplicate_record(
            self.store, "language", "prof-coder", "prof-intermediate", "Intermediate",
            alias="qwen3-intermediate", help_text=self.help_text())
        store_module.remove_record(self.store, "language", "prof-coder")

        with self.assertRaisesRegex(store_module.RecordRefusal, "retained language evidence"):
            store_module.duplicate_record(
                self.store, "language", "prof-intermediate", "prof-coder", "Reused ID",
                alias="qwen3-reused", help_text=self.help_text())

        self.assertEqual(store_module.qualification_state(self.store, intermediate)["state"],
                         "candidate")
        self.assertEqual(self.connection.execute(
            "SELECT record_label FROM runs WHERE run_id = 'run-old-language'").fetchone()[0],
            original["name"])
        self.assertEqual(self.connection.execute(
            "SELECT record_id FROM qualification_records WHERE qualification_id = "
            "'qual-old-language'").fetchone()[0], "prof-coder")
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM model_profiles WHERE profile_id = 'prof-coder'").fetchone()[0], 0)

    def test_retained_bundle_history_prevents_reusing_a_removed_bundles_id(self):
        self.insert_language_profile()
        self.save(dict(self.bundle(), load_tokens=512, load_prompt="Describe the sea."))
        original = store_module.read_record(self.store, "bundle", "bundle-voice")
        fingerprint = store_module.fingerprint(self.store, original)
        self.connection.executescript(f"""
            INSERT INTO runs VALUES ('run-old-bundle', 'bundle', 'bundle-voice',
                '{original['name']}', '{fingerprint}', NULL, 'bundle', 'test', NULL,
                'completed', 0, '', '', 0, '{NOW}', NULL);
            INSERT INTO qualification_records VALUES ('qual-old-bundle', 'bundle',
                'bundle-voice', '{original['name']}', '{fingerprint}', NULL, '{NOW}');
            INSERT INTO acceptances VALUES ('accept-old-bundle', 'qual-old-bundle',
                'scope', '', 'operator', '{NOW}', NULL);
        """)
        intermediate, _warnings = store_module.duplicate_record(
            self.store, "bundle", "bundle-voice", "bundle-intermediate", "Intermediate",
            help_text=self.help_text())
        store_module.remove_record(self.store, "bundle", "bundle-voice")

        with self.assertRaisesRegex(store_module.RecordRefusal, "retained bundle evidence"):
            store_module.duplicate_record(
                self.store, "bundle", "bundle-intermediate", "bundle-voice", "Reused ID",
                help_text=self.help_text())

        state = store_module.qualification_state(self.store, intermediate)
        self.assertEqual(state["state"], "candidate", state)
        self.assertEqual(self.connection.execute(
            "SELECT record_id FROM runs WHERE run_id = 'run-old-bundle'").fetchone()[0],
            "bundle-voice")
        self.assertEqual(self.connection.execute(
            "SELECT record_id FROM qualification_records WHERE qualification_id = "
            "'qual-old-bundle'").fetchone()[0], "bundle-voice")
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM bundles WHERE bundle_id = 'bundle-voice'").fetchone()[0], 0)

    def test_refused_duplicates_leave_the_store_unchanged(self):
        self.insert_language_profile()
        before = {table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("model_profiles", "profile_arguments", "profile_artifacts")}
        for new_id in ("prof-coder", "../bad"):
            with self.subTest(new_id=new_id):
                with self.assertRaises(store_module.RecordRefusal):
                    store_module.duplicate_record(
                        self.store, "language", "prof-coder", new_id, "Copy",
                        alias="qwen3-copy", help_text=self.help_text())
        with self.assertRaises(store_module.RecordRefusal):
            store_module.duplicate_record(self.store, "language", "prof-coder", "prof-bad",
                                          "Copy", alias="", help_text=self.help_text())
        with self.assertRaises(store_module.StoreError):
            store_module.duplicate_record(self.store, "language", "missing", "prof-missing",
                                          "Missing copy", alias="new")
        self.connection.execute(
            "UPDATE model_profiles SET context_size = 0 WHERE profile_id = 'prof-coder'")
        with self.assertRaisesRegex(store_module.RecordRefusal, "context_size"):
            store_module.duplicate_record(
                self.store, "language", "prof-coder", "prof-invalid", "Invalid copy",
                alias="invalid-copy", help_text=self.help_text())
        self.assertEqual({table: self.connection.execute(
            f"SELECT count(*) FROM {table}").fetchone()[0] for table in before}, before)
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM model_profiles WHERE profile_id LIKE '%copy' OR profile_id = 'prof-bad'"
        ).fetchone())
