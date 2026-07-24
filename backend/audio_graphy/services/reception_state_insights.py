"""SQL-bounded, tenant-protected aggregation of reception state transitions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, distinct, func, literal, select, tuple_, union_all
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.errors import ValidationError
from audio_graphy.models.reception import DialogueStateTransition, Reception
from audio_graphy.schemas.reception_state_insights import (
    MAX_STATE_SAMPLE_RECEPTIONS,
    MAX_STATE_STAGES,
    MAX_STATE_TOP_TRIGGERS,
    ReceptionStateInsightsResponse,
    StateStageInsight,
    StateTransitionInsight,
    StateTriggerInsight,
)


def _rounded_confidence(value: Any) -> float:
    return round(float(value), 6)


class ReceptionStateInsightService:
    """Aggregate state flows with a fixed number of SQL queries.

    Aggregation stays in the database; Python receives at most the configured
    stage/edge/trigger/sample budgets, independent of the number of receptions.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def analyze(
        self,
        *,
        tenant_id: str,
        forced_agent_user_id: int | None,
        store_ids: Sequence[str],
        agent_names: Sequence[str],
        scenarios: Sequence[str],
        started_from: datetime | None,
        started_to: datetime | None,
        reception_ids: Sequence[int],
        transition_limit: int,
    ) -> ReceptionStateInsightsResponse:
        """Return one bounded state-flow snapshot for an authorized slice."""
        for field, values in (
            ("store_id", store_ids),
            ("agent_name", agent_names),
            ("scenario", scenarios),
            ("reception_id", reception_ids),
        ):
            if len(values) > 50:
                raise ValidationError(
                    f"Too many {field} filters",
                    code="RECEPTION_STATE_FILTER_LIMIT_EXCEEDED",
                    detail={"field": field, "limit": 50, "count": len(values)},
                )

        timezone_mismatch = (
            started_from is not None
            and started_to is not None
            and (started_from.utcoffset() is None) != (started_to.utcoffset() is None)
        )
        if (
            started_from is not None
            and started_to is not None
            and (timezone_mismatch or started_to <= started_from)
        ):
            raise ValidationError(
                "Reception state insight time filters are inconsistent",
                code="RECEPTION_STATE_TIME_RANGE_INVALID",
                detail={
                    "started_from": started_from.isoformat(),
                    "started_to": started_to.isoformat(),
                },
            )

        reception_filters: list[Any] = [Reception.tenant_id == tenant_id]
        if forced_agent_user_id is not None:
            reception_filters.append(Reception.agent_user_id == forced_agent_user_id)
        if store_ids:
            reception_filters.append(Reception.store_id.in_(store_ids))
        if agent_names:
            reception_filters.append(Reception.agent_name.in_(agent_names))
        if scenarios:
            reception_filters.append(Reception.scenario.in_(scenarios))
        if started_from is not None:
            reception_filters.append(Reception.started_at >= started_from)
        if started_to is not None:
            reception_filters.append(Reception.started_at < started_to)
        if reception_ids:
            reception_filters.append(Reception.id.in_(reception_ids))

        transition_join = and_(
            DialogueStateTransition.reception_id == Reception.id,
            DialogueStateTransition.tenant_id == Reception.tenant_id,
        )
        transition_filters = [
            DialogueStateTransition.tenant_id == tenant_id,
            *reception_filters,
        ]

        async with self._session_factory() as session:
            total_receptions = int(
                (
                    await session.execute(
                        select(func.count(Reception.id)).where(*reception_filters)
                    )
                ).scalar_one()
            )
            total_transitions = int(
                (
                    await session.execute(
                        select(func.count(DialogueStateTransition.id))
                        .select_from(DialogueStateTransition)
                        .join(Reception, transition_join)
                        .where(*transition_filters)
                    )
                ).scalar_one()
            )

            evidence_length = self._evidence_length_expression(session)
            transition_count = func.count(DialogueStateTransition.id).label("transition_count")
            transition_rows = (
                await session.execute(
                    select(
                        DialogueStateTransition.from_state.label("from_state"),
                        DialogueStateTransition.to_state.label("to_state"),
                        transition_count,
                        func.avg(DialogueStateTransition.confidence).label("average_confidence"),
                        func.coalesce(func.sum(evidence_length), 0).label("evidence_count"),
                    )
                    .select_from(DialogueStateTransition)
                    .join(Reception, transition_join)
                    .where(*transition_filters)
                    .group_by(
                        DialogueStateTransition.from_state,
                        DialogueStateTransition.to_state,
                    )
                    .order_by(
                        transition_count.desc(),
                        DialogueStateTransition.from_state,
                        DialogueStateTransition.to_state,
                    )
                    .limit(transition_limit + 1)
                )
            ).all()
            visible_transition_rows = transition_rows[:transition_limit]
            transition_pairs = [
                (str(row.from_state), str(row.to_state)) for row in visible_transition_rows
            ]

            triggers = await self._top_triggers(
                session,
                transition_join=transition_join,
                transition_filters=transition_filters,
                transition_pairs=transition_pairs,
            )
            samples = await self._sample_reception_ids(
                session,
                transition_join=transition_join,
                transition_filters=transition_filters,
                transition_pairs=transition_pairs,
            )
            stage_rows = await self._stages(
                session,
                transition_join=transition_join,
                transition_filters=transition_filters,
            )

        visible_stage_rows = stage_rows[:MAX_STATE_STAGES]
        stages = [
            StateStageInsight(
                state=str(row.state),
                count=int(row.stage_count),
                reception_count=int(row.reception_count),
                incoming_count=int(row.incoming_count),
                outgoing_count=int(row.outgoing_count),
                average_confidence=_rounded_confidence(row.average_confidence),
            )
            for row in visible_stage_rows
        ]
        transitions = [
            StateTransitionInsight(
                from_state=str(row.from_state),
                to_state=str(row.to_state),
                count=int(row.transition_count),
                average_confidence=_rounded_confidence(row.average_confidence),
                evidence_count=int(row.evidence_count),
                top_triggers=triggers.get(
                    (str(row.from_state), str(row.to_state)),
                    [],
                ),
                sample_reception_ids=samples.get(
                    (str(row.from_state), str(row.to_state)),
                    [],
                ),
            )
            for row in visible_transition_rows
        ]
        return ReceptionStateInsightsResponse(
            stages=stages,
            transitions=transitions,
            total_receptions=total_receptions,
            total_transitions=total_transitions,
            returned_stages=len(stages),
            stage_limit=MAX_STATE_STAGES,
            returned_transitions=len(transitions),
            transition_limit=transition_limit,
            truncated=(
                len(stage_rows) > MAX_STATE_STAGES or len(transition_rows) > transition_limit
            ),
            generated_at=datetime.now(UTC),
        )

    @staticmethod
    def _evidence_length_expression(session: AsyncSession) -> Any:
        dialect_name = session.get_bind().dialect.name
        if dialect_name in {"mysql", "mariadb"}:
            return func.coalesce(
                func.json_length(DialogueStateTransition.evidence_refs),
                0,
            )
        return func.coalesce(
            func.json_array_length(DialogueStateTransition.evidence_refs),
            0,
        )

    @staticmethod
    async def _top_triggers(
        session: AsyncSession,
        *,
        transition_join: Any,
        transition_filters: list[Any],
        transition_pairs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], list[StateTriggerInsight]]:
        if not transition_pairs:
            return {}

        trigger_counts = (
            select(
                DialogueStateTransition.from_state.label("from_state"),
                DialogueStateTransition.to_state.label("to_state"),
                DialogueStateTransition.trigger.label("trigger"),
                func.count(DialogueStateTransition.id).label("trigger_count"),
            )
            .select_from(DialogueStateTransition)
            .join(Reception, transition_join)
            .where(
                *transition_filters,
                tuple_(
                    DialogueStateTransition.from_state,
                    DialogueStateTransition.to_state,
                ).in_(transition_pairs),
            )
            .group_by(
                DialogueStateTransition.from_state,
                DialogueStateTransition.to_state,
                DialogueStateTransition.trigger,
            )
            .subquery()
        )
        ranked = select(
            trigger_counts,
            func.row_number()
            .over(
                partition_by=(
                    trigger_counts.c.from_state,
                    trigger_counts.c.to_state,
                ),
                order_by=(
                    trigger_counts.c.trigger_count.desc(),
                    trigger_counts.c.trigger.asc(),
                ),
            )
            .label("rank"),
        ).subquery()
        rows = (
            await session.execute(
                select(
                    ranked.c.from_state,
                    ranked.c.to_state,
                    ranked.c.trigger,
                    ranked.c.trigger_count,
                )
                .where(ranked.c.rank <= MAX_STATE_TOP_TRIGGERS)
                .order_by(
                    ranked.c.from_state,
                    ranked.c.to_state,
                    ranked.c.rank,
                )
            )
        ).all()

        result: dict[tuple[str, str], list[StateTriggerInsight]] = {}
        for row in rows:
            result.setdefault(
                (str(row.from_state), str(row.to_state)),
                [],
            ).append(
                StateTriggerInsight(
                    trigger=str(row.trigger),
                    count=int(row.trigger_count),
                )
            )
        return result

    @staticmethod
    async def _sample_reception_ids(
        session: AsyncSession,
        *,
        transition_join: Any,
        transition_filters: list[Any],
        transition_pairs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], list[int]]:
        if not transition_pairs:
            return {}

        distinct_receptions = (
            select(
                DialogueStateTransition.from_state.label("from_state"),
                DialogueStateTransition.to_state.label("to_state"),
                DialogueStateTransition.reception_id.label("reception_id"),
            )
            .select_from(DialogueStateTransition)
            .join(Reception, transition_join)
            .where(
                *transition_filters,
                tuple_(
                    DialogueStateTransition.from_state,
                    DialogueStateTransition.to_state,
                ).in_(transition_pairs),
            )
            .distinct()
            .subquery()
        )
        ranked = select(
            distinct_receptions,
            func.row_number()
            .over(
                partition_by=(
                    distinct_receptions.c.from_state,
                    distinct_receptions.c.to_state,
                ),
                order_by=distinct_receptions.c.reception_id.asc(),
            )
            .label("rank"),
        ).subquery()
        rows = (
            await session.execute(
                select(
                    ranked.c.from_state,
                    ranked.c.to_state,
                    ranked.c.reception_id,
                )
                .where(ranked.c.rank <= MAX_STATE_SAMPLE_RECEPTIONS)
                .order_by(
                    ranked.c.from_state,
                    ranked.c.to_state,
                    ranked.c.rank,
                )
            )
        ).all()

        result: dict[tuple[str, str], list[int]] = {}
        for row in rows:
            result.setdefault(
                (str(row.from_state), str(row.to_state)),
                [],
            ).append(int(row.reception_id))
        return result

    @staticmethod
    async def _stages(
        session: AsyncSession,
        *,
        transition_join: Any,
        transition_filters: list[Any],
    ) -> list[Any]:
        endpoints = union_all(
            select(
                DialogueStateTransition.id.label("transition_id"),
                DialogueStateTransition.reception_id.label("reception_id"),
                DialogueStateTransition.from_state.label("state"),
                DialogueStateTransition.confidence.label("confidence"),
                literal(0).label("incoming"),
                literal(1).label("outgoing"),
            )
            .select_from(DialogueStateTransition)
            .join(Reception, transition_join)
            .where(*transition_filters),
            select(
                DialogueStateTransition.id.label("transition_id"),
                DialogueStateTransition.reception_id.label("reception_id"),
                DialogueStateTransition.to_state.label("state"),
                DialogueStateTransition.confidence.label("confidence"),
                literal(1).label("incoming"),
                literal(0).label("outgoing"),
            )
            .select_from(DialogueStateTransition)
            .join(Reception, transition_join)
            .where(*transition_filters),
        ).subquery()
        # Collapse self-transitions so node count/confidence count the underlying
        # transition once while incoming/outgoing retain both directions.
        collapsed = (
            select(
                endpoints.c.state,
                endpoints.c.transition_id,
                endpoints.c.reception_id,
                endpoints.c.confidence,
                func.max(endpoints.c.incoming).label("incoming"),
                func.max(endpoints.c.outgoing).label("outgoing"),
            )
            .group_by(
                endpoints.c.state,
                endpoints.c.transition_id,
                endpoints.c.reception_id,
                endpoints.c.confidence,
            )
            .subquery()
        )
        stage_count = func.count(collapsed.c.transition_id).label("stage_count")
        return list(
            (
                await session.execute(
                    select(
                        collapsed.c.state,
                        stage_count,
                        func.count(distinct(collapsed.c.reception_id)).label("reception_count"),
                        func.sum(collapsed.c.incoming).label("incoming_count"),
                        func.sum(collapsed.c.outgoing).label("outgoing_count"),
                        func.avg(collapsed.c.confidence).label("average_confidence"),
                    )
                    .group_by(collapsed.c.state)
                    .order_by(stage_count.desc(), collapsed.c.state)
                    .limit(MAX_STATE_STAGES + 1)
                )
            ).all()
        )


__all__ = ["ReceptionStateInsightService"]
