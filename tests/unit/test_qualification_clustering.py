"""Phase 5 event clustering: duplicate publisher coverage counts once; provenance is kept."""

from __future__ import annotations

from typing import Any

from app.domain.enums import SignalType
from app.domain.qualification.bundle import build_bundle
from app.domain.qualification.clustering import EventCluster, SignalEvidence, cluster_signals
from app.domain.qualification.icp import IcpConfig
from tests.qualification_factories import NOW, evaluate, facts, sig


def clusters(signals: list[SignalEvidence], **kw: Any) -> list[EventCluster]:
    return cluster_signals(signals, company_name="Acme Robotics", **kw)


def test_same_round_from_four_publishers_is_one_event() -> None:
    signals = [
        sig("Acme Robotics raises Rs 4.5 crore seed round led by Blume", source="a", days_ago=10),
        sig("Robotics startup Acme Robotics bags Rs 4.5 crore", source="b", days_ago=10),
        sig("Acme Robotics secures Rs 4.5 Cr in seed funding", source="c", days_ago=9),
        sig("Blume backs Acme Robotics with Rs 4.5 crore", source="d", days_ago=11),
    ]
    [event] = clusters(signals)
    assert len(event.publishers) == 1  # all fixtures share one platform label
    assert event.source_count == 4
    assert sorted(map(str, event.signal_ids)) == sorted(str(s.signal_id) for s in signals)
    assert event.primary_type is SignalType.FUNDING
    assert event.event_date == min(s.published_at for s in signals if s.published_at)


def test_duplicate_coverage_does_not_inflate_confidence_or_qualification() -> None:
    one = [sig("Acme Robotics raises Rs 4 crore", source="a")]
    four = one + [
        sig(f"Acme Robotics raises Rs 4 crore - report {i}", source=f"x{i}") for i in range(3)
    ]
    [single], [merged] = clusters(one), clusters(four)
    assert merged.confidence == single.confidence  # corroboration is not extra confidence
    hard = evaluate(four)
    assert len(hard.qualifying_clusters) == 1
    bundle = build_bundle(facts(), hard, clusters(four), IcpConfig(), NOW)
    assert len(bundle.events) == 1
    assert len(bundle.signal_ids) == 4  # provenance of every duplicate kept


def test_multi_type_signals_of_one_article_share_an_event() -> None:
    title = "Acme Robotics partners Tata to deploy 1,000 robots"
    signals = [
        sig(title, SignalType.EXPANSION, source="a"),
        sig(title, SignalType.PRODUCT_CUSTOMER, source="a"),
    ]
    [event] = clusters(signals)
    assert event.types == {SignalType.EXPANSION, SignalType.PRODUCT_CUSTOMER}
    assert event.primary_type is SignalType.EXPANSION


def test_different_events_stay_separate() -> None:
    signals = [
        sig("Acme Robotics raises Rs 4 crore seed round", source="a", days_ago=10),
        sig("Acme Robotics opens Pune warehouse", SignalType.EXPANSION, source="b", days_ago=12),
        sig("Acme Robotics raises Rs 4 crore seed round", source="c", days_ago=60),  # >7 d
    ]
    assert len(clusters(signals)) == 3


def test_shared_number_and_tokens_merge() -> None:
    signals = [
        sig(
            "Tata Motors, Acme Robotics deploy over 1,000 electric trucks",
            SignalType.EXPANSION,
            source="a",
        ),
        sig(
            "EIM signs MoU with Acme Robotics for 1,000 electric trucks",
            SignalType.EXPANSION,
            source="b",
        ),
    ]
    assert len(clusters(signals)) == 1


def test_years_are_not_significant_numbers() -> None:
    signals = [
        sig("Acme Robotics wins 2026 award for logistics", SignalType.EXPANSION, source="a"),
        sig("Acme Robotics hosts 2026 logistics summit", SignalType.EXPANSION, source="b"),
    ]
    assert len(clusters(signals)) == 2


def test_dateless_items_merge_only_on_near_identical_titles() -> None:
    same = [
        sig("Acme Robotics raises Rs 2 crore from angels", source="a", days_ago=None),
        sig("Acme Robotics raises Rs 2 crore from angels", source="b", days_ago=None),
    ]
    assert len(clusters(same)) == 1
    loose = [
        sig("Acme Robotics raises Rs 2 crore from angels", source="a", days_ago=None),
        sig("Angel investors back Acme Robotics seed", source="b", days_ago=5),
    ]
    assert len(clusters(loose)) == 2


def test_clustering_is_deterministic_and_ids_stable() -> None:
    signals = [sig("Acme Robotics raises Rs 4 crore", source=s) for s in "abc"]
    first = [c.cluster_id for c in clusters(signals)]
    second = [c.cluster_id for c in clusters(list(reversed(signals)))]
    assert first == second
    assert first[0].startswith("evt_")


def test_attribution_propagates_to_the_event() -> None:
    signals = [
        sig("Acme Robotics raises Rs 4 crore", source="a", attributed=False),
        sig("Acme Robotics raises Rs 4 crore seed", source="b", attributed=True),
    ]
    [event] = clusters(signals)
    assert event.attributed


def test_bundle_orders_qualifying_events_first_and_limits_size() -> None:
    topics = [
        "warehouse", "drones", "payments", "biscuits", "football", "cinema",
        "yoga", "chess", "poetry", "gardening", "cycling",
    ]  # fmt: skip
    signals = [
        sig(f"Acme Robotics {topic} update", SignalType.HIRING, source=topic, days_ago=i)
        for i, topic in enumerate(topics, start=1)
    ] + [sig("Acme Robotics raises Rs 3 crore", source="f", days_ago=30)]
    hard = evaluate(signals)
    bundle = build_bundle(facts(), hard, clusters(signals), IcpConfig(), NOW)
    assert len(bundle.events) == 8
    assert "raises Rs 3 crore" in bundle.events[0].headline
    laya = bundle.for_laya()
    assert "signal_ids" not in laya["events"][0]  # ids are noise for the model
    record = bundle.as_record()
    assert record["events"][0]["signal_ids"]
    assert record["total_events"] == 12
