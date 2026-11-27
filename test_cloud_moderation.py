import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from cloud_moderation import CloudModeration, LoginRequired


class CloudRecoveryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.notice = Mock()
        self.cloud = CloudModeration(str(Path(temp.name) / 'test.db'), 'tester', 'video',
            'https://example.test', lambda video_id: {'id': video_id, 'account_name': 'tester'}, self.notice)
        self.store = self.cloud.store
        self.store.enable_cloud()
        batch = [dict(platform='channels', object_id='video', comment_id=str(i), content=str(i)) for i in range(3)]
        self.batch = self.store.create('chat', batch)
        self.store.confirm(self.batch, 'chat', 'operator')
        self.browser = Mock()

    def test_relogin_skips_confirmed_deletes(self):
        self.browser.delete.side_effect = [None, LoginRequired()]
        with self.assertRaises(LoginRequired):
            self.cloud.process(self.browser, self.batch)
        self.assertEqual(self.store.get(self.batch)['state'], 'waiting_login')
        self.assertEqual(self.store.cloud_deleted(self.batch), ['0'])
        self.store.resume_cloud()
        self.browser.delete.reset_mock(side_effect=True)
        self.cloud.process(self.browser, self.batch)
        self.assertEqual([args.args[1] for args in self.browser.delete.call_args_list], ['1', '2'])
        self.assertEqual(self.store.get(self.batch)['result']['deleted'], ['0', '1', '2'])

    def test_crash_during_delete_never_requeues_unknown_item(self):
        self.assertIsNotNone(self.store.claim_cloud(self.batch))
        self.store.begin_cloud_item(self.batch, '0')
        self.store.complete_cloud_item(self.batch, '0')
        self.store.begin_cloud_item(self.batch, '1')
        self.assertEqual(self.store.recover_cloud(), 1)
        self.assertEqual(self.store.get(self.batch)['result'], {'status':'unknown','deleted':['0'],'uncertain':'1'})
        self.assertFalse(self.store.confirmed_batches())

    def test_activation_waits_for_local_claim(self):
        self.assertFalse(self.store.claim(self.batch))
        with self.store._connect() as db:
            db.execute('DELETE FROM cloud_settings')
        self.assertTrue(self.store.claim(self.batch))
        self.assertFalse(self.store.enable_cloud())

    def test_login_link_replacement_revokes_old_secret(self):
        token = self.cloud.issue_login().split('#')[1]
        self.assertEqual(self.cloud.qr_response(token)['account'], 'tester')
        self.cloud.issue_login()
        self.assertIsNone(self.cloud.qr_response(token))


if __name__ == '__main__':
    unittest.main()
