import csv
import io
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen, Request, build_opener, HTTPRedirectHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class ApplicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = app.DATA_PATH
        app.DATA_PATH = Path(self.temp.name) / 'feedback.db'
        app.init_database()
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.FeedbackHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        app.DATA_PATH = self.old_path
        self.temp.cleanup()

    def get(self, path):
        with urlopen(self.base + path) as response:
            return response.read().decode('utf-8-sig')

    def post(self, **overrides):
        data = dict(course='測試課程', comment='教材範例很清楚', **{k: '4' for k, _, _ in app.DIMENSIONS})
        data.update(overrides)
        request = Request(self.base + '/submit', data=urlencode(data).encode())
        try:
            return build_opener(NoRedirect).open(request)
        except HTTPError as response:
            return response

    def test_pages_and_errors(self):
        self.assertIn('學習回聲', self.get('/'))
        self.assertNotIn('@@', self.get('/'))
        self.assertIn('目前還沒有回饋', self.get('/dashboard'))
        self.assertIn(':root', self.get('/static/style.css'))
        self.assertEqual('ok', self.get('/health'))
        with self.assertRaises(HTTPError) as error:
            self.get('/missing')
        self.assertEqual(404, error.exception.code)

    def test_submission_threshold_and_csv(self):
        for _ in range(4):
            response = self.post()
            self.assertEqual(303, response.code)
            self.assertEqual('/?submitted=1', response.headers['Location'])
            response.close()
        page = self.get('/dashboard?'+urlencode({'course':'測試課程'}))
        self.assertIn('整體分析尚未開放', page)
        self.assertEqual(1, len(list(csv.reader(io.StringIO(self.get('/export.csv'))))))
        response = self.post()
        self.assertEqual(303, response.code)
        response.close()
        page = self.get('/dashboard')
        self.assertIn('4.0', page)
        self.assertIn('5 則提及', page)
        self.assertNotIn('教材範例很清楚', page)
        export = list(csv.reader(io.StringIO(self.get('/export.csv'))))
        self.assertEqual(['測試課程', '5', '4.0'], export[1][:3])
        # A new connection reads data committed by the request handler.
        self.assertEqual(5, len(app.fetch_responses()))

    def test_invalid_inputs_and_escaping(self):
        for values in ({'clarity':'6'}, {'course':''}, {'comment':'a@school.edu'}, {'comment':'x'*1001}):
            response = self.post(**values)
            self.assertEqual(400, response.code)
            response.close()
        response = self.post(course='<script>alert(1)</script>', clarity='bad')
        self.assertIn('&lt;script&gt;', response.read().decode())
        response.close()
        self.assertEqual([], app.fetch_responses())
        self.assertEqual("'=SUM(1)", app.csv_safe('=SUM(1)'))


if __name__ == '__main__':
    unittest.main()
