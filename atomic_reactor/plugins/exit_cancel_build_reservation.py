"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import koji

from atomic_reactor.config import get_koji_session
from atomic_reactor.constants import PLUGIN_CANCEL_BUILD_RESERVATION, PROG
from atomic_reactor.plugin import ExitPlugin


class CancelBuildReservation(ExitPlugin):
    """Cancel reserved build."""

    key = PLUGIN_CANCEL_BUILD_RESERVATION

    def run(self):
        if not self.workflow.conf.koji.get('reserve_build', False):
            self.log.debug("Build reservation feature is not enabled. Skip cancelation.")
            return

        reserved_token = self.workflow.data.reserved_token
        reserved_build_id = self.workflow.data.reserved_build_id

        if reserved_token is None and reserved_build_id is None:
            self.log.debug("There is no reserved build. Skip cancelation.")
            return

        session = get_koji_session(self.workflow.conf)
        build_info = session.getBuild(reserved_build_id)

        if build_info is None:
            self.log.warning(
                "Cannot get the reserved build %s from Brew/Koji.", reserved_build_id
            )
            return

        state_building = koji.BUILD_STATES["BUILDING"]
        state_failed = koji.BUILD_STATES["FAILED"]
        cur_state = build_info["state"]
        cur_state_name = koji.BUILD_STATES[cur_state]

        if cur_state != state_building:
            self.log.debug("Reserved build %s is in state %s already. Skip cancelation.",
                           reserved_build_id, cur_state_name)
            return

        if not self.workflow.build_process_failed and cur_state == state_building:
            session.CGRefundBuild(PROG, reserved_build_id, reserved_token, state_failed)
            err_msg = (
                f"Build process succeeds, but the reserved build {reserved_build_id} "
                f"is in state {cur_state_name}. "
                f"Please check if koji_import plugin is configured properly to execute."
            )
            raise RuntimeError(err_msg)

        if self.workflow.data.build_canceled:
            state = koji.BUILD_STATES["CANCELED"]
        else:
            state = state_failed
        session.CGRefundBuild(PROG, reserved_build_id, reserved_token, state)
