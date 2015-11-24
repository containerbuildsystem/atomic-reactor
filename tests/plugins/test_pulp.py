"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import os

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner
from atomic_reactor.util import ImageName
try:
    import dockpulp
    from atomic_reactor.plugins.post_push_to_pulp import PulpPushPlugin
except (ImportError, SyntaxError):
    dockpulp = None

import pytest
from flexmock import flexmock
from tests.constants import INPUT_IMAGE, SOURCE, MOCK
if MOCK:
    from tests.docker_mock import mock_docker


class X(object):
    image_id = INPUT_IMAGE
    git_dockerfile_path = None
    git_path = None
    base_image = ImageName(repo="qwe", tag="asd")


def prepare(check_repo_retval=0):
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)
    setattr(workflow, 'tag_conf', X())
    setattr(workflow.tag_conf, 'images', [ImageName(repo="image-name1"),
                                          ImageName(namespace="prefix",
                                                    repo="image-name2"),
                                          ImageName(repo="image-name3", tag="asd")])

    # Mock dockpulp and docker
    dockpulp.Pulp = flexmock(dockpulp.Pulp)
    dockpulp.Pulp.registry='registry.example.com'
    (flexmock(dockpulp.imgutils).should_receive('get_metadata')
     .with_args(object)
     .and_return([{'id': 'foo'}]))
    (flexmock(dockpulp.imgutils).should_receive('get_versions')
     .with_args(object)
     .and_return({'id': '1.6.0'}))
    (flexmock(dockpulp.imgutils).should_receive('check_repo')
     .and_return(check_repo_retval))
    (flexmock(dockpulp.Pulp)
     .should_receive('set_certs')
     .with_args(object, object))
    (flexmock(dockpulp.Pulp)
     .should_receive('getRepos')
     .with_args(list, fields=list)
     .and_return([
         {"id": "redhat-image-name1"},
         {"id": "redhat-prefix-image-name2"}
      ]))
    (flexmock(dockpulp.Pulp)
     .should_receive('createRepo'))
    (flexmock(dockpulp.Pulp)
     .should_receive('upload')
     .with_args(unicode))
    (flexmock(dockpulp.Pulp)
     .should_receive('copy')
     .with_args(unicode, unicode))
    (flexmock(dockpulp.Pulp)
     .should_receive('updateRepo')
     .with_args(unicode, dict))
    (flexmock(dockpulp.Pulp)
     .should_receive('crane')
     .with_args(list, wait=True)
     .and_return([2, 3, 4]))
    (flexmock(dockpulp.Pulp)
     .should_receive('')
     .with_args(object, object)
     .and_return([1, 2, 3]))
    (flexmock(dockpulp.Pulp)
     .should_receive('watch_tasks')
     .with_args(list))
    mock_docker()
    return tasker, workflow


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
@pytest.mark.parametrize(("check_repo_retval", "should_raise"), [
    (3, True),
    (2, True),
    (1, True),
    (0, False),
])
def test_pulp_source_secret(tmpdir, check_repo_retval, should_raise, monkeypatch):
    tasker, workflow = prepare(check_repo_retval=check_repo_retval)
    monkeypatch.setenv('SOURCE_SECRET_PATH', str(tmpdir))
    with open(os.path.join(str(tmpdir), "pulp.cer"), "wt") as cer:
        cer.write("pulp certificate\n")
    with open(os.path.join(str(tmpdir), "pulp.key"), "wt") as key:
        key.write("pulp key\n")

    runner = PostBuildPluginsRunner(tasker, workflow, [{
        'name': PulpPushPlugin.key,
        'args': {
            'pulp_registry_name': 'test'
        }}])

    if should_raise:
        with pytest.raises(Exception) as exc:
            runner.run()

        return

    runner.run()
    assert PulpPushPlugin.key is not None
    images = [i.to_str() for i in workflow.postbuild_results[PulpPushPlugin.key]]
    assert "registry.example.com/image-name1:latest" in images
    assert "registry.example.com/prefix/image-name2:latest" in images
    assert "registry.example.com/image-name3:asd" in images


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
def test_pulp_service_account_secret(tmpdir, monkeypatch):
    tasker, workflow = prepare()
    monkeypatch.setenv('SOURCE_SECRET_PATH', str(tmpdir) + "/not-used")
    with open(os.path.join(str(tmpdir), "pulp.cer"), "wt") as cer:
        cer.write("pulp certificate\n")
    with open(os.path.join(str(tmpdir), "pulp.key"), "wt") as key:
        key.write("pulp key\n")

    runner = PostBuildPluginsRunner(tasker, workflow, [{
        'name': PulpPushPlugin.key,
        'args': {
            'pulp_registry_name': 'test',
            'pulp_secret_path': str(tmpdir),
        }}])

    runner.run()
    images = [i.to_str() for i in workflow.postbuild_results[PulpPushPlugin.key]]
    assert "registry.example.com/image-name1:latest" in images
    assert "registry.example.com/prefix/image-name2:latest" in images
    assert "registry.example.com/image-name3:asd" in images
