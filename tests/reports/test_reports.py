"""Offline checks using the actual shared ReportInput contract."""

import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.reports import build_provider, build_report, render_report_text
from app.shared.errors import InvalidInput
from app.shared.models import MetricSnapshot, PostStatus, PostView, ReportInput
from app.shared.ports import ReportProvider

UTC = timezone.utc
START = datetime(2026, 11, 1, tzinfo=UTC)
END = datetime(2026, 11, 8, tzinfo=UTC)
BUSINESS = "business-1"


def post(post_id="post-1", **changes):
    values = dict(
        id=post_id, business_id=BUSINESS, revision=1,
        text="Untrusted <script> @everyone", image_asset_id="asset-1",
        channel_id="-100123", scheduled_at=None, status=PostStatus.PUBLISHED,
        created_at=START - timedelta(days=3), updated_at=END + timedelta(days=10),
        published_at=START - timedelta(days=2), is_demo=False,
    )
    values.update(changes)
    return PostView(**values)


def metric(value, when=START, name="reactions", post_id="post-1", source="telegram", **changes):
    if name == "subscribers":
        post_id = None
    values = dict(
        business_id=BUSINESS, name=name, value=value, observed_at=when,
        source=source, post_id=post_id,
        event_id=f"{source}:{name}:{post_id}:{when.isoformat()}:{value}",
    )
    values.update(changes)
    return MetricSnapshot(**values)


def data(posts=(), metrics=(), start=START, end=END):
    return ReportInput(business_id=BUSINESS, start=start, end=end, posts=list(posts), metrics=list(metrics))


class ActivityTests(unittest.TestCase):
    def test_empty_report_counts_zero_activity_but_no_metrics(self):
        report = build_report(data())
        self.assertEqual(report.activities[0].created_posts, 0)
        self.assertEqual(report.activities[0].published_posts, 0)
        self.assertEqual(report.metrics, ())
        self.assertIn("Реакции и подписчики: нет данных", render_report_text(report))

    def test_inclusive_boundaries_and_old_creation_new_publication(self):
        rows = [
            post("start", created_at=START, published_at=START),
            post("end", created_at=END, published_at=END),
            post("old", published_at=START + timedelta(hours=1)),
            post("later", created_at=END + timedelta(microseconds=1), published_at=None),
        ]
        activity = build_report(data(rows)).activities[0]
        self.assertEqual(activity.created_posts, 2)
        self.assertEqual(activity.published_posts, 3)
        self.assertEqual(dict(activity.current_states)[PostStatus.PUBLISHED], 4)

    def test_point_interval_is_valid_and_includes_exact_event(self):
        report = build_report(data([post(published_at=START)], start=START, end=START))
        self.assertEqual(report.activities[0].published_posts, 1)

    def test_current_status_and_revision_do_not_invent_historical_events(self):
        rows = [
            post("failed", status=PostStatus.FAILED, published_at=None, revision=99),
            post("approved", status=PostStatus.APPROVED, published_at=None),
        ]
        report = build_report(data(rows))
        states = dict(report.activities[0].current_states)
        self.assertEqual(states[PostStatus.FAILED], 1)
        self.assertEqual(states[PostStatus.APPROVED], 1)
        self.assertEqual(report.activities[0].published_posts, 0)
        text = render_report_text(report)
        self.assertIn("Сейчас, по всем постам", text)
        self.assertIn("Число ошибок и изменений версий за период: нет данных", text)
        self.assertNotIn("99", text)

    def test_demo_activity_is_separate(self):
        report = build_report(data([
            post("real", created_at=START, published_at=START),
            post("demo", created_at=START, published_at=START, is_demo=True),
        ]))
        self.assertEqual(report.activities[0].created_posts, 1)
        self.assertEqual(report.activities[1].created_posts, 1)
        self.assertIn("Демо-посты", render_report_text(report))

    def test_duplicate_current_post_rejected_even_with_other_revision(self):
        with self.assertRaises(InvalidInput):
            build_report(data([post(), post(revision=2)]))


class CounterTests(unittest.TestCase):
    def test_reaction_snapshots_are_counters_not_additive_events(self):
        report = build_report(data([post()], [
            metric(1), metric(5, START + timedelta(days=1)), metric(7, END),
        ]))
        self.assertEqual(len(report.counters), 1)
        self.assertEqual(report.metrics[0].value, 7)
        self.assertIsNone(report.metrics[0].delta)

    def test_old_post_reactions_in_period_are_included(self):
        report = build_report(data([post()], [metric(8)]))
        self.assertEqual(report.activities[0].created_posts, 0)
        self.assertEqual(report.activities[0].published_posts, 0)
        self.assertEqual(report.metrics[0].value, 8)

    def test_subscribers_latest_and_delta_use_same_stream_baseline(self):
        samples = [
            metric(100, START - timedelta(days=2), name="subscribers"),
            metric(110, START - timedelta(hours=1), name="subscribers"),
            metric(115, START, name="subscribers"),
            metric(125, END, name="subscribers"),
        ]
        report = build_report(data(metrics=samples))
        reading = report.counters[0]
        self.assertEqual(reading.value, 125)
        self.assertEqual(reading.baseline_value, 110)
        self.assertEqual(reading.delta, 15)
        self.assertEqual(reading.baseline_at, START - timedelta(hours=1))
        self.assertEqual(report.metrics[0].value, 125)

    def test_missing_baseline_does_not_become_zero(self):
        report = build_report(data(metrics=[metric(123, name="subscribers")]))
        self.assertIsNone(report.counters[0].baseline_value)
        self.assertIsNone(report.counters[0].delta)
        self.assertIn("Изменение: нет данных", render_report_text(report))

    def test_baseline_only_is_stale_known_value_not_proven_no_change(self):
        before = START - timedelta(hours=1)
        report = build_report(data(metrics=[metric(7, before, name="subscribers")]))
        self.assertEqual(report.counters[0].value, 7)
        self.assertEqual(report.counters[0].observed_at, before)
        self.assertIsNone(report.counters[0].delta)

    def test_zero_counter_and_zero_delta_are_real_measurements(self):
        report = build_report(data(metrics=[
            metric(0, START - timedelta(seconds=1), name="subscribers"),
            metric(0, name="subscribers"),
        ]))
        self.assertEqual(report.metrics[0].value, 0)
        self.assertEqual(report.metrics[0].delta, 0)
        self.assertIn("реальные данные: 0", render_report_text(report))
        self.assertIn("Изменение: +0", render_report_text(report))

    def test_removed_reactions_allow_negative_delta(self):
        report = build_report(data([post()], [
            metric(10, START - timedelta(hours=1)), metric(8),
        ]))
        self.assertEqual(report.metrics[0].delta, -2)

    def test_several_posts_sum_latest_once_and_show_missing_post_data(self):
        report = build_report(data([post("a"), post("b"), post("missing")], [
            metric(2, post_id="a"),
            metric(5, END, post_id="a"),
            metric(3, post_id="b"),
        ]))
        summary = report.metrics[0]
        self.assertEqual(summary.value, 8)
        self.assertEqual((summary.observed_streams, summary.eligible_streams), (2, 3))
        self.assertIn("Данные по постам: 2/3", render_report_text(report))

    def test_partial_delta_exposes_coverage(self):
        report = build_report(data([post("a"), post("b")], [
            metric(2, START - timedelta(hours=1), post_id="a"),
            metric(5, END, post_id="a"), metric(9, END, post_id="b"),
        ]))
        self.assertEqual(report.metrics[0].delta, 3)
        self.assertEqual(report.metrics[0].delta_streams, 1)
        self.assertIn("пары замеров: 1/2", render_report_text(report))

    def test_sources_are_not_summed_and_do_not_supply_each_others_baseline(self):
        report = build_report(data(metrics=[
            metric(100, START - timedelta(days=1), name="subscribers", source="a"),
            metric(110, END, name="subscribers", source="b"),
        ]))
        self.assertEqual(len(report.metrics), 2)
        self.assertEqual([summary.value for summary in report.metrics], [100, 110])
        self.assertTrue(all(summary.delta is None for summary in report.metrics))

    def test_demo_source_and_demo_post_never_merge_into_real_reactions(self):
        report = build_report(data([post("real"), post("demo", is_demo=True)], [
            metric(2, post_id="real"),
            metric(100, post_id="real", source="demo"),
            metric(200, post_id="demo"),
        ]))
        values = {(s.source, s.is_demo): s.value for s in report.metrics}
        self.assertEqual(values[("telegram", False)], 2)
        self.assertEqual(values[("telegram", True)], 200)
        self.assertEqual(values[("demo", True)], 100)
        self.assertIn("ДЕМО", render_report_text(report))

    def test_demo_source_coverage_includes_real_posts_without_measurements(self):
        for source in ("demo", "demo:-100123"):
            with self.subTest(source=source):
                report = build_report(data([post("real-a"), post("real-b")], [
                    metric(100, post_id="real-a", source=source),
                    metric(2, post_id="real-a", source="telegram"),
                    metric(3, post_id="real-b", source="telegram"),
                ]))
                summaries = {(s.source, s.is_demo): s for s in report.metrics}
                demo = summaries[(source, True)]
                real = summaries[("telegram", False)]
                self.assertEqual(demo.value, 100)
                self.assertEqual((demo.observed_streams, demo.eligible_streams), (1, 2))
                self.assertEqual(real.value, 5)
                self.assertEqual((real.observed_streams, real.eligible_streams), (2, 2))

    def test_demo_subscribers_stay_separate(self):
        report = build_report(data(metrics=[
            metric(2, name="subscribers"), metric(100, name="subscribers", source="demo"),
        ]))
        self.assertEqual(len(report.metrics), 2)
        self.assertEqual({s.is_demo: s.value for s in report.metrics}, {False: 2, True: 100})

    def test_backend_demo_channel_namespace_and_baseline_never_mix_with_real(self):
        report = build_report(data(metrics=[
            metric(10, START - timedelta(hours=1), name="subscribers", source="demo:-100123"),
            metric(15, END, name="subscribers", source="demo:-100123"),
            metric(100, START - timedelta(hours=1), name="subscribers", source="telegram:-100123"),
            metric(120, END, name="subscribers", source="telegram:-100123"),
        ]))
        summaries = {s.source: s for s in report.metrics}
        self.assertTrue(summaries["demo:-100123"].is_demo)
        self.assertEqual(summaries["demo:-100123"].value, 15)
        self.assertEqual(summaries["demo:-100123"].delta, 5)
        self.assertFalse(summaries["telegram:-100123"].is_demo)
        self.assertEqual(summaries["telegram:-100123"].value, 120)
        self.assertEqual(summaries["telegram:-100123"].delta, 20)
        text = render_report_text(report)
        self.assertIn('источник "demo:-100123", ДЕМО: 15', text)
        self.assertIn('источник "telegram:-100123", реальные данные: 120', text)

    def test_future_and_before_publication_and_orphan_samples_are_excluded(self):
        published = post(published_at=START)
        report = build_report(data([published, post("draft", published_at=None)], [
            metric(100, END + timedelta(microseconds=1)),
            metric(200, START - timedelta(microseconds=1)),
            metric(300, post_id="unknown"), metric(400, post_id="draft"),
            metric(5, END),
        ]))
        self.assertEqual(report.metrics[0].value, 5)
        self.assertEqual(len(report.warnings), 3)

    def test_subscriber_post_scope_and_unscoped_reactions_are_excluded(self):
        bad_subscribers = metric(100, name="subscribers").model_copy(update={"post_id": "post-1"})
        bad_reactions = metric(200).model_copy(update={"post_id": None})
        report = build_report(data([post()], [bad_subscribers, bad_reactions]))
        self.assertEqual(report.metrics, ())
        self.assertEqual(len(report.warnings), 2)

    def test_identical_events_deduplicate_but_conflicting_events_fail(self):
        sample = metric(1)
        report = build_report(data([post()], [sample, sample]))
        self.assertEqual(report.metrics[0].value, 1)
        with self.assertRaises(InvalidInput):
            build_report(data([post()], [sample, sample.model_copy(update={"value": 2})]))

    def test_conflicting_values_at_same_time_fail_without_guessing_order(self):
        with self.assertRaises(InvalidInput):
            build_report(data([post()], [metric(1), metric(2)]))

    def test_input_order_has_no_effect(self):
        samples = [metric(1), metric(4, END)]
        self.assertEqual(
            build_report(data([post()], samples)), build_report(data([post()], reversed(samples)))
        )


class ValidationAndRenderingTests(unittest.TestCase):
    def test_cross_business_records_rejected(self):
        for report_input in (
            data([post(business_id="other")]),
            data(metrics=[metric(1, business_id="other", name="subscribers")]),
        ):
            with self.subTest(data=report_input), self.assertRaises(InvalidInput):
                build_report(report_input)

    def test_invalid_period_and_bad_input_fail_safely(self):
        for report_input in (
            data(start=END, end=START),
            data().model_copy(update={"start": START.replace(tzinfo=None)}),
            {},
        ):
            with self.subTest(data=report_input), self.assertRaises(InvalidInput):
                build_report(report_input)

    def test_boolean_negative_or_noninteger_counter_rejected(self):
        for value in (True, -1, 1.5):
            sample = metric(1, name="subscribers").model_copy(update={"value": value})
            with self.subTest(value=value), self.assertRaises(InvalidInput):
                build_report(data(metrics=[sample]))

    def test_publication_before_creation_rejected(self):
        with self.assertRaises(InvalidInput):
            build_report(data([post(created_at=START, published_at=START - timedelta(seconds=1))]))

    def test_mixed_timezones_are_compared_by_instant(self):
        moscow = timezone(timedelta(hours=3))
        report = build_report(data(
            [post(created_at=START.astimezone(moscow), published_at=START)],
            [metric(2, END.astimezone(moscow))], start=START.astimezone(moscow),
        ))
        self.assertEqual(report.activities[0].created_posts, 1)
        self.assertEqual(report.metrics[0].value, 2)
        self.assertEqual(report.end.tzinfo, UTC)

    def test_dst_fold_uses_utc_for_inclusive_bounds(self):
        ny = ZoneInfo("America/New_York")
        first = datetime(2026, 11, 1, 1, 30, tzinfo=ny, fold=0)
        second = datetime(2026, 11, 1, 1, 30, tzinfo=ny, fold=1)
        report = build_report(data(
            [post("a", created_at=first, published_at=first),
             post("b", created_at=second, published_at=second)],
            start=first, end=second,
        ))
        self.assertEqual(report.end - report.start, timedelta(hours=1))
        self.assertEqual(report.activities[0].published_posts, 2)

    def test_factory_matches_backend_protocol_and_outputs_plain_text(self):
        provider: ReportProvider = build_provider(object())
        text = provider.build(data([post()], [metric(0)]))
        self.assertTrue(text.startswith("Отчёт по публикациям\n"))
        self.assertNotIn("backend", text)
        self.assertNotIn("<script>", text)
        self.assertNotIn("@everyone", text)
        self.assertIn("Просмотры, охват, пересылки, клики и продажи: нет данных", text)

    def test_source_control_characters_are_quoted(self):
        source = "telegram\nподписчики: 100000"
        text = build_provider(None).build(data(metrics=[metric(2, name="subscribers", source=source)]))
        self.assertIn("telegram\\nподписчики", text)
        self.assertNotIn("telegram\nподписчики", text)

    def test_oversized_text_fails_without_silent_truncation(self):
        report = build_report(data(metrics=[metric(2, name="subscribers", source="x" * 5000)]))
        with self.assertRaises(InvalidInput):
            render_report_text(report)

    def test_output_dataclasses_are_immutable(self):
        report = build_report(data())
        with self.assertRaises(FrozenInstanceError):
            report.start = END


if __name__ == "__main__":
    unittest.main()
