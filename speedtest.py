"""Measure download speed with ten sequential HTTP requests."""

import argparse
import sys
from dataclasses import dataclass
from http.client import HTTPException
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

REQUEST_COUNT = 10
TIMEOUT_SECONDS = 30
CHUNK_SIZE_BYTES = 64 * 1024
BYTES_PER_MIB = 1024 * 1024


@dataclass(frozen=True)
class Measurement:
    """Store the elapsed seconds and body bytes of one complete download."""

    elapsed_seconds: float
    downloaded_bytes: int


@dataclass(frozen=True)
class Statistics:
    """Store aggregate metrics without rounding their values."""

    request_count: int
    average_seconds: float
    total_bytes: int
    speed_mib_s: float


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line and reject invalid URLs before network access."""
    parser = argparse.ArgumentParser(
        description="Measure download speed with 10 sequential HTTP GET requests."
    )
    parser.add_argument("url", help="absolute HTTP/HTTPS URL of a finite resource")
    args = parser.parse_args(argv)
    if any(ord(character) <= 32 or ord(character) >= 127 for character in args.url):
        parser.error(
            "URL must be ASCII; percent-encode spaces and non-ASCII characters"
        )
    try:
        address = urlsplit(args.url)
        if (
            address.scheme not in {"http", "https"}
            or not address.hostname
            or address.port == 0
        ):
            raise ValueError("expected an absolute HTTP/HTTPS URL with a valid port")
    except ValueError as error:
        parser.error(str(error))
    return args


def download_once(url: str) -> Measurement:
    """Read one complete response in chunks and measure its time and size."""
    started_seconds = perf_counter()
    downloaded_bytes = 0
    try:
        with urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            if response.status is None or not 200 <= response.status < 300:
                raise URLError("expected a successful HTTP response")
            content_length = response.getheader("Content-Length", "").strip()
            expected_bytes = None
            if (
                content_length.isascii()
                and content_length.isdecimal()
                and response.getheader("Transfer-Encoding", "").lower() != "chunked"
                and response.status != 204
            ):
                expected_bytes = int(content_length)
            while chunk := response.read(CHUNK_SIZE_BYTES):
                downloaded_bytes += len(chunk)
            elapsed_seconds = perf_counter() - started_seconds
            if expected_bytes is not None and downloaded_bytes != expected_bytes:
                raise HTTPException(
                    f"incomplete response: received {downloaded_bytes} "
                    f"of {expected_bytes} bytes"
                )
    except HTTPError as error:
        error.close()
        raise
    return Measurement(elapsed_seconds, downloaded_bytes)


def measure_downloads(url: str) -> list[Measurement]:
    """Complete each download before starting the next one, stopping on error."""
    return [download_once(url) for _ in range(REQUEST_COUNT)]


def calculate_statistics(measurements: list[Measurement]) -> Statistics:
    """Calculate mean duration and total throughput from complete downloads."""
    if len(measurements) != REQUEST_COUNT:
        raise ValueError(f"expected {REQUEST_COUNT} complete downloads")
    total_seconds = sum(item.elapsed_seconds for item in measurements)
    if total_seconds <= 0:
        raise ValueError("total download time must be positive")
    total_bytes = sum(item.downloaded_bytes for item in measurements)
    return Statistics(
        request_count=len(measurements),
        average_seconds=total_seconds / len(measurements),
        total_bytes=total_bytes,
        speed_mib_s=total_bytes / total_seconds / BYTES_PER_MIB,
    )


def main() -> int:
    """Run the command-line application."""
    try:
        args = parse_args()
        measurements = measure_downloads(args.url)
        statistics = calculate_statistics(measurements)
    except KeyboardInterrupt:
        print("Error: interrupted by user", file=sys.stderr)
        return 130
    except (OSError, HTTPException, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Requests: {statistics.request_count}")
    print(f"Average request time: {statistics.average_seconds:.3f} s")
    print(f"Downloaded: {statistics.total_bytes} bytes")
    print(f"Download speed: {statistics.speed_mib_s:.3f} MiB/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
