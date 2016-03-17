"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import json
import os
import subprocess

import pytest
from flexmock import flexmock
from atomic_reactor.constants import EXPORTED_SQUASHED_IMAGE_NAME

from atomic_reactor.util import ImageName
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner
from atomic_reactor.plugins.post_cp_built_image_to_nfs import CopyBuiltImageToNFSPlugin
import osbs.conf
from osbs.api import OSBS
from tests.constants import INPUT_IMAGE
from tests.fixtures import docker_tasker

class Y(object):
    pass


class X(object):
    image_id = INPUT_IMAGE
    source = Y()
    source.dockerfile_path = None
    source.path = None
    base_image = ImageName(repo="qwe", tag="asd")


NFS_SERVER_PATH = "server:path"


@pytest.mark.parametrize(('nfs_server_path', 'detect'), [
    (NFS_SERVER_PATH, False),
    (NFS_SERVER_PATH.split(':')[1], True),
])
@pytest.mark.parametrize('dest_dir', [None, "test_directory"])
def test_cp_built_image_to_nfs(tmpdir, monkeypatch, docker_tasker, dest_dir,
                               nfs_server_path, detect):
    mountpoint = tmpdir.join("mountpoint")

    def fake_check_call(cmd):
        assert cmd == [
            "mount",
            "-t", "nfs",
            "-o", "nolock",
            NFS_SERVER_PATH,
            mountpoint,
        ]
    flexmock(subprocess, check_call=fake_check_call)

    fake_conf = osbs.conf.Configuration(conf_file=None, openshift_uri='/')
    flexmock(osbs.conf).should_receive('Configuration').and_return(fake_conf)

    class FakePod(object):
        def get_host(self):
            return NFS_SERVER_PATH.split(':')[0]

    expectation = (flexmock(OSBS)
                   .should_receive('get_pod_for_build')
                   .and_return(FakePod()))
    if detect:
        expectation.once()
    else:
        expectation.never()

    monkeypatch.setenv("BUILD", json.dumps({
        'metadata': {'name': ''},
    }))

    workflow = DockerBuildWorkflow({"provider": "git", "uri": "asd"}, "test-image")
    workflow.builder = X()
    workflow.exported_image_sequence.append({"path": os.path.join(str(tmpdir),
                                                             EXPORTED_SQUASHED_IMAGE_NAME)})
    open(workflow.exported_image_sequence[-1].get("path"), 'a').close()

    runner = PostBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': CopyBuiltImageToNFSPlugin.key,
            'args': {
                "nfs_server_path": nfs_server_path,
                "dest_dir": dest_dir,
                "mountpoint": str(mountpoint),
                "url": '/',
            }
        }]
    )
    runner.run()
    if dest_dir is None:
        assert os.path.isfile(os.path.join(str(mountpoint), EXPORTED_SQUASHED_IMAGE_NAME))
    else:
        assert os.path.isfile(os.path.join(str(mountpoint), dest_dir, EXPORTED_SQUASHED_IMAGE_NAME))

    result  = workflow.postbuild_results[CopyBuiltImageToNFSPlugin.key]
    assert result == NFS_SERVER_PATH.split(':')[0]
