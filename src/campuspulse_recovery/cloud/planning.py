"""Shared server-owned planning policy; no external API or credential setup."""
from ..lms.planning import MoodlePlanningStore, moodle_resource_id
from ..lms.sync import MoodleSync
from .authority import utcnow


def assignments_resource(settings):
    from .accounts import account_binding
    bound = account_binding(settings)
    if bound is not None:
        return moodle_resource_id('https://lms.astanait.edu.kz', bound.moodle_user_id)
    cfg = settings.moodle
    return None if cfg is None else moodle_resource_id(cfg.site_url, cfg.expected_user_id)


def planning_inputs(settings, policy, documents, *, clock=utcnow):
    from .accounts import account_binding
    bound = account_binding(settings)
    if bound is not None:
        from ..lms.feed_planning import FeedPlanningStore
        return FeedPlanningStore(documents, policy, user_id=bound.moodle_user_id, clock=clock)
    cfg = settings.moodle
    if cfg is None:
        return None
    connection = MoodleSync(documents, owner_id=policy.config.student.trusted_user_id,
        site_url=cfg.site_url, expected_user_id=cfg.expected_user_id,
        interval_minutes=cfg.interval_minutes, stale_after_minutes=cfg.stale_after_minutes,
        clock=clock)
    return MoodlePlanningStore(documents, policy, connection=connection, clock=clock)


async def current_constraints(settings, inputs, context, now):
    if inputs is None:
        return settings.constraints(now)
    return inputs.constraints(await inputs.get(context), now=now)
