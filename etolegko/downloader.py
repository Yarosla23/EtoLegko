"""Download finite HTTP resources sequentially and measure their bodies."""

from http.client import HTTPException
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from etolegko.metrics import REQUEST_COUNT, Measurement

TIMEOUT_SECONDS = 30
CHUNK_SIZE_BYTES = 64 * 1024


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
