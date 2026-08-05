from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

ORIN_MTTCAN_NOMINAL = {
    "brp_min": 1,
    "brp_max": 511,
    "tseg1_min": 2,
    "tseg1_max": 255,
    "tseg2_min": 1,
    "tseg2_max": 127,
}
ORIN_MTTCAN_DATA = {
    "brp_min": 1,
    "brp_max": 15,
    "tseg1_min": 1,
    "tseg1_max": 31,
    "tseg2_min": 1,
    "tseg2_max": 15,
}


@dataclass(frozen=True, slots=True)
class TimingCandidate:
    target_bitrate: int
    target_sample_point: float
    actual_bitrate: float
    bitrate_error_ppm: float
    actual_sample_point: float
    sample_error: float
    clock_hz: int
    brp: int
    total_tq: int
    tseg1: int
    tseg2: int
    exact_bitrate: bool
    exact_sample_point: bool

    @property
    def prop_seg(self) -> int:
        return self.tseg1 // 2

    @property
    def phase_seg1(self) -> int:
        return self.tseg1 - self.prop_seg


def iter_candidates(
    *, bitrate: int, sample_point: float, clock_hz: int, limits: dict[str, int]
) -> Iterator[TimingCandidate]:
    if bitrate <= 0 or clock_hz <= 0 or not 0 < sample_point < 1:
        raise ValueError("bitrate、clock_hz 必须为正数，sample_point 必须在 0..1")
    total_min = 1 + limits["tseg1_min"] + limits["tseg2_min"]
    total_max = 1 + limits["tseg1_max"] + limits["tseg2_max"]
    for brp in range(limits["brp_min"], limits["brp_max"] + 1):
        for total_tq in range(total_min, total_max + 1):
            for sample_tq in {
                int(sample_point * total_tq),
                round(sample_point * total_tq),
                int(sample_point * total_tq) + 1,
            }:
                tseg1, tseg2 = sample_tq - 1, total_tq - sample_tq
                if not limits["tseg1_min"] <= tseg1 <= limits["tseg1_max"]:
                    continue
                if not limits["tseg2_min"] <= tseg2 <= limits["tseg2_max"]:
                    continue
                actual_bitrate = clock_hz / (brp * total_tq)
                actual_sample = sample_tq / total_tq
                yield TimingCandidate(
                    target_bitrate=bitrate,
                    target_sample_point=sample_point,
                    actual_bitrate=actual_bitrate,
                    bitrate_error_ppm=abs(actual_bitrate - bitrate) / bitrate * 1_000_000,
                    actual_sample_point=actual_sample,
                    sample_error=abs(actual_sample - sample_point),
                    clock_hz=clock_hz,
                    brp=brp,
                    total_tq=total_tq,
                    tseg1=tseg1,
                    tseg2=tseg2,
                    exact_bitrate=abs(actual_bitrate - bitrate) < 1e-9,
                    exact_sample_point=abs(actual_sample - sample_point) < 1e-12,
                )


def best_candidate(
    *, bitrate: int, sample_point: float, clock_hz: int = 50_000_000, data_phase: bool = False
) -> TimingCandidate:
    limits = ORIN_MTTCAN_DATA if data_phase else ORIN_MTTCAN_NOMINAL
    return min(
        iter_candidates(bitrate=bitrate, sample_point=sample_point, clock_hz=clock_hz, limits=limits),
        key=lambda item: (item.bitrate_error_ppm, item.sample_error, -item.total_tq, item.brp),
    )


def tdcr(candidate: TimingCandidate, tdcf: int = 0) -> dict[str, int | str]:
    if not 0 <= tdcf <= 0x7F:
        raise ValueError("TDCF 必须在 0..127")
    tdco = candidate.tseg1
    value = (tdco << 8) | tdcf
    return {"tdco": tdco, "tdcf": tdcf, "tdcr_decimal": value, "tdcr_hex": f"0x{value:x}"}
