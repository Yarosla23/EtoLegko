"""Test the CLI, downloads, and statistics without external internet access."""

import argparse
import os
import runpy
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.client import HTTPException, IncompleteRead
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError, URLError

from etolegko import cli, downloader, metrics

SCRIPT = Path(__file__).resolve().parents[1] / "speedtest.py"


class CliTests(unittest.TestCase):
    """Check command-line validation and import behavior."""

    def test_help_exits_without_network(self) -> None:
        output = StringIO()
        with patch("etolegko.downloader.urlopen") as open_url, redirect_stdout(output):
            with self.assertRaises(SystemExit) as caught:
                cli.parse_args(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("url", output.getvalue())
        open_url.assert_not_called()

    def test_invalid_arguments_exit_before_network(self) -> None:
        cases = [
            [],
            ["--unknown"],
            ["example.com/file"],
            ["file:///tmp/file"],
            ["ftp://example.com/file"],
            ["https:///file"],
            ["http://localhost:0/file"],
            ["https://example.com:70000/file"],
            ["https://example.com:invalid/file"],
            ["http://[invalid/file"],
            ["https://example.com/with space"],
            ["https://example.com/\nfile"],
            ["https://example.com/файл"],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                errors = StringIO()
                with patch("etolegko.downloader.urlopen") as open_url:
                    with (
                        redirect_stderr(errors),
                        self.assertRaises(SystemExit) as caught,
                    ):
                        cli.parse_args(arguments)
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("error:", errors.getvalue())
                open_url.assert_not_called()

    def test_valid_url_is_preserved(self) -> None:
        for url in [
            "https://example.com/file?size=100&format=raw",
            "http://127.0.0.1:8080/file",
            "http://[::1]:8080/file",
            "https://example.com/%D1%84%D0%B0%D0%B9%D0%BB",
        ]:
            with self.subTest(url=url):
                self.assertEqual(cli.parse_args([url]).url, url)

    def test_import_has_no_cli_or_network_side_effects(self) -> None:
        with (
            patch.object(argparse.ArgumentParser, "parse_args") as parse,
            patch("urllib.request.urlopen") as open_url,
        ):
            runpy.run_path(str(SCRIPT))
        parse.assert_not_called()
        open_url.assert_not_called()


def mock_response(
    chunks: list[bytes], headers: dict[str, str] | None = None
) -> MagicMock:
    """Build a context-managed response whose reads and closure are observable."""
    response = MagicMock()
    response.status = 200
    response.getheader.side_effect = (headers or {}).get
    response.read.side_effect = chunks
    response.__enter__.return_value = response
    return response


class DownloadTests(unittest.TestCase):
    """Check streaming, timing, cleanup, and sequential downloads."""

    def test_full_body_is_read_between_clock_samples(self) -> None:
        response = mock_response([b"abc", b"de", b""], {"Content-Length": "5"})
        events = []
        samples = iter([10.0, 10.5])
        chunks = iter([b"abc", b"de", b""])

        def clock() -> float:
            events.append("clock")
            return next(samples)

        def read(size: int) -> bytes:
            self.assertEqual(size, downloader.CHUNK_SIZE_BYTES)
            events.append("read")
            return next(chunks)

        def open_response(url: str, timeout: int) -> MagicMock:
            self.assertEqual(url, "https://example.com/file")
            self.assertEqual(timeout, downloader.TIMEOUT_SECONDS)
            events.append("open")
            return response

        response.read.side_effect = read
        response.__exit__.side_effect = lambda *args: events.append("close")
        with (
            patch("etolegko.downloader.urlopen", side_effect=open_response),
            patch("etolegko.downloader.perf_counter", side_effect=clock),
        ):
            result = downloader.download_once("https://example.com/file")
        self.assertEqual(result, metrics.Measurement(0.5, 5))
        self.assertEqual(
            events, ["clock", "open", "read", "read", "read", "clock", "close"]
        )

    def test_response_without_content_length_is_supported(self) -> None:
        response = mock_response([b"abc", b""])
        with (
            patch("etolegko.downloader.urlopen", return_value=response),
            patch("etolegko.downloader.perf_counter", side_effect=[1.0, 2.0]),
        ):
            self.assertEqual(
                downloader.download_once("https://example.com/file"),
                metrics.Measurement(1.0, 3),
            )

    def test_premature_eof_closes_response_and_raises(self) -> None:
        response = mock_response([b"abc", b""], {"Content-Length": "10"})
        with patch("etolegko.downloader.urlopen", return_value=response):
            with self.assertRaisesRegex(HTTPException, "received 3 of 10"):
                downloader.download_once("https://example.com/file")
        response.__exit__.assert_called_once()

    def test_read_error_closes_response_and_propagates(self) -> None:
        response = mock_response([])
        response.read.side_effect = TimeoutError("read timed out")
        with patch("etolegko.downloader.urlopen", return_value=response):
            with self.assertRaises(TimeoutError):
                downloader.download_once("https://example.com/file")
        response.__exit__.assert_called_once()

    def test_http_error_response_is_closed(self) -> None:
        body = MagicMock()
        error = HTTPError("https://example.com/file", 404, "Not Found", {}, body)
        with patch("etolegko.downloader.urlopen", side_effect=error):
            with self.assertRaises(HTTPError):
                downloader.download_once("https://example.com/file")
        body.close.assert_called_once()

    def test_chunked_response_takes_precedence_over_content_length(self) -> None:
        response = mock_response(
            [b"abc", b""], {"Content-Length": "100", "Transfer-Encoding": "chunked"}
        )
        with patch("etolegko.downloader.urlopen", return_value=response):
            self.assertEqual(
                downloader.download_once("https://example.com/file").downloaded_bytes, 3
            )

    def test_unsuccessful_final_status_is_rejected(self) -> None:
        for status in [None, 302]:
            with self.subTest(status=status):
                response = mock_response([b""])
                response.status = status
                with patch("etolegko.downloader.urlopen", return_value=response):
                    with self.assertRaises(URLError):
                        downloader.download_once("https://example.com/file")
                response.__exit__.assert_called_once()

    def test_ten_responses_are_fully_read_and_closed_in_order(self) -> None:
        responses = [mock_response([b"a", b"bc", b""]) for _ in range(10)]
        remaining = iter(responses)
        opened = []

        def open_response(url: str, timeout: int) -> MagicMock:
            if opened:
                opened[-1].__exit__.assert_called_once()
                self.assertEqual(
                    opened[-1].read.call_args_list,
                    [call(downloader.CHUNK_SIZE_BYTES)] * 3,
                )
            response = next(remaining)
            opened.append(response)
            return response

        with (
            patch("etolegko.downloader.urlopen", side_effect=open_response) as open_url,
            patch("etolegko.downloader.perf_counter", side_effect=range(20)),
        ):
            results = downloader.measure_downloads("https://example.com/file")
        self.assertEqual(open_url.call_count, 10)
        self.assertEqual(results, [metrics.Measurement(1.0, 3)] * 10)
        responses[-1].__exit__.assert_called_once()
        self.assertEqual(responses[-1].read.call_count, 3)

    def test_measurement_stops_at_first_failed_download(self) -> None:
        with patch(
            "etolegko.downloader.download_once",
            side_effect=[metrics.Measurement(1.0, 3), URLError("connection failed")],
        ) as download:
            with self.assertRaises(URLError):
                downloader.measure_downloads("https://example.com/file")
        self.assertEqual(download.call_count, 2)


class MainTests(unittest.TestCase):
    """Check user-visible results and failure behavior."""

    def test_successful_report_matches_the_contract(self) -> None:
        output, errors = StringIO(), StringIO()
        with (
            patch("sys.argv", ["speedtest.py", "https://example.com/file"]),
            patch(
                "etolegko.cli.measure_downloads",
                return_value=[metrics.Measurement(0.5, 1_048_576)] * 10,
            ),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            result = cli.main()
        self.assertEqual(result, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(
            output.getvalue(),
            "Requests: 10\n"
            "Average request time: 0.500 s\n"
            "Downloaded: 10485760 bytes\n"
            "Download speed: 2.000 MiB/s\n",
        )

    def test_invalid_measurements_produce_failure_without_report(self) -> None:
        output, errors = StringIO(), StringIO()
        with (
            patch("sys.argv", ["speedtest.py", "https://example.com/file"]),
            patch(
                "etolegko.cli.measure_downloads",
                return_value=[metrics.Measurement(0.0, 1)] * 10,
            ),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            result = cli.main()
        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("time must be positive", errors.getvalue())

    def test_network_errors_produce_only_stderr_and_failure_code(self) -> None:
        for error in [
            URLError("connection failed"),
            TimeoutError("timed out"),
            IncompleteRead(b"abc", 7),
            HTTPException("invalid response"),
        ]:
            with self.subTest(error=error):
                output, errors = StringIO(), StringIO()
                with (
                    patch("sys.argv", ["speedtest.py", "https://example.com/file"]),
                    patch("etolegko.cli.measure_downloads", side_effect=error),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    result = cli.main()
                self.assertEqual(result, 1)
                self.assertEqual(output.getvalue(), "")
                self.assertIn("Error:", errors.getvalue())
                self.assertNotIn("Traceback", errors.getvalue())

    def test_keyboard_interrupt_returns_130_without_report(self) -> None:
        output, errors = StringIO(), StringIO()
        with (
            patch("sys.argv", ["speedtest.py", "https://example.com/file"]),
            patch("etolegko.cli.measure_downloads", side_effect=KeyboardInterrupt),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            result = cli.main()
        self.assertEqual(result, 130)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("interrupted", errors.getvalue())


class StatisticsTests(unittest.TestCase):
    """Check aggregate throughput independently of networking and CLI output."""

    def test_known_measurements(self) -> None:
        statistics = metrics.calculate_statistics(
            [metrics.Measurement(0.5, 1_048_576)] * 10
        )
        self.assertEqual(statistics, metrics.Statistics(10, 0.5, 10_485_760, 2.0))

    def test_throughput_uses_total_bytes_and_time(self) -> None:
        measurements = [metrics.Measurement(1.0, 1_048_576)] * 5 + [
            metrics.Measurement(3.0, 2_097_152)
        ] * 5
        statistics = metrics.calculate_statistics(measurements)
        self.assertEqual(statistics.average_seconds, 2.0)
        self.assertEqual(statistics.total_bytes, 15_728_640)
        self.assertEqual(statistics.speed_mib_s, 0.75)

    def test_empty_bodies_have_zero_speed(self) -> None:
        statistics = metrics.calculate_statistics([metrics.Measurement(0.5, 0)] * 10)
        self.assertEqual(statistics.total_bytes, 0)
        self.assertEqual(statistics.speed_mib_s, 0.0)

    def test_nonpositive_total_time_is_rejected(self) -> None:
        for elapsed_seconds in [0.0, -1.0]:
            with self.subTest(elapsed_seconds=elapsed_seconds):
                with self.assertRaisesRegex(ValueError, "time must be positive"):
                    metrics.calculate_statistics(
                        [metrics.Measurement(elapsed_seconds, 1)] * 10
                    )

    def test_incomplete_measurement_set_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "10 complete downloads"):
            metrics.calculate_statistics([metrics.Measurement(1.0, 1)] * 9)


class IntegrationTests(unittest.TestCase):
    """Run the real CLI against a finite local HTTP server."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.requests: dict[str, int] = {}
        cls.body = bytes(range(256)) * 300

        class Handler(BaseHTTPRequestHandler):
            """Serve full, redirected, chunked, and intentionally broken responses."""

            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                """Return the response selected by the local test route."""
                cls.requests[self.path] = cls.requests.get(self.path, 0) + 1
                status = {"/redirect": 302, "/missing": 404, "/empty": 204}.get(
                    self.path, 200
                )
                body = b"" if status != 200 else cls.body
                self.send_response(status)
                self.send_header("Connection", "close")
                if self.path == "/redirect":
                    self.send_header("Location", "/data")
                if self.path == "/chunked":
                    self.send_header("Transfer-Encoding", "chunked")
                elif self.path != "/no-length":
                    expected_bytes = len(body) + (
                        10 if self.path == "/truncated" else 0
                    )
                    self.send_header("Content-Length", str(expected_bytes))
                self.end_headers()
                for offset in range(0, len(body), 4096):
                    chunk = body[offset : offset + 4096]
                    if self.path == "/chunked":
                        self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
                        self.wfile.write(chunk + b"\r\n")
                    else:
                        self.wfile.write(chunk)
                    self.wfile.flush()
                if self.path == "/chunked":
                    self.wfile.write(b"0\r\n\r\n")
                self.close_connection = True

            def log_message(self, message: str, *args: object) -> None:
                """Keep fixture access logs out of test output."""

        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.addClassCleanup(cls.server.server_close)
        cls.server_thread = Thread(
            target=cls.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        cls.server_thread.start()
        cls.addClassCleanup(cls.server_thread.join, 5)
        cls.addClassCleanup(cls.server.shutdown)

    def setUp(self) -> None:
        self.requests.clear()

    def run_cli(self, path: str) -> subprocess.CompletedProcess[str]:
        """Invoke the actual script with local proxy bypass and a time limit."""
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                f"http://127.0.0.1:{self.server.server_port}{path}",
            ],
            cwd=SCRIPT.parent,
            env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def test_complete_downloads_report_actual_body_bytes(self) -> None:
        for path in ["/data", "/no-length", "/chunked"]:
            with self.subTest(path=path):
                self.requests.clear()
                result = self.run_cli(path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertIn("Requests: 10\n", result.stdout)
                self.assertIn(
                    f"Downloaded: {len(self.body) * 10} bytes\n", result.stdout
                )
                self.assertIn(" MiB/s\n", result.stdout)
                self.assertEqual(self.requests, {path: 10})

    def test_redirect_preserves_ten_logical_downloads(self) -> None:
        result = self.run_cli("/redirect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Downloaded: {len(self.body) * 10} bytes\n", result.stdout)
        self.assertEqual(self.requests, {"/redirect": 10, "/data": 10})

    def test_http_error_and_truncated_body_stop_the_process(self) -> None:
        for path, message in [("/missing", "404"), ("/truncated", "incomplete")]:
            with self.subTest(path=path):
                self.requests.clear()
                result = self.run_cli(path)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn(message, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.requests, {path: 1})

    def test_empty_successful_responses_report_zero_speed(self) -> None:
        result = self.run_cli("/empty")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Downloaded: 0 bytes\n", result.stdout)
        self.assertIn("Download speed: 0.000 MiB/s\n", result.stdout)
        self.assertEqual(self.requests, {"/empty": 10})


if __name__ == "__main__":
    unittest.main()
