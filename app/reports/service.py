"""Pure reports for the shared backend contracts; no I/O or event loops."""

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone

from app.shared.errors import InvalidInput
from app.shared.models import MetricSnapshot, PostStatus, PostView, ReportInput

from .models import ActivitySummary, CounterReading, MetricSummary, Report


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidInput("Время отчёта должно содержать часовой пояс.")
    return value.astimezone(timezone.utc)


def build_report(data: ReportInput) -> Report:
    """Calculate from the shared ReportInput, whose interval is [start, end].

    All business posts are supplied as current snapshots. Count actual
    created_at/published_at events inclusively; never infer approval, failure,
    revision or retry histories from status/updated_at. Current status counts
    are explicitly separate from period events.

    Metric streams are (name, post_id, source). Select their latest sample at
    or before end, plus the latest baseline strictly before start. Subscriber
    and reaction values are cumulative counters, never sums over time. An
    old post can receive reactions in this period. Ignore future samples and
    reactions before publication or without a known published post, warning
    explicitly. Reject cross-business data and conflicting duplicate events.
    source='demo', the 'demo:' source namespace, or post.is_demo marks a
    separate demo cohort. The backend uses 'demo:<channel_id>' for subscribers.

    There are no views/reach/forwards metrics in this input contract. Counts
    are only as complete as the backend snapshot; no network data is fetched.
    """
    if not isinstance(data, ReportInput):
        raise InvalidInput("Ожидались данные общего контракта ReportInput.")
    start, end = _utc(data.start), _utc(data.end)
    if end < start:
        raise InvalidInput("Конец отчётного периода раньше начала.")

    posts: dict[str, PostView] = {}
    for post in data.posts:
        if not isinstance(post, PostView) or post.business_id != data.business_id:
            raise InvalidInput("Посты отчёта относятся к другому бизнесу.")
        if post.id in posts:
            raise InvalidInput("В отчёте повторяется идентификатор поста.")
        created = _utc(post.created_at)
        if post.published_at is not None and _utc(post.published_at) < created:
            raise InvalidInput("Публикация не может предшествовать созданию поста.")
        if not isinstance(post.is_demo, bool) or not isinstance(post.status, PostStatus):
            raise InvalidInput("Некорректное состояние поста в отчёте.")
        posts[post.id] = post

    activities = []
    for is_demo in (False, True):
        cohort = [post for post in posts.values() if post.is_demo == is_demo]
        states = Counter(post.status for post in cohort)
        activities.append(ActivitySummary(
            is_demo=is_demo,
            created_posts=sum(start <= _utc(post.created_at) <= end for post in cohort),
            published_posts=sum(
                post.published_at is not None and start <= _utc(post.published_at) <= end
                for post in cohort
            ),
            current_states=tuple((status, states[status]) for status in PostStatus),
        ))

    streams: dict[tuple[str, str | None, str], list[MetricSnapshot]] = defaultdict(list)
    seen_events: dict[str, MetricSnapshot] = {}
    ignored: Counter[str] = Counter()
    for metric in data.metrics:
        if not isinstance(metric, MetricSnapshot) or metric.business_id != data.business_id:
            raise InvalidInput("Метрики отчёта относятся к другому бизнесу.")
        if type(metric.value) is not int or metric.value < 0:
            raise InvalidInput("Счётчик метрики должен быть неотрицательным целым числом.")
        if not metric.source.strip() or not metric.event_id.strip():
            raise InvalidInput("У метрики отсутствует источник или идентификатор события.")
        previous = seen_events.get(metric.event_id)
        if previous is not None:
            if previous != metric:
                raise InvalidInput("Противоречивые метрики имеют один идентификатор события.")
            continue
        seen_events[metric.event_id] = metric
        timestamp = _utc(metric.observed_at)
        if timestamp > end:
            ignored["future"] += 1
            continue
        if metric.name == "reactions":
            post = posts.get(metric.post_id)
            if post is None or post.published_at is None:
                ignored["unpublished"] += 1
                continue
            if timestamp < _utc(post.published_at):
                ignored["before_publication"] += 1
                continue
        elif metric.name != "subscribers" or metric.post_id is not None:
            ignored["unsupported"] += 1
            continue
        streams[(metric.name, metric.post_id, metric.source)].append(metric)

    counters = []
    for (name, post_id, source), samples in sorted(
        streams.items(), key=lambda item: (item[0][0], item[0][2], item[0][1] or "")
    ):
        samples.sort(key=lambda sample: (_utc(sample.observed_at), sample.event_id))
        by_time: dict[datetime, int] = {}
        for sample in samples:
            timestamp = _utc(sample.observed_at)
            if timestamp in by_time and by_time[timestamp] != sample.value:
                raise InvalidInput("Противоречивые значения одного счётчика в один момент.")
            by_time[timestamp] = sample.value
        latest = samples[-1]
        baseline = next(
            (sample for sample in reversed(samples) if _utc(sample.observed_at) < start), None
        )
        latest_at = _utc(latest.observed_at)
        counters.append(CounterReading(
            name=name,
            source=source,
            is_demo=(source == "demo" or source.startswith("demo:")
                     or (post_id is not None and posts[post_id].is_demo)),
            post_id=post_id,
            value=latest.value,
            observed_at=latest_at,
            baseline_value=baseline.value if baseline is not None else None,
            baseline_at=_utc(baseline.observed_at) if baseline is not None else None,
            delta=latest.value - baseline.value if baseline is not None and latest_at >= start else None,
        ))

    groups: dict[tuple[str, str, bool], list[CounterReading]] = defaultdict(list)
    for reading in counters:
        groups[(reading.name, reading.source, reading.is_demo)].append(reading)
    summaries = []
    for (name, source, is_demo), readings in sorted(groups.items()):
        demo_source = source == "demo" or source.startswith("demo:")
        eligible = {
            post.id for post in posts.values()
            if (demo_source or post.is_demo) == is_demo and post.published_at is not None
            and _utc(post.published_at) <= end
        }
        eligible.update(reading.post_id for reading in readings if reading.post_id is not None)
        deltas = [reading.delta for reading in readings if reading.delta is not None]
        baseline_times = [
            reading.baseline_at for reading in readings
            if reading.delta is not None and reading.baseline_at is not None
        ]
        summaries.append(MetricSummary(
            name=name, source=source, is_demo=is_demo,
            value=sum(reading.value for reading in readings),
            observed_streams=len(readings),
            eligible_streams=len(eligible) if name == "reactions" else 1,
            observed_from=min(reading.observed_at for reading in readings),
            observed_to=max(reading.observed_at for reading in readings),
            delta=sum(deltas) if deltas else None,
            delta_streams=len(deltas),
            baseline_from=min(baseline_times) if baseline_times else None,
            baseline_to=max(baseline_times) if baseline_times else None,
        ))

    labels = {
        "future": "Исключены снимки после конца периода",
        "unpublished": "Исключены реакции без известного опубликованного поста",
        "before_publication": "Исключены реакции до публикации поста",
        "unsupported": "Исключены снимки с неподдерживаемой областью метрики",
    }
    return Report(
        start=start, end=end, activities=tuple(activities),
        counters=tuple(counters), metrics=tuple(summaries),
        warnings=tuple(f"{labels[key]}: {ignored[key]}." for key in labels if ignored[key]),
    )


_STATUS_LABELS = {
    PostStatus.REVIEW: "на проверке", PostStatus.EDITING: "редактируются", PostStatus.APPROVED: "согласованы",
    PostStatus.PUBLISHING: "публикуются", PostStatus.PUBLISHED: "опубликованы",
    PostStatus.REJECTED: "отклонены", PostStatus.FAILED: "с ошибкой",
    PostStatus.DELIVERY_UNKNOWN: "доставка не подтверждена",
}


def _time_range(first: datetime, last: datetime) -> str:
    def stamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")
    return stamp(first) if first == last else f"{stamp(first)} — {stamp(last)}"


def render_report_text(report: Report) -> str:
    """Render plain Telegram text; caller must set parse_mode=None.

    User post text, errors and IDs never enter the output. Source names are
    JSON quoted to escape controls. Refuse an oversized result without hiding
    caveats. No transport calls or historical-state claims are made here.
    """
    if not isinstance(report, Report):
        raise InvalidInput("Ожидался структурированный отчёт.")
    lines = [
        "Отчёт по публикациям",
        f"Период UTC: {_time_range(report.start, report.end)} (обе границы включены)",
    ]
    for activity in report.activities:
        if activity.is_demo and not any(count for _, count in activity.current_states):
            continue
        lines.extend((
            "", "Демо-посты:" if activity.is_demo else "Реальные посты:",
            f"Создано за период: {activity.created_posts}",
            f"Опубликовано за период: {activity.published_posts}",
            "Сейчас, по всем постам: " + "; ".join(
                f"{_STATUS_LABELS[status]} {count}" for status, count in activity.current_states
            ),
        ))
    lines.extend(("", "Последние известные значения счётчиков до конца периода:"))
    if not report.metrics:
        lines.append("Реакции и подписчики: нет данных.")
    for summary in report.metrics:
        source = json.dumps(summary.source, ensure_ascii=False)
        cohort = "ДЕМО" if summary.is_demo else "реальные данные"
        label = "Реакции" if summary.name == "reactions" else "Подписчики"
        lines.append(f"{label}, источник {source}, {cohort}: {summary.value}")
        if summary.name == "reactions":
            lines.append(f"Данные по постам: {summary.observed_streams}/{summary.eligible_streams}.")
        lines.append(f"Время замеров UTC: {_time_range(summary.observed_from, summary.observed_to)}")
        if summary.delta is None:
            lines.append("Изменение: нет данных (нужны исходный и последующий замеры).")
        else:
            lines.append(
                f"Изменение: {summary.delta:+d} "
                f"(пары замеров: {summary.delta_streams}/{summary.observed_streams})."
            )
            lines.append(
                "Исходные замеры до начала периода UTC: "
                + _time_range(summary.baseline_from, summary.baseline_to)
            )
    if report.metrics:
        present = {metric.name for metric in report.metrics}
        for name, label in (("reactions", "Реакции"), ("subscribers", "Подписчики")):
            if name not in present:
                lines.append(f"{label}: нет данных.")
    lines.extend((
        "Счётчики накопленные; замеры могут быть сделаны в разное время.",
        "Текущие статусы не описывают историю за период.",
        "Число ошибок и изменений версий за период: нет данных.",
        "Просмотры, охват, пересылки, клики и продажи: нет данных.",
    ))
    if report.warnings:
        lines.extend(("", *report.warnings))
    result = "\n".join(lines)
    if len(result.encode("utf-16-le")) // 2 > 4096:
        raise InvalidInput("Отчёт превышает лимит одного сообщения Telegram.")
    return result
