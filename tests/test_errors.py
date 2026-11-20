import unittest
from quota_resume.errors import ActionError, describe, diagnostic_reason
from quota_resume.readonly import Unavailable


class ErrorTests(unittest.TestCase):
    def test_known_conflict_is_actionable(self):
        message=describe(ActionError('old_plugin_active'))
        self.assertIn('Enable.cmd',message)
        self.assertIn('暂停',message)

    def test_unknown_exception_text_is_never_exposed(self):
        self.assertNotIn('secret-path-or-key',describe(ValueError('secret-path-or-key')))

    def test_known_compatibility_reason_is_preserved_but_unknown_details_are_hidden(self):
        self.assertEqual(diagnostic_reason(Unavailable('desktop_engine_not_identified')),'desktop_engine_not_identified')
        self.assertIsNone(diagnostic_reason(Unavailable('secret-path-or-key')))
        self.assertNotIn('secret-path-or-key',describe(Unavailable('secret-path-or-key')))


if __name__=='__main__': unittest.main()
