"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, unicode_literals

try:
    import koji
except ImportError:
    KOJI_FOUND = False
else:
    KOJI_FOUND = True
    from atomic_reactor.plugins.pre_koji import KojiPlugin


from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.util import ImageName
from tests.constants import SOURCE

from flexmock import flexmock
import pytest


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


def prepare():
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())

    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='21'))
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)

    flexmock(koji, ClientSession=MockedClientSession)

    return tasker, workflow


@pytest.mark.skipif(not KOJI_FOUND,
                    reason="koji module is not present in PyPI")
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
