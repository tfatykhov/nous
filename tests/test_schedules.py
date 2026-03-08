"""Tests for schedule management and task scheduler (011.1)."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from nous.config import Settings
from nous.handlers.task_scheduler import TaskScheduler
from nous.heart.heart import Heart
from nous.heart.schedules import ScheduleManager
from nous.storage.models import Schedule


@pytest_asyncio.fixture
async def session(db):
    async with db.engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        yield session
        await session.close()
        await trans.rollback()


@pytest_asyncio.fixture
async def schedule_mgr(db):
    return ScheduleManager(db, "test-agent")


class TestScheduleManager:

    async def test_create_once_schedule(self, schedule_mgr: ScheduleManager):
        fire_at = datetime.now(UTC) + timedelta(hours=2)
        schedule = await schedule_mgr.create(
            task="Remind Tim",
            schedule_type="once",
            fire_at=fire_at,
        )
        assert schedule.schedule_type == "once"
        assert schedule.next_fire_at == fire_at
        assert schedule.active is True

    async def test_create_recurring_with_interval(self, schedule_mgr: ScheduleManager):
        schedule = await schedule_mgr.create(
            task="Check snow",
            schedule_type="recurring",
            interval_seconds=21600,
        )
        assert schedule.schedule_type == "recurring"
        assert schedule.interval_seconds == 21600
        assert schedule.next_fire_at is not None

    async def test_create_recurring_with_cron(self, schedule_mgr: ScheduleManager):
        schedule = await schedule_mgr.create(
            task="Daily check",
            schedule_type="recurring",
            cron_expr="0 8 * * *",
        )
        assert schedule.cron_expr == "0 8 * * *"
        assert schedule.next_fire_at is not None

    async def test_create_recurring_without_timing_raises(self, schedule_mgr: ScheduleManager):
        with pytest.raises(ValueError, match="interval_seconds or cron_expr"):
            await schedule_mgr.create(
                task="Bad schedule",
                schedule_type="recurring",
            )

    async def test_get_due_schedules(self, schedule_mgr: ScheduleManager):
        # Create schedule due in the past
        past = datetime.now(UTC) - timedelta(minutes=5)
        await schedule_mgr.create(
            task="Overdue task",
            schedule_type="once",
            fire_at=past,
        )
        # Create schedule due in the future
        future = datetime.now(UTC) + timedelta(hours=2)
        await schedule_mgr.create(
            task="Future task",
            schedule_type="once",
            fire_at=future,
        )

        due = await schedule_mgr.get_due(datetime.now(UTC))
        assert len(due) == 1
        assert due[0].task == "Overdue task"

    async def test_advance_recurring_schedule(self, schedule_mgr: ScheduleManager):
        schedule = await schedule_mgr.create(
            task="Recurring",
            schedule_type="recurring",
            interval_seconds=3600,
        )
        now = datetime.now(UTC)
        await schedule_mgr.advance(schedule.id, now)

        updated = await schedule_mgr.get(schedule.id)
        assert updated.fire_count == 1
        assert updated.last_fired_at is not None
        # next_fire_at should be ~1 hour from now
        assert updated.next_fire_at > now

    async def test_advance_deactivates_at_max_fires(self, schedule_mgr: ScheduleManager):
        schedule = await schedule_mgr.create(
            task="Limited",
            schedule_type="recurring",
            interval_seconds=3600,
            max_fires=1,
        )
        await schedule_mgr.advance(schedule.id, datetime.now(UTC))

        updated = await schedule_mgr.get(schedule.id)
        assert updated.fire_count == 1
        assert updated.active is False

    async def test_deactivate_once_schedule(self, schedule_mgr: ScheduleManager):
        fire_at = datetime.now(UTC) + timedelta(hours=1)
        schedule = await schedule_mgr.create(
            task="One shot",
            schedule_type="once",
            fire_at=fire_at,
        )
        await schedule_mgr.deactivate(schedule.id)

        updated = await schedule_mgr.get(schedule.id)
        assert updated.active is False

    async def test_list_active_schedules(self, schedule_mgr: ScheduleManager):
        await schedule_mgr.create(
            task="Active",
            schedule_type="recurring",
            interval_seconds=3600,
        )
        s2 = await schedule_mgr.create(
            task="Inactive",
            schedule_type="once",
            fire_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await schedule_mgr.deactivate(s2.id)

        active = await schedule_mgr.list(active_only=True)
        assert len(active) == 1
        assert active[0].task == "Active"

    async def test_list_all_schedules(self, schedule_mgr: ScheduleManager):
        await schedule_mgr.create(
            task="A", schedule_type="recurring", interval_seconds=3600,
        )
        s2 = await schedule_mgr.create(
            task="B", schedule_type="once",
            fire_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await schedule_mgr.deactivate(s2.id)

        all_sched = await schedule_mgr.list(active_only=False)
        assert len(all_sched) == 2

    async def test_get_schedule(self, schedule_mgr: ScheduleManager):
        schedule = await schedule_mgr.create(
            task="Get me",
            schedule_type="recurring",
            interval_seconds=3600,
        )
        fetched = await schedule_mgr.get(schedule.id)
        assert fetched is not None
        assert fetched.task == "Get me"

    async def test_get_nonexistent_schedule(self, schedule_mgr: ScheduleManager):
        import uuid
        result = await schedule_mgr.get(uuid.uuid4())
        assert result is None

    async def test_advance_cron_schedule(self, schedule_mgr: ScheduleManager):
        schedule = await schedule_mgr.create(
            task="Cron recurring",
            schedule_type="recurring",
            cron_expr="0 */6 * * *",  # Every 6 hours
        )
        now = datetime.now(UTC)
        await schedule_mgr.advance(schedule.id, now)

        updated = await schedule_mgr.get(schedule.id)
        assert updated.fire_count == 1
        assert updated.next_fire_at > now

    async def test_schedule_notify_defaults_false(self, schedule_mgr: ScheduleManager):
        from datetime import UTC, datetime, timedelta
        fire_at = datetime.now(UTC) + timedelta(hours=1)
        schedule = await schedule_mgr.create(
            task="Check status",
            schedule_type="once",
            fire_at=fire_at,
        )
        assert schedule.notify is False

    async def test_create_schedule_with_model(self, schedule_mgr: ScheduleManager):
        fire_at = datetime.now(UTC) + timedelta(hours=1)
        schedule = await schedule_mgr.create(
            task="Research topic",
            schedule_type="once",
            fire_at=fire_at,
            model="claude-haiku-3-5-20241022",
        )
        assert schedule.model == "claude-haiku-3-5-20241022"

    async def test_create_schedule_with_frame_type(self, schedule_mgr: ScheduleManager):
        fire_at = datetime.now(UTC) + timedelta(hours=1)
        schedule = await schedule_mgr.create(
            task="Research topic",
            schedule_type="once",
            fire_at=fire_at,
            frame_type="research",
        )
        assert schedule.frame_type == "research"

    async def test_schedule_model_defaults_none(self, schedule_mgr: ScheduleManager):
        fire_at = datetime.now(UTC) + timedelta(hours=1)
        schedule = await schedule_mgr.create(
            task="Check status",
            schedule_type="once",
            fire_at=fire_at,
        )
        assert schedule.model is None
        assert schedule.frame_type is None


# ---------------------------------------------------------------------------
# TaskScheduler tests
# ---------------------------------------------------------------------------


class TestTaskScheduler:
    """Tests for the background task scheduler."""

    @pytest.fixture
    def scheduler_settings(self):
        return Settings(schedule_check_interval=1)

    @pytest_asyncio.fixture
    async def scheduler_heart(self, db, scheduler_settings):
        heart = Heart(db, scheduler_settings)
        yield heart
        await heart.close()

    async def test_fires_due_once_schedule(self, scheduler_heart, scheduler_settings):
        """A due once-schedule creates a subtask and deactivates."""
        scheduler = TaskScheduler(scheduler_heart, scheduler_settings)

        # Create a schedule that is already due
        past = datetime.now(UTC) - timedelta(minutes=5)
        schedule = await scheduler_heart.schedules.create(
            task="One-shot reminder",
            schedule_type="once",
            fire_at=past,
            timeout=60,
            notify=True,
        )

        fired = await scheduler._fire_due_tasks()
        assert fired == 1

        # Subtask was created
        subtasks = await scheduler_heart.subtasks.list(status="pending")
        assert len(subtasks) >= 1
        assert any(s.task == "One-shot reminder" for s in subtasks)

        # Schedule deactivated
        updated = await scheduler_heart.schedules.get(schedule.id)
        assert updated.active is False

    async def test_fires_and_advances_recurring(self, scheduler_heart, scheduler_settings):
        """A due recurring schedule creates a subtask and advances next_fire_at."""
        scheduler = TaskScheduler(scheduler_heart, scheduler_settings)

        # Create a recurring schedule that is already due
        schedule = await scheduler_heart.schedules.create(
            task="Check conditions",
            schedule_type="recurring",
            interval_seconds=3600,
        )
        # Manually set next_fire_at to the past
        async with scheduler_heart.db.session() as sess:
            from sqlalchemy import update as sql_update
            await sess.execute(
                sql_update(Schedule)
                .where(Schedule.id == schedule.id)
                .values(next_fire_at=datetime.now(UTC) - timedelta(minutes=5))
            )
            await sess.commit()

        fired = await scheduler._fire_due_tasks()
        assert fired == 1

        # Subtask created
        subtasks = await scheduler_heart.subtasks.list(status="pending")
        assert any(s.task == "Check conditions" for s in subtasks)

        # Schedule still active with advanced next_fire_at
        updated = await scheduler_heart.schedules.get(schedule.id)
        assert updated.active is True
        assert updated.fire_count == 1
        assert updated.next_fire_at > datetime.now(UTC)

    async def test_skips_future_schedules(self, scheduler_heart, scheduler_settings):
        """Schedules with next_fire_at in the future are not fired."""
        scheduler = TaskScheduler(scheduler_heart, scheduler_settings)

        future = datetime.now(UTC) + timedelta(hours=2)
        await scheduler_heart.schedules.create(
            task="Future task",
            schedule_type="once",
            fire_at=future,
        )

        fired = await scheduler._fire_due_tasks()
        assert fired == 0

        # No subtasks created
        subtasks = await scheduler_heart.subtasks.list(status="pending")
        assert len(subtasks) == 0

    async def test_handles_queue_full_gracefully(self, scheduler_heart, scheduler_settings):
        """When subtask queue is full, scheduler logs warning and continues."""
        scheduler = TaskScheduler(scheduler_heart, scheduler_settings)

        # Fill the pending subtask queue (limit is 5)
        for i in range(5):
            await scheduler_heart.subtasks.create(task=f"Filler {i}")

        # Create a due schedule
        past = datetime.now(UTC) - timedelta(minutes=5)
        await scheduler_heart.schedules.create(
            task="Queue full schedule",
            schedule_type="once",
            fire_at=past,
        )

        # Should not raise, returns 0 (schedule skipped)
        fired = await scheduler._fire_due_tasks()
        assert fired == 0

    async def test_start_and_stop(self, scheduler_heart, scheduler_settings):
        """start() spawns task, stop() cancels it."""
        scheduler = TaskScheduler(scheduler_heart, scheduler_settings)

        await scheduler.start()
        assert scheduler._task is not None
        assert not scheduler._task.done()

        await scheduler.stop()
        assert scheduler._task is None


class TestSchedulerModelPassthrough:
    """Verify scheduler passes model/frame_type when creating subtasks."""

    async def test_scheduler_passes_model_and_frame_type(self):
        import uuid
        from unittest.mock import AsyncMock, MagicMock

        settings = Settings()
        mock_heart = MagicMock()
        mock_heart.subtasks = AsyncMock()
        mock_heart.subtasks.create = AsyncMock()

        mock_schedule = MagicMock()
        mock_schedule.id = uuid.uuid4()
        mock_schedule.task = "Research AI papers"
        mock_schedule.schedule_type = "once"
        mock_schedule.timeout_seconds = 120
        mock_schedule.notify = False
        mock_schedule.created_by_session = "session-123"
        mock_schedule.model = "claude-haiku-3-5-20241022"
        mock_schedule.frame_type = "research"

        mock_heart.schedules = AsyncMock()
        mock_heart.schedules.get_due = AsyncMock(return_value=[mock_schedule])
        mock_heart.schedules.deactivate = AsyncMock()

        scheduler = TaskScheduler(heart=mock_heart, settings=settings)
        await scheduler._fire_due_tasks()

        create_kwargs = mock_heart.subtasks.create.call_args.kwargs
        assert create_kwargs["model"] == "claude-haiku-3-5-20241022"
        assert create_kwargs["frame_type"] == "research"
