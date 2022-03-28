"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from flexmock import flexmock
from textwrap import dedent

from atomic_reactor.cli import job
from atomic_reactor.utils.remote_host import RemoteHost
from osbs.tekton import PipelineRun


NAMSPACE = 'test_namespace'
SLOTS_DIR = "/home/builder/osbs_slots"
REQUIRED_CONFIG = """\
version: 1
koji:
  hub_url: /
  root_url: ''
  auth: {}
openshift:
  url: openshift_url
source_registry:
  url: source_registry.com
registries:
  - url: registry_url
remote_hosts:
  slots_dir: {}
  pools:
    x86_64:
      remote-host-001:
        enabled: true
        auth: /path/to/key
        username: builder
        slots: 3
        socket_path: "/path/to/podman/socket.sock"
""".format("{}", SLOTS_DIR)


def test_remote_hosts_unlocking_recovery(tmp_path, caplog):
    flexmock(RemoteHost).should_receive('is_operational').and_return(True)

    flexmock(RemoteHost).should_receive('prid_in_slot').with_args(0).and_return('pr123').once()
    flexmock(RemoteHost).should_receive('prid_in_slot').with_args(1).and_return('pr124').once()
    flexmock(RemoteHost).should_receive('prid_in_slot').with_args(2).and_return('pr125').once()

    flexmock(RemoteHost).should_receive('unlock').with_args(0, 'pr123').times(0)
    flexmock(RemoteHost).should_receive('unlock').with_args(1, 'pr124').and_return(True).once()
    flexmock(RemoteHost).should_receive('unlock').with_args(2, 'pr125').and_return(True).once()

    config_yaml = tmp_path / 'config.yaml'
    config = REQUIRED_CONFIG
    config_yaml.write_text(dedent(config), "utf-8")

    # 1st pipeline running, 2nd finished, 3rd finished
    (flexmock(PipelineRun).should_receive("has_not_finished")
     .and_return(True)
     .and_return(False)
     .and_return(False))

    job.remote_hosts_unlocking_recovery({'config_file': str(config_yaml),
                                         'namespace': NAMSPACE})

    slot0_msg = 'slot: 0 is occupied by prid: pr123'
    slot1_msg = 'slot: 1 is occupied by prid: pr124'
    slot2_msg = 'slot: 2 is occupied by prid: pr125'
    assert slot0_msg in caplog.text
    assert slot1_msg in caplog.text
    assert slot2_msg in caplog.text
    unlock1_msg = 'pr124 finished, will unlock slot: 1'
    unlock2_msg = 'pr125 finished, will unlock slot: 2'
    assert unlock1_msg in caplog.text
    assert unlock2_msg in caplog.text
    unlock0_msg = 'pr123 finished, will unlock slot: 0'
    assert unlock0_msg not in caplog.text
