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


class TestKoji(object):
    @pytest.mark.parametrize(('root',
                              'koji_ssl_certs',
                              'expected_string',
                              'expected_file',
                              'proxy'), [
        # Plain http repo
        ('http://example.com',
         False,
         None,
         None,
         None),

        # Plain http repo with proxy
        ('http://example.com',
         False,
         None,
         None,
         'http://proxy.example.com'),

        # https with koji_ssl_certs
        # ('https://example.com',
        #  True,
        #  'sslcacert=',
        #  '/etc/yum.repos.d/example.com.cert'),

        # https with no cert available
        ('https://nosuchwebsiteforsure.com',
         False,
         'sslverify=0',
         None,
         None),

        # https with no cert available
        ('https://nosuchwebsiteforsure.com',
         False,
         'sslverify=0',
         None,
         'http://proxy.example.com'),

        # https with cert available
        # ('https://example.com',
        #  False,
        #  'sslcacert=/etc/yum.repos.d/example.com.cert',
        #  '/etc/yum.repos.d/example.com.cert'),

    ])
    def test_koji_plugin(self, tmpdir, root, koji_ssl_certs,
                         expected_string, expected_file, proxy):
        tasker, workflow = prepare()
        args = {
            'target': KOJI_TARGET,
            'hub': '',
            'root': root,
            'proxy': proxy
        }

        if koji_ssl_certs:
            args['koji_ssl_certs'] = str(tmpdir)
            with open('{}/ca'.format(tmpdir), 'w') as ca_fd:
                ca_fd.write('ca')

        runner = PreBuildPluginsRunner(tasker, workflow, [{
            'name': KojiPlugin.key,
            'args': args,
        }])

        runner.run()
        repofile = '/etc/yum.repos.d/target.repo'
        assert repofile in workflow.files
        content = workflow.files[repofile]
        assert content.startswith("[atomic-reactor-koji-plugin-target]\n")
        assert "gpgcheck=0\n" in content
        assert "enabled=1\n" in content
        assert "name=atomic-reactor-koji-plugin-target\n" in content
        assert "baseurl=%s/repos/tag/2/$basearch\n" % root in content

        if proxy:
            assert "proxy=%s" % proxy in content

        if expected_string:
            assert expected_string in content

        if expected_file:
            assert expected_file in workflow.files
