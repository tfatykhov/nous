"""F099 Phase 3a: the card plumbing (R5): the router holds the runner; the runner and publisher get the service."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import nous.main as main
from nous.a2ui.actions import ActionRouter
from nous.config import Settings
from nous.handlers.continuation_publisher import OwnerPublisher
from nous.handlers.continuation_runner import ContinuationRunner

CARDS = {
    "result_inbox_enabled": True,
    "intentions_enabled": True,
    "continuation_enabled": True,
    "intention_cards_enabled": True,
}


def test_the_router_holds_the_continuation_runner():
    runner = object()
    assert ActionRouter(None, Settings(_env_file=None), None, continuation_runner=runner)._continuation_runner is runner
    assert ActionRouter(None, Settings(_env_file=None), None)._continuation_runner is None  # PIN: the default


def test_the_publisher_takes_the_service_at_construction_or_late():
    service = object()
    assert OwnerPublisher(database=None, settings=Settings(_env_file=None))._surface_service is None
    assert (
        OwnerPublisher(database=None, settings=Settings(_env_file=None), surface_service=service)._surface_service
        is service
    )
    late = OwnerPublisher(database=None, settings=Settings(_env_file=None))
    late.set_surface_service(service)
    assert late._surface_service is service


def test_the_runner_hands_the_service_to_its_publisher():
    publisher = OwnerPublisher(database=None, settings=Settings(_env_file=None, **CARDS))
    runner = ContinuationRunner(
        database=None,
        settings=Settings(_env_file=None, **CARDS),
        runner=None,
        heart=None,
        brain=None,
        publisher=publisher,
    )
    assert runner._surface_service is None  # until main.py binds it: every card step is then a no-op
    service = object()
    runner.set_surface_service(service)
    assert runner._surface_service is service and publisher._surface_service is service


def test_a_publisher_that_cannot_take_a_service_does_not_break_the_binding():
    runner = ContinuationRunner(
        database=None,
        settings=Settings(_env_file=None),
        runner=None,
        heart=None,
        brain=None,
        publisher=SimpleNamespace(),
    )
    runner.set_surface_service(object())  # no raise


def test_main_binds_the_service_where_it_is_built_and_passes_the_runner_to_the_router():  # PIN
    """Mutation: bind it before ``SurfaceService(`` is built, or drop ``continuation_runner=`` from the router call."""
    source = inspect.getsource(main.create_components)
    built = source.index("surface_service = SurfaceService(")
    bound = source.index("continuation_runner.set_surface_service(surface_service)")
    assert built < bound < source.index("# F038: DAG Orchestration")
    assert source.rindex("if continuation_runner is not None:", 0, bound) > built
    router_call = source[source.index("action_router = ActionRouter(") :]
    assert "continuation_runner=continuation_runner," in router_call[: router_call.index("\n        )")]
