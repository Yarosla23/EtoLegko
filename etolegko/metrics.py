"""Define measurement data and calculate aggregate download statistics."""

from dataclasses import dataclass

REQUEST_COUNT = 10
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
