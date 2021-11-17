"""
Copyright (c) 2015, 2018, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import koji
import os

from atomic_reactor.plugins.pre_koji import KojiPlugin
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.util import DockerfileImages
from flexmock import flexmock
from fnmatch import fnmatch
import pytest
from tests.stubs import StubSource
from tests.util import add_koji_map_in_workflow


KOJI_TARGET = "target"
KOJI_TARGET_BROKEN_TAG = "target-broken"
KOJI_TARGET_BROKEN_REPO = "target-broken-repo"
KOJI_TAG = "tag"
KOJI_BROKEN_TAG = "tag-broken"
KOJI_BROKEN_REPO = "tag-broken-repo"
GET_TARGET_RESPONSE = {"build_tag_name": KOJI_TAG}
BROKEN_TAG_RESPONSE = {"build_tag_name": KOJI_BROKEN_TAG}
BROKEN_REPO_RESPONSE = {"build_tag_name": KOJI_BROKEN_REPO}
TAG_ID = "1"
BROKEN_REPO_TAG_ID = "2"
GET_TAG_RESPONSE = {"id": TAG_ID, "name": KOJI_TAG}
REPO_ID = "2"
BROKEN_REPO_ID = "3"
REPO_BROKEN_TAG_RESPONSE = {"id": BROKEN_REPO_ID, "name": KOJI_BROKEN_REPO}
GET_REPO_RESPONSE = {"id": "2"}
ROOT = "http://example.com"


# ClientSession is xmlrpc instance, we need to mock it explicitly
class MockedClientSession(object):
    def __init__(self, hub, opts=None):
        self.ca_path = None
        self.cert_path = None
        self.serverca_path = None

    def getBuildTarget(self, target):
        if target == KOJI_TARGET_BROKEN_TAG:
            return BROKEN_TAG_RESPONSE
        if target == KOJI_TARGET_BROKEN_REPO:
            return BROKEN_REPO_RESPONSE
        return GET_TARGET_RESPONSE

    def getTag(self, tag):
        if tag == KOJI_BROKEN_TAG:
            return None
        if tag == KOJI_BROKEN_REPO:
            return REPO_BROKEN_TAG_RESPONSE
        return GET_TAG_RESPONSE

    def getRepo(self, repo):
        if repo == BROKEN_REPO_ID:
            return None
        return GET_REPO_RESPONSE

    def ssl_login(self, cert=None, ca=None, serverca=None, proxyuser=None):
        self.ca_path = ca
        self.cert_path = cert
        self.serverca_path = serverca
        return True

    def krb_login(self, *args, **kwargs):
        return True


class MockedPathInfo(object):
    def __init__(self, topdir=None):
        self.topdir = topdir

    def repo(self, repo_id, name):
        return "{0}/repos/{1}/{2}".format(self.topdir, name, repo_id)


def prepare(workflow):
    workflow.source = StubSource()

    session = MockedClientSession(hub='', opts=None)
    workflow.koji_session = session
    flexmock(koji,
             ClientSession=session,
             PathInfo=MockedPathInfo)

    return workflow


@pytest.mark.usefixtures('user_params')
class TestKoji(object):
    @pytest.mark.parametrize('parent_images', [True, False])
    @pytest.mark.parametrize('base_from_scratch', [True, False])
    @pytest.mark.parametrize(('target', 'expect_success'), [
        (KOJI_TARGET, True),
        (KOJI_TARGET_BROKEN_TAG, False),
        (KOJI_TARGET_BROKEN_REPO, False)])
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

        # https with a cert for authentication
        ('https://nosuchwebsiteforsure.com',
         True,
         'sslverify=0',
         None,
         'http://proxy.example.com'),


    ])
    def test_koji_plugin(self, workflow, source_dir, caplog,
                         parent_images, base_from_scratch,
                         target, expect_success, root, koji_ssl_certs,
                         expected_string, expected_file, proxy):
        prepare(workflow)
        dockerfile_images = []
        if parent_images:
            dockerfile_images.append('parent_image:latest')
        if base_from_scratch:
            dockerfile_images.append('scratch')
        workflow.dockerfile_images = DockerfileImages(dockerfile_images)

        args = {'target': target}

        if koji_ssl_certs:
            source_dir.joinpath("cert").write_text("cert", "utf-8")
            source_dir.joinpath("serverca").write_text("serverca", "utf-8")

        workflow.conf.conf = {'version': 1, 'yum_proxy': proxy}
        add_koji_map_in_workflow(workflow, hub_url='', root_url=root,
                                 ssl_certs_dir=str(source_dir) if koji_ssl_certs else None)

        runner = PreBuildPluginsRunner(workflow, [{
            'name': KojiPlugin.key,
            'args': args,
        }])

        runner.run()

        if base_from_scratch and not parent_images:
            log_msg = "from scratch single stage can't add repos from koji target"
            assert log_msg in caplog.text
            return
        if not expect_success:
            return

        if koji_ssl_certs:
            for file_path, expected in [(workflow.koji_session.cert_path, 'cert'),
                                        (workflow.koji_session.serverca_path, 'serverca')]:

                assert os.path.isfile(file_path)
                with open(file_path, 'r') as fd:
                    assert fd.read() == expected

        repofile = '/etc/yum.repos.d/target-?????.repo'
        assert len(workflow.files) == 1
        assert fnmatch(next(iter(workflow.files.keys())), repofile)
        content = next(iter(workflow.files.values()))
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

    @pytest.mark.parametrize('target, yum_repos, include_repo', [
        ('target', ['repo'], False),
        (None, ['repo'], False),
        (None, ['repo'], True),
        (None, [], False),
        (None, [], True),
    ])
    def test_skip_plugin(self, workflow, caplog, target, yum_repos, include_repo):
        prepare(workflow)
        args = {'target': target}

        add_koji_map_in_workflow(workflow, hub_url='', root_url='http://example.com')

        workflow.user_params['include_koji_repo'] = include_repo
        workflow.user_params['yum_repourls'] = yum_repos

        runner = PreBuildPluginsRunner(workflow, [{
            'name': KojiPlugin.key,
            'args': args,
        }])

        runner.run()

        if (not include_repo and yum_repos):
            log_msg = 'there is a yum repo user parameter, skipping plugin'
        else:
            log_msg = 'no target provided, skipping plugin'

        assert log_msg in caplog.text
