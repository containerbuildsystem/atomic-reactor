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
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_add_yum_repo_by_url import AddYumRepoByUrlPlugin
from atomic_reactor.util import ImageName
from atomic_reactor.yum_util import YumRepo
import requests
import pytest
from flexmock import flexmock
import os.path
from tests.constants import DOCKERFILE_GIT, MOCK
if MOCK:
    from tests.retry_mock import mock_get_retry_session
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
    (flexmock(requests.Session, get=lambda *_: requests.Response()))
    mock_get_retry_session()

    return tasker, workflow


@pytest.mark.parametrize('inject_proxy', [None, 'http://proxy.example.com'])
def test_no_repourls(inject_proxy):
    tasker, workflow = prepare()
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': {'repourls': [], 'inject_proxy': inject_proxy}}])
    runner.run()
    assert AddYumRepoByUrlPlugin.key is not None
    assert workflow.files == {}


@pytest.mark.parametrize('inject_proxy', [None, 'http://proxy.example.com'])
def test_single_repourl(inject_proxy):
    tasker, workflow = prepare()
    url = 'http://example.com/example%20repo.repo'
    filename = 'example repo-4ca91.repo'
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': {'repourls': [url], 'inject_proxy': inject_proxy}}])
    runner.run()
    repo_content = repocontent
    if inject_proxy:
        repo_content = '%sproxy = %s\n\n' % (repocontent.decode('utf-8'), inject_proxy)
    # next(iter(...)) is for py 2/3 compatibility
    assert next(iter(workflow.files.keys())) == os.path.join(YUM_REPOS_DIR, filename)
    assert next(iter(workflow.files.values())) == repo_content
    assert len(workflow.files) == 1


@pytest.mark.parametrize('inject_proxy', [None, 'http://proxy.example.com'])
@pytest.mark.parametrize(('repos', 'filenames'), (
    (['http://example.com/a/b/c/myrepo.repo', 'http://example.com/repo-2.repo'],
     ['myrepo-d0856.repo', 'repo-2-ba4b3.repo']),
    (['http://example.com/spam/myrepo.repo', 'http://example.com/bacon/myrepo.repo'],
     ['myrepo-608de.repo', 'myrepo-a1f78.repo']),
))
def test_multiple_repourls(inject_proxy, repos, filenames):
    tasker, workflow = prepare()
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': {'repourls': repos, 'inject_proxy': inject_proxy}}])
    runner.run()
    repo_content = repocontent
    if inject_proxy:
        repo_content = '%sproxy = %s\n\n' % (repocontent.decode('utf-8'), inject_proxy)

    for filename in filenames:
        assert workflow.files[os.path.join(YUM_REPOS_DIR, filename)]
        assert workflow.files[os.path.join(YUM_REPOS_DIR, filename)] == repo_content

    assert len(workflow.files) == 2


@pytest.mark.parametrize('inject_proxy', [None, 'http://proxy.example.com'])
def test_single_repourl_no_suffix(inject_proxy):
    tasker, workflow = prepare()
    url = 'http://example.com/example%20repo'
    filename = 'example repo-4ca91.repo'
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': {'repourls': [url], 'inject_proxy': inject_proxy}}])
    runner.run()
    repo_content = repocontent
    if inject_proxy:
        repo_content = '%sproxy = %s\n\n' % (repocontent.decode('utf-8'), inject_proxy)
    # next(iter(...)) is for py 2/3 compatibility
    assert next(iter(workflow.files.keys())) == os.path.join(YUM_REPOS_DIR, filename)
    assert next(iter(workflow.files.values())) == repo_content
    assert len(workflow.files) == 1


@pytest.mark.parametrize('inject_proxy', [None, 'http://proxy.example.com'])
@pytest.mark.parametrize(('repos', 'filenames'), (
    (['http://example.com/a/b/c/myrepo', 'http://example.com/repo-2.repo'],
     ['myrepo-d0856.repo', 'repo-2-ba4b3.repo']),
    (['http://example.com/a/b/c/myrepo', 'http://example.com/repo-2'],
     ['myrepo-d0856.repo', 'repo-2-ba4b3.repo']),
    (['http://example.com/a/b/c/myrepo.repo', 'http://example.com/repo-2'],
     ['myrepo-d0856.repo', 'repo-2-ba4b3.repo']),
    (['http://example.com/spam/myrepo.repo', 'http://example.com/bacon/myrepo'],
     ['myrepo-608de.repo', 'myrepo-a1f78.repo']),
))
def test_multiple_repourls_no_suffix(inject_proxy, repos, filenames):
    tasker, workflow = prepare()
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': {'repourls': repos, 'inject_proxy': inject_proxy}}])
    runner.run()
    repo_content = repocontent
    if inject_proxy:
        repo_content = '%sproxy = %s\n\n' % (repocontent.decode('utf-8'), inject_proxy)

    for filename in filenames:
        assert workflow.files[os.path.join(YUM_REPOS_DIR, filename)]
        assert workflow.files[os.path.join(YUM_REPOS_DIR, filename)] == repo_content

    assert len(workflow.files) == 2


@pytest.mark.parametrize('inject_proxy', [None, 'http://proxy.example.com'])
def test_multiple_same_repourls_no_suffix(inject_proxy):
    tasker, workflow = prepare()
    repos = ['http://example.com/a/b/c/myrepo', 'http://example.com/a/b/c/myrepo.repo']
    filename = 'myrepo-d0856.repo'
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': {'repourls': repos, 'inject_proxy': inject_proxy}}])
    runner.run()
    repo_content = repocontent
    if inject_proxy:
        repo_content = '%sproxy = %s\n\n' % (repocontent.decode('utf-8'), inject_proxy)

    assert workflow.files[os.path.join(YUM_REPOS_DIR, filename)]
    assert workflow.files[os.path.join(YUM_REPOS_DIR, filename)] == repo_content

    assert len(workflow.files) == 1


def test_invalid_repourl():
    """Plugin should raise RuntimeError with repo details when invalid URL
       is used
    """
    WRONG_REPO_URL = "http://example.com/nope/repo"
    tasker, workflow = prepare()
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': {'repourls': [WRONG_REPO_URL], 'inject_proxy': None}}])

    (flexmock(YumRepo)
        .should_receive('fetch')
        .and_raise(Exception, 'Oh noes, repo is not working!'))

    with pytest.raises(PluginFailedException) as exc:
        runner.run()

    msg = "Failed to fetch yum repo {repo}".format(repo=WRONG_REPO_URL)
    assert msg in str(exc)
