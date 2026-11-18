"""Parse command-line arguments and present download measurements."""

import argparse
import sys
from http.client import HTTPException
from urllib.parse import urlsplit

from etolegko.downloader import measure_downloads
from etolegko.metrics import calculate_statistics


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
