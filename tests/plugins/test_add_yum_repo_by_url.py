"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from atomic_reactor.constants import YUM_REPOS_DIR

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PreBuildPlugin
from atomic_reactor.plugins.pre_add_yum_repo_by_url import AddYumRepoByUrlPlugin
from atomic_reactor.util import ImageName
import requests
from flexmock import flexmock
import os.path
from tests.constants import DOCKERFILE_GIT, MOCK
if MOCK:
    from tests.docker_mock import mock_docker


repocontent = b'''[repo]\n'''


class X(object):
    pass


def prepare():
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow({"provider": "git", "uri": DOCKERFILE_GIT}, "test-image")
    setattr(workflow, 'builder', X())

    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='21'))
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)
    (flexmock(requests.Response, content=repocontent)
        .should_receive('raise_for_status')
        .and_return(None))
    (flexmock(requests, get=lambda *_: requests.Response()))
    return tasker, workflow


def test_no_repourls():
    tasker, workflow = prepare()
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': {'repourls': []}}])
    runner.run()
    assert AddYumRepoByUrlPlugin.key is not None
    assert workflow.files == {}


def test_single_repourl():
    tasker, workflow = prepare()
    url = 'http://example.com/example%20repo.repo'
    filename = 'example repo.repo'
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': {'repourls': [url]}}])
    runner.run()
    # next(iter(...)) is for py 2/3 compatibility
    assert next(iter(workflow.files.keys())) == os.path.join(YUM_REPOS_DIR, filename)
    assert next(iter(workflow.files.values())) == repocontent
    assert len(workflow.files) == 1


def test_multiple_repourls():
    tasker, workflow = prepare()
    url1 = 'http://example.com/a/b/c/myrepo.repo'
    filename1 = 'myrepo.repo'
    url2 = 'http://example.com/repo-2.repo'
    filename2 = 'repo-2.repo'
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': {'repourls': [url1, url2]}}])
    runner.run()
    assert workflow.files[os.path.join(YUM_REPOS_DIR, filename1)]
    assert workflow.files[os.path.join(YUM_REPOS_DIR, filename2)]
    assert workflow.files[os.path.join(YUM_REPOS_DIR, filename1)] == repocontent
    assert workflow.files[os.path.join(YUM_REPOS_DIR, filename2)] == repocontent
    assert len(workflow.files) == 2
