import concurrent.futures
import http.client
import io
import json
import os
import re
import socket
import ssl
import threading
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

# main requires DATABASE_URL at import; these tests never connect to it.
os.environ.setdefault("DATABASE_URL", "postgresql://unused@localhost/unused")
import main


def login_response(token):
    return 200, json.dumps({"response": {"access_token": token}}).encode()


HISTORY = [{"purchase_date": "2026-09-01", "purchase_price": "100", "items": []}]
HISTORY_RESPONSE = 200, json.dumps({"result": "success", "response": HISTORY}).encode()
INVALID_TOKEN_RESPONSE = 400, json.dumps({
    "result": "request error",
    "error": {"error_code": "a001", "error_message": "invalid token error"},
}).encode()


class OKCardTokenCacheTests(unittest.TestCase):
    def setUp(self):
        main._okcard_tokens.clear()
        self.addCleanup(main._okcard_tokens.clear)

    def test_sync_fetch_reuses_token(self):
        with patch.object(main, "_okcard_request", side_effect=[
            login_response("cached"), HISTORY_RESPONSE, HISTORY_RESPONSE,
        ]) as request:
            first = main.okcard_cached_history("user", "pass")
            second = main.okcard_cached_history("user", "pass")

        self.assertEqual(first, HISTORY)
        self.assertEqual(second, HISTORY)
        self.assertEqual(request.call_count, 3)
        for call in request.call_args_list[1:]:
            self.assertEqual(call.kwargs["headers"]["Idtoken"], "cached")

    def test_rejection_refreshes_token_and_reuses_replacement(self):
        for rejection in [(401, b"unauthorized"), (403, b"forbidden"),
                          (200, b'{"result":"failure"}'), INVALID_TOKEN_RESPONSE]:
            with self.subTest(rejection=rejection):
                main._okcard_tokens.clear()
                with patch.object(main, "_okcard_request", side_effect=[
                    login_response("old"), rejection,
                    login_response("new"), HISTORY_RESPONSE, HISTORY_RESPONSE,
                ]) as request:
                    self.assertEqual(main.okcard_cached_history("user", "pass"), HISTORY)
                    self.assertEqual(main.okcard_cached_history("user", "pass"), HISTORY)
                self.assertEqual(request.call_count, 5)
                self.assertEqual(request.call_args_list[3].kwargs["headers"]["Idtoken"], "new")
                self.assertEqual(request.call_args_list[4].kwargs["headers"]["Idtoken"], "new")

    def test_second_rejection_stops_retry_and_evicts_token(self):
        with patch.object(main, "_okcard_request", side_effect=[
            login_response("old"), (401, b"expired"),
            login_response("new"), (401, b"expired"),
        ]) as request:
            with self.assertRaises(main.HTTPException) as error:
                main.okcard_cached_history("user", "pass")
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(main._okcard_tokens, {})

    def test_repeated_http_400_invalid_token_stops_and_allows_fresh_login(self):
        with patch.object(main, "_okcard_request", side_effect=[
            login_response("old"), INVALID_TOKEN_RESPONSE,
            login_response("new"), INVALID_TOKEN_RESPONSE,
            login_response("fresh"), HISTORY_RESPONSE,
        ]) as request:
            with self.assertRaises(main.HTTPException) as error:
                main.okcard_cached_history("user", "pass")
            self.assertEqual(error.exception.status_code, 502)
            self.assertIn("a001", error.exception.detail)
            self.assertEqual(request.call_count, 4)
            self.assertEqual(main._okcard_tokens, {})
            self.assertEqual(main.okcard_cached_history("user", "pass"), HISTORY)
        self.assertEqual(request.call_count, 6)
        self.assertEqual(main._okcard_tokens[("user", "pass")], "fresh")

    def test_non_auth_failures_do_not_refresh(self):
        failures = [(500, b'{"result":"failure"}'), (429, b"rate limited"),
                    (400, b'{"result":"request error","error":{"error_code":"other"}}'),
                    (400, b'{"result":"request error","error":null}'),
                    (400, b'{"result":"request error","error":"invalid"}'),
                    (400, b"invalid json"),
                    (429, INVALID_TOKEN_RESPONSE[1]),
                    (500, INVALID_TOKEN_RESPONSE[1]),
                    (200, b"invalid json"), (200, b"{}"),
                    (200, b'{"result":"success","response":null}'),
                    urllib.error.URLError("connection failed")]
        for failure in failures:
            with self.subTest(failure=failure):
                main._okcard_tokens.clear()
                with patch.object(main, "_okcard_request", side_effect=[
                    login_response("cached"), failure,
                ]) as request:
                    with self.assertRaises((main.HTTPException, urllib.error.URLError)):
                        main.okcard_cached_history("user", "pass")
                self.assertEqual(request.call_count, 2)
                self.assertEqual(main._okcard_tokens[("user", "pass")], "cached")

    def test_failed_login_is_not_cached(self):
        for response in [(401, b"invalid credentials"),
                         (500, login_response("bad")[1]),
                         (200, b'{"response":{"access_token":123}}')]:
            with self.subTest(response=response):
                with patch.object(main, "_okcard_request", return_value=response) as request:
                    with self.assertRaises(main.HTTPException):
                        main.okcard_cached_history("user", "pass")
                self.assertEqual(request.call_count, 1)
                self.assertEqual(main._okcard_tokens, {})

    def test_credentials_have_separate_tokens(self):
        with patch.object(main, "okcard_login", side_effect=["a", "b", "c"]) as login:
            self.assertEqual(main._okcard_get_token("alice", "pass"), "a")
            self.assertEqual(main._okcard_get_token("bob", "pass"), "b")
            self.assertEqual(main._okcard_get_token("alice", "changed"), "c")
            self.assertEqual(main._okcard_get_token("alice", "pass"), "a")
        self.assertEqual(login.call_count, 3)

    def test_concurrent_requests_login_once(self):
        start = threading.Barrier(8)

        def fetch(_):
            start.wait(timeout=5)
            return main.okcard_cached_history("user", "pass")

        with patch.object(main, "okcard_login", return_value="cached") as login, \
                patch.object(main, "okcard_fetch_history", return_value=HISTORY):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(fetch, range(8)))
        self.assertEqual(results, [HISTORY] * 8)
        login.assert_called_once_with("user", "pass")

    def test_concurrent_rejections_refresh_once(self):
        main._okcard_tokens[("user", "pass")] = "old"
        rejected = threading.Barrier(8)

        def history(token):
            if token == "old":
                rejected.wait(timeout=5)
                raise main._OKCardTokenRejected(status_code=502, detail="expired")
            return HISTORY

        with patch.object(main, "okcard_login", return_value="new") as login, \
                patch.object(main, "okcard_fetch_history", side_effect=history):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: main.okcard_cached_history("user", "pass"), range(8)))
        self.assertEqual(results, [HISTORY] * 8)
        login.assert_called_once_with("user", "pass")
        self.assertEqual(main._okcard_tokens[("user", "pass")], "new")


class OKCardRequestTests(unittest.TestCase):
    def response(self, read):
        response = MagicMock(status=200)
        response.read.side_effect = read
        urlopen = MagicMock()
        urlopen.return_value.__enter__.return_value = response
        return urlopen

    def test_body_read_failures_become_url_errors(self):
        for error in [http.client.IncompleteRead(b"partial"), ConnectionResetError(),
                      http.client.RemoteDisconnected(), ssl.SSLError(), socket.timeout()]:
            with self.subTest(error=error), \
                    patch("urllib.request.urlopen", self.response(error)):
                with self.assertRaises(urllib.error.URLError) as caught:
                    main._okcard_request("https://example.invalid")
                self.assertIs(caught.exception.reason, error)

    def test_connect_failures_become_url_errors(self):
        for error in [urllib.error.URLError("refused"), ConnectionRefusedError(), socket.timeout()]:
            with self.subTest(error=error), patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(urllib.error.URLError):
                    main._okcard_request("https://example.invalid")

    def test_http_error_returns_body_unless_body_read_fails(self):
        def http_error(body):
            return urllib.error.HTTPError("https://example.invalid", 400, "Bad", {}, body)
        with patch("urllib.request.urlopen", side_effect=http_error(io.BytesIO(b"denied"))):
            self.assertEqual(main._okcard_request("https://example.invalid"), (400, b"denied"))
        broken = MagicMock()
        broken.read.side_effect = http.client.IncompleteRead(b"")
        with patch("urllib.request.urlopen", side_effect=http_error(broken)):
            with self.assertRaises(urllib.error.URLError):
                main._okcard_request("https://example.invalid")

    def test_success_returns_status_and_body(self):
        with patch("urllib.request.urlopen", self.response([b"ok"])):
            self.assertEqual(main._okcard_request("https://example.invalid"), (200, b"ok"))


class OKCardHtmlTests(unittest.TestCase):
    def test_data_block_cannot_escape_script(self):
        names = ["</script><b>x</b>", "<!--<script>", "a<b", "</SCRIPT>"]
        purchases = [{"store_name": name, "purchase_price": 1, "purchase_date": "2026-09-01",
                      "items": [{"item_name": name, "item_count": 1, "price": 1}]} for name in names]
        html = main.render_okcard_html("2026-09", purchases)
        block = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.DOTALL).group(1)
        self.assertNotIn("<", block)
        data = json.loads(block)
        self.assertEqual([p["store_name"] for p in data["purchases"]], names)
        self.assertEqual([p["items"][0]["item_name"] for p in data["purchases"]], names)


class ExchangeRateRouteTests(unittest.TestCase):
    def test_paging_parameters_reject_negative_values(self):
        params = main.app.openapi()["paths"]["/bocd_exchange_rates"]["get"]["parameters"]
        schema = {p["name"]: p["schema"] for p in params}
        self.assertEqual(schema["offset"].get("minimum"), 0)
        self.assertEqual((schema["limit"].get("minimum"), schema["limit"].get("maximum")), (1, 100))


if __name__ == "__main__":
    unittest.main()
