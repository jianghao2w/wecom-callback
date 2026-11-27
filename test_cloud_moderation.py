import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cloud_moderation import CloudModeration, LoginRequired, MatchError
from cloud_accounts import CloudAccounts


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

    def test_armed_enrollment_still_requires_verified_login(self):
        self.cloud.arm_enrollment()
        self.assertTrue(self.store.cloud_enrollment_armed())
        self.assertFalse(self.cloud.enable_if_verified())
        self.cloud.publish_status('ready')
        self.assertTrue(self.cloud.enable_if_verified())
        self.assertFalse(self.store.cloud_enrollment_armed())


class CloudAccountRoutingTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.metadata = {
            'one': {'id':'one','account_name':'账号甲','description':'甲作品','create_time':123},
            'two': {'id':'two','account_name':'账号乙','description':'乙作品','create_time':123},
            'two-more': {'id':'two-more','account_name':'账号乙','description':'乙新作品','create_time':124},
        }
        with patch('cloud_accounts.threading.Thread'):
            self.directory = CloudAccounts(str(Path(temp.name) / 'test.db'), '账号甲','one',
                'https://example.test', self.metadata.get, Mock())
            self.other = self.directory.choose(video_id='two')
        self.first = self.directory.default
        self.store = self.directory.store
        for worker in (self.first, self.other):
            worker.publish_status('ready')
            self.assertTrue(worker.enable_if_verified())

    def create(self, video):
        comments = [{'platform':'channels','object_id':video,'comment_id':'comment','content':'text'}]
        key = self.store.create('group',comments)
        self.store.confirm(key,'group','operator')
        return key, comments

    def test_profiles_and_claims_are_independent(self):
        self.assertNotEqual(self.first.profile, self.other.profile)
        a, comments_a = self.create('one')
        b, comments_b = self.create('two-more')
        self.assertIs(self.directory.resolve_route(a, comments_a), self.first)
        self.assertIs(self.directory.resolve_route(b, comments_b), self.other)
        self.assertIsNone(self.store.claim_cloud(b, self.first.account_key, routed=True))
        self.assertFalse(self.store.claim(b))
        self.assertTrue(self.store.claim_cloud(b, self.other.account_key, routed=True))
        self.store.wait_cloud(b)
        self.store.resume_cloud(self.first.account_key)
        self.assertEqual(self.store.get(b)['state'], 'waiting_login')
        self.store.resume_cloud(self.other.account_key)
        self.assertEqual(self.store.get(b)['state'], 'confirmed')

    def test_duplicate_label_does_not_route_unbound_video(self):
        self.store.add_cloud_account('账号乙', 'third-anchor')
        b, comments_b = self.create('two-more')
        self.assertIsNone(self.directory.resolve_route(b, comments_b))
        self.assertEqual(self.store.get(b)['state'], 'confirmed')

    def test_wrong_account_holds_without_attempting_delete(self):
        batch, comments = self.create('two-more')
        self.directory.resolve_route(batch, comments)
        browser = Mock()
        browser.export_id.side_effect = MatchError
        self.other.process(browser, batch)
        browser.delete.assert_not_called()
        self.assertEqual(self.store.get(batch)['state'], 'waiting_owner')
        self.store.requeue_cloud_owner('two-more', self.first.account_key)
        self.assertEqual(self.store.confirmed_batches(self.first.account_key), [batch])


if __name__ == '__main__':
    unittest.main()
