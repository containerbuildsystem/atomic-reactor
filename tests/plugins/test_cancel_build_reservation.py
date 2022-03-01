"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import koji
from atomic_reactor.inner import BuildResult
import pytest
from flexmock import flexmock

from atomic_reactor.constants import PROG
from atomic_reactor.plugins.exit_cancel_build_reservation import CancelBuildReservation


def test_build_reservation_is_not_enabled(workflow, caplog):
    """Skip cancelation if build reservation feature is not enabled."""

    workflow.conf.conf["koji"] = {"reserve_build": False}
    plugin = CancelBuildReservation(workflow)
    plugin.run()
    assert "Build reservation feature is not enabled." in caplog.text


def mock_reactor_config(workflow):
    workflow.conf.conf["koji"] = {
        "reserve_build": True,
        "hub_url": "https://hub.host/",
        "auth": {},
    }


def test_no_build_was_reserved_yet(workflow, caplog):
    """Skip cancelation if there is no reserved build."""
    mock_reactor_config(workflow)
    plugin = CancelBuildReservation(workflow)
    plugin.run()
    assert "There is no reserved build" in caplog.text


class MockClientSession(object):
    def __init__(self, *args, **kwargs):
        pass

    def getBuild(self, build_info):
        return None

    def krb_login(self, *args, **kwargs):
        return True


def test_skip_if_reserved_build_id_not_exist(workflow, caplog):
    mock_reactor_config(workflow)
    workflow.data.reserved_token = "1234"
    workflow.data.reserved_build_id = 1

    flexmock(koji).should_receive("ClientSession").and_return(MockClientSession())

    plugin = CancelBuildReservation(workflow)
    plugin.run()

    assert "Cannot get the reserved build 1" in caplog.text


@pytest.mark.parametrize("build_state", [
    koji.BUILD_STATES["COMPLETE"],
    koji.BUILD_STATES["FAILED"],
    koji.BUILD_STATES["CANCELED"],
])
def test_reserved_build_has_been_released_already(build_state, workflow, caplog):
    """Skip cancelation if a reserved build has been released."""
    mock_reactor_config(workflow)
    workflow.data.reserved_token = "1234"
    workflow.data.reserved_build_id = 1

    class _MockClientSession(MockClientSession):
        def getBuild(self, *args, **kwargs):
            return {"state": build_state}

    flexmock(koji).should_receive("ClientSession").and_return(_MockClientSession())

    plugin = CancelBuildReservation(workflow)
    plugin.run()

    state_name = koji.BUILD_STATES[build_state]
    assert f"Reserved build 1 is in state {state_name} already" in caplog.text


@pytest.mark.parametrize("is_canceled,expected_dest_state", [
    [False, koji.BUILD_STATES["FAILED"]],
    [True, koji.BUILD_STATES["CANCELED"]],
])
def test_cancel_a_reserved_build(is_canceled, expected_dest_state, workflow):
    """A reserved build is canceled with a proper destination state."""
    mock_reactor_config(workflow)
    workflow.data.build_canceled = is_canceled
    workflow.data.reserved_token = "1234"
    workflow.data.reserved_build_id = 1

    class _MockClientSession(MockClientSession):
        def getBuild(self, *args, **kwargs):
            return {"state": koji.BUILD_STATES["BUILDING"]}

        def CGRefundBuild(self, cg, build_id, token, state):
            assert PROG == cg
            assert workflow.data.reserved_token == token
            assert workflow.data.reserved_build_id == build_id
            assert expected_dest_state == state

    flexmock(koji).should_receive("ClientSession").and_return(_MockClientSession())

    plugin = CancelBuildReservation(workflow)
    plugin.run()


def test_mark_reserved_build_fail_if_koji_import_does_not_run(workflow):
    reserved_build_id = 1
    mock_reactor_config(workflow)
    # Mark the build is successful.
    workflow.data.build_result = BuildResult(logs=["Build succeeds."])
    workflow.data.reserved_token = "1234"
    workflow.data.reserved_build_id = reserved_build_id

    class _MockClientSession(MockClientSession):
        def getBuild(self, *args, **kwargs):
            return {"state": koji.BUILD_STATES["BUILDING"]}

        def CGRefundBuild(self, cg, build_id, token, state):
            assert PROG == cg
            assert workflow.data.reserved_token == token
            assert workflow.data.reserved_build_id == build_id
            assert koji.BUILD_STATES["FAILED"] == state

    flexmock(koji).should_receive("ClientSession").and_return(_MockClientSession())

    plugin = CancelBuildReservation(workflow)
    expected_msg = f"but the reserved build {reserved_build_id} is in state BUILDING"

    with pytest.raises(RuntimeError, match=expected_msg):
        plugin.run()
