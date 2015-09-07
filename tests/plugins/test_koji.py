"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, unicode_literals

try:
    import koji as koji
except ImportError:
    import inspect
    import os
    import sys

    # Find our mocked koji module
    import tests.koji as koji
    mock_koji_path = os.path.dirname(inspect.getfile(koji.ClientSession))
    if mock_koji_path not in sys.path:
        sys.path.append(os.path.dirname(mock_koji_path))

    # Now load it properly, the same way the plugin will
    del koji
    import koji as koji

from atomic_reactor.plugins.pre_koji import KojiPlugin
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.util import ImageName
from flexmock import flexmock
import pytest
from tests.constants import SOURCE, MOCK
if MOCK:
    from tests.docker_mock import mock_docker


class X(object):
    pass


KOJI_TARGET = "target"
GET_TARGET_RESPONSE = {"build_tag_name": "asd"}
KOJI_TAG = "tag"
TAG_ID = "1"
GET_TAG_RESPONSE = {"id": TAG_ID, "name": KOJI_TAG}
REPO_ID = "2"
GET_REPO_RESPONSE = {"id": "2"}
ROOT = "http://example.com"


# ClientSession is xmlrpc instance, we need to mock it explicitly
class MockedClientSession(object):
    def __init__(self, hub):
        pass

    def getBuildTarget(self, target):
        return GET_TARGET_RESPONSE

    def getTag(self, tag):
        return GET_TAG_RESPONSE

    def getRepo(self, repo):
        return GET_REPO_RESPONSE


class MockedPathInfo(object):
    def __init__(self, topdir=None):
        self.topdir = topdir

    def repo(self, repo_id, name):
        return "{0}/repos/{1}/{2}".format(self.topdir, name, repo_id)


def prepare():
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())

    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='21'))
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)

    flexmock(koji,
             ClientSession=MockedClientSession,
             PathInfo=MockedPathInfo)

    return tasker, workflow


def test_koji_plugin():
    tasker, workflow = prepare()
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': KojiPlugin.key,
        'args': {
            "target": KOJI_TARGET,
            "hub": "",
            "root": ROOT,
        }
    }])
    runner.run()
    assert list(workflow.files.keys())[0] == "/etc/yum.repos.d/target.repo"
    assert list(workflow.files.values())[0].startswith("[atomic-reactor-koji-plugin-target]\n")
    assert "gpgcheck=0\n" in list(workflow.files.values())[0]
    assert "enabled=1\n" in list(workflow.files.values())[0]
    assert "name=atomic-reactor-koji-plugin-target\n" in list(workflow.files.values())[0]
    assert "baseurl=http://example.com/repos/tag/2/$basearch\n" in list(workflow.files.values())[0]
