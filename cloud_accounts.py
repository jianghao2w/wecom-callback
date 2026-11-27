"""Route group-approved batches to independently authenticated video accounts."""
import threading

from approval_store import ApprovalStore
from cloud_moderation import CloudModeration


class CloudAccounts:
    def __init__(self, path, default_account, anchor_id, origin, metadata, notice):
        self.path, self.origin, self.metadata, self.notice = path, origin, metadata, notice
        self.store = ApprovalStore(path)
        self.store.add_cloud_account(default_account, anchor_id, 'default')
        self.capacity = threading.BoundedSemaphore(2)
        self.workers = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        for row in self.store.cloud_accounts():
            self.get_worker(row)

    def get_worker(self, row):
        key = row['account_key']
        with self.lock:
            worker = self.workers.get(key)
            if worker is None:
                worker = CloudModeration(self.path, row['name'], row['anchor_id'], self.origin,
                    self.metadata, self.notice, account_key=key, capacity=self.capacity)
                self.workers[key] = worker
                threading.Thread(target=worker.loop, daemon=True, name='cloud-video-'+key).start()
            return worker

    @property
    def default(self):
        return self.workers['default']

    def qr_response(self, token):
        with self.lock:
            workers = list(self.workers.values())
        for worker in workers:
            result = worker.qr_response(token)
            if result:
                return result
        return None

    def choose(self, *, name=None, video_id=None):
        if not name and not video_id:
            return self.default
        accounts = self.store.cloud_accounts()
        if video_id:
            bound = self.store.cloud_video_account(video_id)
            matches = [row for row in accounts if row['account_key'] == bound or row['anchor_id'] == video_id]
            if matches:
                return self.get_worker(matches[0])
            metadata = self.metadata(video_id)
            if not metadata or str(metadata.get('id')) != video_id or not metadata.get('description') or not metadata.get('create_time'):
                raise ValueError('未找到该作品的完整登记信息，请先发送分享链接登记。')
            title = str(metadata.get('account_name') or '').strip()
            if not title:
                raise ValueError('该作品尚未取得视频号名称，请换用同一账号另一条已登记作品。')
            return self.get_worker(self.store.add_cloud_account(title, video_id))
        candidates = [row for row in accounts if row['name'] == name]
        if len(candidates) != 1:
            raise ValueError('没有唯一匹配的账号，请改用该账号已登记的作品分享链接。')
        return self.get_worker(candidates[0])

    def resolve_route(self, batch_id, payload, prior=None, metadata_cache=None):
        if not payload or payload[0].get('platform') != 'channels':
            return None
        video_id = str(payload[0]['object_id'])
        rows = self.store.cloud_accounts()
        key = prior or self.store.cloud_video_account(video_id)
        if not key:
            anchor = next((row for row in rows if row['anchor_id'] == video_id), None)
            if anchor:
                key = anchor['account_key']
            else:
                if metadata_cache is None:
                    metadata_cache = {}
                if video_id not in metadata_cache:
                    metadata_cache[video_id] = self.metadata(video_id) or {}
                name = metadata_cache[video_id].get('account_name')
                candidates = [row for row in rows if name and row['name'] == name]
                if len(candidates) == 1:
                    key = candidates[0]['account_key']
                elif not name and len(rows) == 1:
                    # Legacy registrations may lack a nickname. Exact ownership is still required.
                    key = 'default'
        if not key or (key == 'default' and not self.default.active):
            return None
        row = next((row for row in rows if row['account_key'] == key), None)
        if row and self.store.route_cloud(batch_id, key):
            worker = self.get_worker(row)
            worker.wake.set()
            return worker
        return None

    def loop(self):
        while True:
            try:
                cache = {}
                for batch_id, rows, prior in self.store.unassigned_confirmed():
                    self.resolve_route(batch_id, rows, prior, cache)
            except Exception as error:
                print(f'云端账号分流暂不可用: {type(error).__name__}', flush=True)
            self.wake.wait(15)
            self.wake.clear()
