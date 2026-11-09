from collections import Counter

from app.shared.models import MetricSnapshot, PostStatus, ReportInput


def metric_label(sample: MetricSnapshot) -> str:
    return f"{sample.value} ({sample.observed_at:%d.%m %H:%M %Z})"


class Reporter:
    def build(self, data: ReportInput) -> str:
        states = Counter(post.status for post in data.posts if not post.is_demo)
        prepared = sum(not p.is_demo and data.start <= p.created_at <= data.end for p in data.posts)
        published = sum(
            not p.is_demo and p.published_at is not None and data.start <= p.published_at <= data.end
            for p in data.posts
        )
        reactions: dict[tuple[str, str | None], MetricSnapshot] = {}
        subscriber_streams: dict[str, list[MetricSnapshot]] = {}
        for sample in sorted(data.metrics, key=lambda value: value.observed_at):
            if sample.source.startswith("demo") or sample.observed_at > data.end:
                continue
            if sample.name == "reactions" and data.start <= sample.observed_at:
                reactions[sample.source, sample.post_id] = sample
            if sample.name == "subscribers":
                subscriber_streams.setdefault(sample.source, []).append(sample)
        rows = [
            f"Отчёт · {data.start:%d.%m.%Y} — {data.end:%d.%m.%Y}",
            f"Подготовлено: {prepared}", f"Опубликовано: {published}",
            f"Сейчас на согласовании: {states[PostStatus.REVIEW]}",
            f"Сейчас в расписании: {states[PostStatus.APPROVED] + states[PostStatus.PUBLISHING]}",
            f"Сейчас отклонено: {states[PostStatus.REJECTED]}",
            f"Ошибки и неизвестные отправки: {states[PostStatus.FAILED] + states[PostStatus.DELIVERY_UNKNOWN]}",
            ("Реакции: " + (f"{sum(s.value for s in reactions.values())} по {len(reactions)} постам"
                            if reactions else "нет данных")),
        ]
        channels = []
        for source, samples in subscriber_streams.items():
            latest = samples[-1]
            if latest.observed_at < data.start:
                continue
            baseline = next((s for s in reversed(samples[:-1]) if s.observed_at <= data.start), None)
            label = source.split(":", 1)[-1]
            change = f"{latest.value - baseline.value:+d}; начальный замер {metric_label(baseline)}" if baseline \
                else "нет начального замера"
            channels.append(f"Подписчики ({label}): {metric_label(latest)}; изменение: {change}")
        rows.extend(channels or ["Подписчики: нет данных"])
        return "\n".join(rows)
