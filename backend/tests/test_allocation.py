"""频谱分配方案（多段可用 + 排除窗 + 版本）的单元测试。"""
import pytest

from app.services import allocation as A


def test_legacy_band_migrates_to_single_segment():
    alloc = A.allocation_from_band(80.0, 220.0)
    assert alloc.version == 1
    assert len(alloc.segments) == 1
    pieces = A.effective_pieces(alloc)
    assert len(pieces) == 1
    assert (pieces[0][0].low_mhz, pieces[0][0].high_mhz) == (80.0, 220.0)


def test_exclusion_splits_segment_into_pieces():
    alloc = A.Allocation(
        segments=(A.Segment(90, 110),),
        exclusions=(A.Exclusion(98, 102, "保护空洞"),))
    pieces = [(p.low_mhz, p.high_mhz) for p, _ in A.effective_pieces(alloc)]
    assert pieces == [(90.0, 98.0), (102.0, 110.0)]
    view = A.allocation_view(alloc)
    assert view["pieces"][0]["segment_low_mhz"] == 90.0
    assert view["pieces"][1]["segment_high_mhz"] == 110.0


def test_two_disjoint_segments_have_gap_piece():
    alloc = A.Allocation(segments=(A.Segment(90, 100), A.Segment(110, 120)))
    pieces = [(p.low_mhz, p.high_mhz) for p, _ in A.effective_pieces(alloc)]
    assert pieces == [(90.0, 100.0), (110.0, 120.0)]
    # 跨段间空洞的占用找不到归属小片
    assert A.containing_piece(alloc, 97, 113) is None
    assert A.containing_piece(alloc, 91, 99) is not None


def test_payload_rejects_overlapping_segments():
    payload = {"segments": [{"low_mhz": 90, "high_mhz": 100},
                            {"low_mhz": 99, "high_mhz": 110}], "exclusions": []}
    alloc = A.allocation_from_payload(payload)
    with pytest.raises(ValueError, match="重叠"):
        A.validate_allocation(alloc)


def test_payload_rejects_inverted_segment():
    with pytest.raises(ValueError):
        A.allocation_from_payload({"segments": [{"low_mhz": 100, "high_mhz": 90}]})


def test_payload_rejects_dangling_exclusion():
    """排除窗不与任何可用段相交 -> 不完整区间，拒绝（不部分保存）。"""
    payload = {"segments": [{"low_mhz": 90, "high_mhz": 100}],
               "exclusions": [{"low_mhz": 150, "high_mhz": 160, "reason": "x"}]}
    with pytest.raises(ValueError, match="不与任何可用频段相交"):
        A.allocation_from_payload(payload)


def test_exclusion_covering_everything_rejected():
    payload = {"segments": [{"low_mhz": 90, "high_mhz": 100}],
               "exclusions": [{"low_mhz": 89, "high_mhz": 101}]}
    alloc = A.allocation_from_payload(payload)
    with pytest.raises(ValueError, match="没有任何有效净空"):
        A.validate_allocation(alloc)


def test_crossed_exclusions_detection():
    alloc = A.Allocation(
        segments=(A.Segment(90, 120),),
        exclusions=(A.Exclusion(100, 105, "空洞"),))
    # 相切不算跨越
    assert A.crossed_exclusions(alloc, 95, 100) == []
    # 有正长度交集才算
    hit = A.crossed_exclusions(alloc, 99, 106)
    assert len(hit) == 1 and hit[0].reason == "空洞"


def test_fingerprint_ignores_version_and_name():
    a1 = A.allocation_from_band(90, 100)
    a2 = A.Allocation(segments=(A.Segment(90, 100),), version=7, name="改名")
    assert A.allocation_fingerprint(a1) == A.allocation_fingerprint(a2)
    a3 = A.Allocation(segments=(A.Segment(90, 101),), version=2)
    assert A.allocation_fingerprint(a1) != A.allocation_fingerprint(a3)


def test_segment_must_exist():
    with pytest.raises(ValueError, match="至少要包含一个可用频段"):
        A.allocation_from_payload({"segments": []})
