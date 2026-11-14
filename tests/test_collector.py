import datetime as dt
import unittest
from unittest.mock import patch
import collector

NOW = dt.datetime(2026, 11, 14, tzinfo=dt.timezone.utc)
SOURCE = {'id':'one','name':'Source','url':'https://example.com/feed','allowed_hosts':['example.com']}
RSS = b'<rss><channel><item><title>One &amp; two</title><link>https://example.com/a?utm_source=x</link><pubDate>Fri, 13 Nov 2026 12:00:00 GMT</pubDate></item></channel></rss>'
ATOM = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom</title><link href="https://example.com/b"/><updated>2026-11-13T12:00:00Z</updated></entry></feed>'

class CollectorTests(unittest.TestCase):
    def test_parses_rss_and_atom(self):
        self.assertEqual(collector.parse(RSS, SOURCE)[0]['url'], 'https://example.com/a')
        self.assertEqual(collector.parse(ATOM, SOURCE)[0]['title'], 'Atom')
    def test_rejects_entities(self):
        with self.assertRaises(ValueError): collector.parse(b'<!DOCTYPE rss [<!ENTITY x "x">]><rss/>', SOURCE)
    def test_invalid_and_old_dates_are_excluded(self):
        self.assertIsNone(collector.iso('not a date'))
        result = collector.collect([SOURCE], {}, NOW, lambda _: collector.parse(RSS, SOURCE))
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(len(collector.collect([SOURCE], result, NOW + dt.timedelta(days=50), lambda _: [])["items"]), 0)
    def test_failed_run_preserves_last_success_and_items(self):
        old = collector.collect([SOURCE], {}, NOW, lambda _: collector.parse(RSS, SOURCE))
        def fail(_): raise OSError('offline')
        result = collector.collect([SOURCE], old, NOW + dt.timedelta(days=1), fail)
        self.assertEqual(result['status'], 'failed')
        self.assertNotEqual(result['generated_at'], old['generated_at'])
        self.assertEqual(result['last_successful_at'], old['last_successful_at'])
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['sources'][0]['status'], 'failed')
    def test_partial_and_duplicate(self):
        other = dict(SOURCE, id='two')
        def fetch(s):
            if s['id'] == 'two': raise OSError('offline')
            return collector.parse(RSS, s) * 2
        result = collector.collect([SOURCE, other], {}, NOW, fetch)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['items']), 1)
    def test_no_non_http_links(self):
        self.assertIsNone(collector.canonical('javascript:alert(1)'))
        self.assertIsNone(collector.canonical('https://user:pass@example.com/'))
    def test_retry_after_network_error(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return RSS
        class Opener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                if self.calls == 1: raise OSError('temporary')
                return Response()
        opener = Opener()
        with patch.object(collector.urllib.request, 'build_opener', return_value=opener), patch.object(collector.time, 'sleep'):
            self.assertEqual(len(collector.fetch(SOURCE)), 1)
        self.assertEqual(opener.calls, 2)

if __name__ == '__main__': unittest.main()

