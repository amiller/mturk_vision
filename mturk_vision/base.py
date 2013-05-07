import base64
import uuid
import json
import time
import gevent
import gevent.coros
import cgi


class UserNotFinishedException(Exception):
    """Exception thrown when a user isn't finished"""


class AMTManager(object):

    def __init__(self, mode, num_tasks, index_path, config_path, task_key,
                 users_db, response_db, state_db, key_to_path_db, path_to_key_db,
                 data_source, secret=None, **kw):
        self.prefix = task_key + ':'
        self.mode = mode
        self.num_tasks = num_tasks
        self.users_db = users_db
        self.response_db = response_db
        self.state_db = state_db
        self.key_to_path_db = key_to_path_db
        self.path_to_key_db = path_to_key_db
        self.dbs = [self.users_db, self.response_db, self.state_db,
                    self.key_to_path_db, self.path_to_key_db]
        self.index_path = index_path
        self.config_path = config_path
        self.data_source = data_source
        self._make_secret(secret)
        self.data_source_lock = gevent.coros.RLock()
        self.lock_expire = 10
        if 'instructions' in kw:
            self.instructions = '<pre>%s</pre>' % cgi.escape(kw['instructions'])

    @property
    def index(self):
        # Reload each time to simplify development
        return open(self.index_path).read()

    @property
    def config(self):
        # Reload each time to simplify development
        config = open(self.config_path).read()
        if hasattr(self, 'instructions'):
            config_js = json.loads(config)
            config_js['instructions'] = self.instructions
            config = json.dumps(config_js)
        return config

    def _flush_db(self, db, keep_rows=None):
        keys = db.keys(self.prefix + '*')
        if keep_rows:
            keep_rows = set(self.prefix + x for x in keep_rows)
            keys = [x for x in keys if x not in keep_rows]
        print('Deleting [%d] keys' % len(keys))
        if keys:
            db.delete(*keys)

    def add_row(self, row, priority=0):
        data_lock, state_db, key_to_path_db, path_to_key_db = self.data_lock()
        columns = self.data_source.columns(row)
        self._add_row(row, columns, state_db, key_to_path_db, path_to_key_db, priority)
        self.data_unlock(data_lock, state_db, key_to_path_db, path_to_key_db)

    def _add_row(self, row, columns, state_db, key_to_path_db, path_to_key_db, priority=0):
        if not self.required_columns.issubset(columns):
            return
        state_db.zadd(self.prefix + 'rows', priority, row)
        for column in columns:
            row_column_code = self.row_column_encode(row, column)
            key = self.urlsafe_uuid()
            path_to_key_db.set(self.prefix + row_column_code, key)
            key_to_path_db.set(self.prefix + key, row_column_code)

    def data_lock(self):
        locked = 0
        data_lock = self.urlsafe_uuid()
        while 1:
            locked = self.state_db.set(self.prefix + 'data_lock', data_lock, nx=True, ex=self.lock_expire)
            if locked:
                break
            time.sleep(1.)
        print('Locked[%s]' % self.prefix)
        state_db = self.state_db.pipeline()
        key_to_path_db = self.key_to_path_db.pipeline()
        path_to_key_db = self.path_to_key_db.pipeline()
        return data_lock, state_db, key_to_path_db, path_to_key_db

    def get_row(self, user):
        # TODO: Do this in lua
        num = 1
        while 1:
            rows = self.state_db.zrevrangebyscore(self.prefix + 'rows', float('inf'), float('-inf'), num=num, start=0)
            for row in rows:
                if not self.state_db.sismember(self.prefix + 'seen:' + user, row):
                    self.state_db.sadd(self.prefix + 'seen:' + user, row)
                    self.state_db.zincrby(self.prefix + 'rows', row, -1)
                    return row
            if num >= self.num_tasks:
                return
            num = min(num * 2, self.num_tasks)

    def data_locked(self, data_lock):
        return self.state_db.get(self.prefix + 'data_lock') == data_lock

    def data_lock_extend(self):
        self.state_db.expire(self.prefix + 'data_lock', self.lock_expire)

    def data_unlock(self, data_lock, state_db, key_to_path_db, path_to_key_db):
        # TODO: Replace with a lua script run on Redis
        self.data_lock_extend()
        if self.state_db.get(self.prefix + 'data_lock') == data_lock:
            state_db.execute()
            key_to_path_db.execute()
            path_to_key_db.execute()
            self.state_db.delete(self.prefix + 'data_lock')
        else:
            print('Could not unlock[%s]' % self.prefix)

    def reset(self):
        data_lock, state_db, key_to_path_db, path_to_key_db = self.data_lock()
        self._flush_db(self.state_db, keep_rows=['data_lock'])
        self._flush_db(self.key_to_path_db)
        self._flush_db(self.path_to_key_db)
        st = time.time()
        for row, columns in self.data_source.row_columns():
            columns = set(columns)
            if (time.time() - st) * 2 >= self.lock_expire:
                self.data_lock_extend()
            print((repr(row), repr(columns)))
            self._add_row(row, columns, state_db, key_to_path_db, path_to_key_db)
        self.data_unlock(data_lock, state_db, key_to_path_db, path_to_key_db)

    def _make_secret(self, secret=None):
        """Make secret used for admin functions"""
        if secret is None:
            self.secret = self.urlsafe_uuid()
        else:
            self.secret = secret
        print('Results URL:  /admin/%s/results.js' % self.secret)
        print('Users URL:  /admin/%s/users.js' % self.secret)
        print('Quit URL:  /admin/%s/stop' % self.secret)

    def urlsafe_uuid(self):
        """Make a urlsafe uuid"""
        return base64.urlsafe_b64encode(uuid.uuid4().bytes)[:-2]

    def user(self, bottle_request):
        """Make a new user entry"""
        user_id = self.urlsafe_uuid()
        out = {'query_string': bottle_request.query_string,
               'remote_addr': bottle_request.remote_addr,
               'tasks_finished': 0,
               'tasks_viewed': 0,
               'start_time': time.time()}
        out.update(dict(bottle_request.query))
        self.users_db.hmset(self.prefix + user_id, out)
        return {"user_id": user_id}

    def row_column_encode(self, row, column):
        return base64.b64encode(row) + ' ' + base64.b64encode(column)

    def row_column_decode(self, row_column_code):
        return map(base64.b64decode, row_column_code.split(' ', 1))

    def read_data(self, data_key):
        """Get data from disk corresponding to data_key

        Args:
            data_key: String data key

        Raises:
            KeyError: Data key not in DB
        """
        path = self.key_to_path_db.get(self.prefix + data_key)
        if path is None:
            raise KeyError
        row, column = self.row_column_decode(path)
        return self.read_row_column(row, column)

    def read_row_column(self, row, column):
        try:
            self.data_source_lock.acquire()
            return self.data_source.value(row, column)
        finally:
            self.data_source_lock.release()

    def admin_users(self, secret):
        """Return contents of users_db"""
        if secret == self.secret:
            return {k: self.users_db.hgetall(k) for k in self.users_db.keys(self.prefix + '*')}

    def _user_finished(self, user_id, force=False):
        """Check if the user has finished their tasks, if so output the return dictionary.

        Updates tasks_viewed if we aren't finished.

        Args:
            user_id: User ID string

        Returns:
            A dictionary of the result.

        Raises:
            UserNotFinishedException: User hasn't finished their tasks
        """
        cur_user = self.users_db.hgetall(self.prefix + user_id)
        if int(cur_user['tasks_finished']) >= self.num_tasks or force:
            end_time = time.time()
            self.users_db.hset(self.prefix + user_id, 'end_time', end_time)
            if self.mode == 'amt':
                pct_finished = int(cur_user['tasks_finished']) / float(cur_user['tasks_viewed'])
                query_string = '&'.join(['%s=%s' % x for x in [('assignmentId', cur_user.get('assignmentId', 'NoId')),
                                                               ('pct_finished', pct_finished),
                                                               ('tasks_finished', cur_user['tasks_finished']),
                                                               ('tasks_viewed', cur_user['tasks_viewed']),
                                                               ('time_taken', end_time - float(cur_user['start_time']))]])
                return {'submit_url': '%s/mturk/externalSubmit?%s' % (cur_user.get('turkSubmitTo', 'http://www.mturk.com'), query_string)}
            else:
                return {'submit_url': 'data:,Done%20annotating'}
        self.users_db.hincrby(self.prefix + user_id, 'tasks_viewed')
        raise UserNotFinishedException

    def result(self, user_id):
        self.users_db.hincrby(self.prefix + user_id, 'tasks_finished')
