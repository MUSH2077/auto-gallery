"""Complete read-only scheduler decisions with bounded page retention."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select, func, or_, String, cast
from app.models import SubscriptionSource, Subscription, Creator
from app.services.settings import get_scheduler_config
from app.services.subscription_schedule import schedule_decision_snapshot
from app.services.subscription_calendar import effective_calendar_rule
from app.services.auth_health import (
    actionable_binding_exists,
    credential_issue_binding_exists,
)

from app.providers import registry
from app.services.search_projection_fields import _iso


def provider_state(source: str, source_url: str | None) -> dict:
    try:
        provider = registry.get(source)
    except KeyError:
        return {
            "provider_display_name": source,
            "can_download": False,
            "url_valid": False,
            "skip_reason": "unknown_provider",
        }
    normalized_url = provider.normalize_url(source_url) if source_url else None
    candidate_url = normalized_url or source_url
    url_valid = bool(candidate_url and provider.validate_url(candidate_url))
    return {
        "provider_display_name": provider.display_name,
        "can_download": bool(provider.capabilities.can_download),
        "url_valid": url_valid,
        "skip_reason": None,
    }



def decision_item(
    ss,
    sub,
    creator,
    binding_auth_actionable=False,
    binding_credential_issue=False,
    *,
    config,
    now,
    tz,
    tz_name,
    scheduler_enabled,
    overdue_cutoff,
    source_policy=None,
    subscription_policy=None,
):
    from app.services.auth_health import classify_source_health

    source = ss
    subscription = sub
    ss = source_policy if source_policy is not None else ss
    sub = subscription_policy if subscription_policy is not None else sub
    suppressed = False
    provider = provider_state(source.source, source.source_url)
    can_download = bool(provider["can_download"])
    url_valid = bool(provider["url_valid"])
    auth_health = classify_source_health(ss, sub)
    auth_healthy = ss.auth_healthy is not False
    decision = schedule_decision_snapshot(
        sub,
        config,
        ss.last_synced_at,
        ss.last_attempted_at,
        now,
        tz,
        ss.next_sync_at,
    )
    due = bool(decision.get("due"))
    reason = str(decision.get("reason"))
    suppression_reason = None

    effective_auth_state = (
        "unhealthy" if binding_auth_actionable else auth_health.auth_state
    )
    effective_credential_state = (
        "missing" if binding_credential_issue else auth_health.credential_state
    )

    if not sub.is_active:
        due = False
        reason = "subscription_inactive"
    elif not sub.sync_enabled:
        due = False
        reason = "subscription_sync_disabled"
    elif binding_auth_actionable or auth_health.actionable:
        due = False
        reason = "auth_unhealthy"
    elif binding_credential_issue:
        due = False
        reason = "credential_missing"
    elif not ss.is_enabled:
        due = False
        reason = "source_disabled"
    elif not can_download:
        due = False
        reason = provider["skip_reason"] or "provider_not_downloadable"
    elif not url_valid:
        due = False
        reason = "url_invalid"

    if not scheduler_enabled:
        if due:
            suppressed = True
        due = False
        suppression_reason = "scheduler_disabled"

    next_due_at = decision.get("next_due_at")
    parsed_next_due_at = None
    if next_due_at:
        try:
            parsed_next_due_at = datetime.fromisoformat(next_due_at)
            if parsed_next_due_at.tzinfo is None:
                parsed_next_due_at = parsed_next_due_at.replace(tzinfo=tz)
        except (TypeError, ValueError):
            parsed_next_due_at = None
    is_overdue = bool(due and parsed_next_due_at and parsed_next_due_at <= overdue_cutoff)
    is_attention = (
        reason
        in {
            "auth_unhealthy",
            "credential_missing",
            "url_invalid",
            "provider_not_downloadable",
        }
        or is_overdue
    )

    return {
        "subscription_id": str(subscription.id),
        "subscription_name": sub.name,
        "subscription_active": sub.is_active,
        "subscription_sync_enabled": sub.sync_enabled,
        "creator_id": str(creator.id),
        "creator_name": creator.display_name or creator.name,
        "source_id": str(source.id),
        "source": source.source,
        "source_display_name": provider["provider_display_name"],
        "source_url": source.source_url,
        "source_creator_id": source.source_creator_id,
        "source_enabled": ss.is_enabled,
        "effective_mode": decision.get("mode") or sub.schedule_mode or config.get("schedule_mode", "interval"),
        "timezone": tz_name,
        "scheduled_times": sub.scheduled_times or config.get("scheduled_times", ""),
        "schedule_rule": (effective_calendar_rule(sub, config) if (sub.schedule_mode or config.get("schedule_mode")) in {"calendar", "fixed_time"} else None),
        "sync_interval_hours": sub.sync_interval_hours,
        "last_synced_at": _iso(ss.last_synced_at),
        "last_attempted_at": _iso(ss.last_attempted_at),
        "due": due,
        "decision": "due_now" if due else reason,
        "reason": reason,
        "suppression_reason": suppression_reason,
        "next_due_at": next_due_at,
        "window_start": decision.get("window_start"),
        "window_end": decision.get("window_end"),
        "auth_healthy": auth_healthy,
        "auth_state": effective_auth_state,
        "credential_state": effective_credential_state,
        "url_valid": url_valid,
        "can_download": can_download,
        "is_overdue": is_overdue,
        "is_attention": is_attention,
    }, suppressed


async def decision_page(db, *, view="all", q=None, state="all", subscription_ids=None, offset=0, limit=100):
    config = await get_scheduler_config(db)
    tz_name = config.get("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz)
    context = dict(
        config=config,
        now=now,
        tz=tz,
        tz_name=tz_name,
        scheduler_enabled=bool(config.get("scheduler_enabled", True)),
        overdue_cutoff=now - timedelta(minutes=max(5, int(config.get("scheduler_scan_interval_minutes", 60))) * 2),
    )
    base = (
        select(
            SubscriptionSource,
            Subscription,
            Creator,
            actionable_binding_exists().label("binding_auth_actionable"),
            credential_issue_binding_exists().label("binding_credential_issue"),
        )
        .join(Subscription, SubscriptionSource.subscription_id == Subscription.id)
        .join(Creator, Subscription.creator_id == Creator.id)
    )
    if subscription_ids:
        base = base.where(Subscription.id.in_(subscription_ids))
    order = (Creator.display_name.asc().nullsfirst(), Creator.name, SubscriptionSource.source, SubscriptionSource.created_at.desc(), SubscriptionSource.id)
    filtered = base
    if q and q.strip():
        filtered = filtered.where(
            or_(
                *[
                    column.icontains(q.strip(), autoescape=True)
                    for column in (
                        Creator.display_name,
                        Creator.name,
                        Subscription.name,
                        SubscriptionSource.source,
                        SubscriptionSource.source_url,
                        SubscriptionSource.source_creator_id,
                        cast(SubscriptionSource.id, String),
                    )
                ]
            )
        )
    summary = {"blocked_count": 0, "overdue_count": 0, "oldest_overdue_at": None}
    suppressed_count = 0
    # Summary semantics cover all selected subscriptions, independent of page,
    # q/state/view. Stream at most 200 rows at once; CPU remains O(N).
    stream = await db.stream(base.execution_options(yield_per=200))
    async for partition in stream.partitions(200):
        for row in partition:
            item, suppressed = decision_item(*row, **context)
            suppressed_count += int(suppressed)
            if item["is_attention"] and not item["is_overdue"]:
                summary["blocked_count"] += 1
            if item["is_overdue"]:
                summary["overdue_count"] += 1
                due_at = item["next_due_at"]
                if summary["oldest_overdue_at"] is None or due_at < summary["oldest_overdue_at"]:
                    summary["oldest_overdue_at"] = due_at
    if view == "all" and state == "all":
        total = int((await db.execute(select(func.count()).select_from(filtered.subquery()))).scalar_one())
        rows = (await db.execute(filtered.order_by(*order).offset(offset).limit(limit))).all()
        items = [decision_item(*row, **context)[0] for row in rows]
    else:
        total, items = 0, []
        stream = await db.stream(filtered.order_by(*order).execution_options(yield_per=200))
        async for partition in stream.partitions(200):
            for row in partition:
                item, _ = decision_item(*row, **context)
                if view == "attention" and not item["is_attention"]:
                    continue
                if state == "due" and not item["due"]:
                    continue
                if state == "manual" and item["reason"] != "manual_mode":
                    continue
                if state == "disabled" and item["reason"] not in {"source_disabled", "subscription_sync_disabled", "subscription_inactive"}:
                    continue
                if offset <= total < offset + limit:
                    items.append(item)
                total += 1
    return dict(
        updated_at=now.isoformat(),
        scheduler_enabled=context["scheduler_enabled"],
        suppressed_count=suppressed_count,
        timezone=tz_name,
        view=view,
        total=total,
        items=items,
        offset=offset,
        limit=limit,
        next_offset=offset + len(items) if offset + len(items) < total else None,
        summary=summary,
    )
