"""
Copyright (c) 2015, 2018 Red Hat, Inc
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
from fnmatch import fnmatch
import os.path
from tests.constants import DOCKERFILE_GIT, MOCK
from tests.stubs import StubInsideBuilder, StubSource
if MOCK:
    from tests.retry_mock import mock_get_retry_session
    from tests.docker_mock import mock_docker


repocontent = b'''[repo]\n'''


def prepare():
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow({"provider": "git", "uri": DOCKERFILE_GIT}, "test-image")
    workflow.source = StubSource()
    workflow.builder = StubInsideBuilder().for_workflow(workflow)
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


@pytest.mark.parametrize('base_from_scratch', [True, False])
@pytest.mark.parametrize('parent_images', [True, False])
@pytest.mark.parametrize('inject_proxy', [None, 'http://proxy.example.com'])
@pytest.mark.parametrize(('repos', 'filenames'), (
    (['http://example.com/a/b/c/myrepo.repo', 'http://example.com/repo-2.repo'],
     ['myrepo-d0856.repo', 'repo-2-ba4b3.repo']),
    (['http://example.com/spam/myrepo.repo', 'http://example.com/bacon/myrepo.repo'],
     ['myrepo-608de.repo', 'myrepo-a1f78.repo']),
))
def test_multiple_repourls(caplog, base_from_scratch, parent_images, inject_proxy, repos,
                           filenames):
    tasker, workflow = prepare()
    workflow.builder.base_from_scratch = base_from_scratch
    workflow.builder.parent_images = parent_images
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': {'repourls': repos, 'inject_proxy': inject_proxy}}])
    runner.run()

    if base_from_scratch and not parent_images:
        assert AddYumRepoByUrlPlugin.key is not None
        assert workflow.files == {}
        log_msg = "Skipping add yum repo by url: unsupported for FROM-scratch images"
        assert log_msg in caplog.text()
    else:
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
    pattern = 'example repo-?????.repo'
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': {'repourls': [url], 'inject_proxy': inject_proxy}}])
    runner.run()
    repo_content = repocontent
    if inject_proxy:
        repo_content = '%sproxy = %s\n\n' % (repocontent.decode('utf-8'), inject_proxy)
    # next(iter(...)) is for py 2/3 compatibility
    assert fnmatch(next(iter(workflow.files.keys())), os.path.join(YUM_REPOS_DIR, pattern))
    assert next(iter(workflow.files.values())) == repo_content
    assert len(workflow.files) == 1


@pytest.mark.parametrize('inject_proxy', [None, 'http://proxy.example.com'])
@pytest.mark.parametrize(('repos', 'patterns'), (
    (['http://example.com/a/b/c/myrepo', 'http://example.com/repo-2.repo'],
     ['myrepo-?????.repo', 'repo-2-?????.repo']),
    (['http://example.com/a/b/c/myrepo', 'http://example.com/repo-2'],
     ['myrepo-?????.repo', 'repo-2-?????.repo']),
    (['http://example.com/a/b/c/myrepo.repo', 'http://example.com/repo-2'],
     ['myrepo-?????.repo', 'repo-2-?????.repo']),
    (['http://example.com/spam/myrepo.repo', 'http://example.com/bacon/myrepo'],
     ['myrepo-?????.repo', 'myrepo-?????.repo']),
    (['http://example.com/a/b/c/myrepo.repo?blab=bla', 'http://example.com/a/b/c/repo-2?blab=bla'],
     ['myrepo-?????.repo', 'repo-2-?????.repo']),
    (['http://example.com/a/b/c/myrepo', 'http://example.com/a/b/c/myrepo.repo'],
     ['myrepo-?????.repo', 'myrepo-?????.repo']),
))
def test_multiple_repourls_no_suffix(inject_proxy, repos, patterns):
    tasker, workflow = prepare()
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': AddYumRepoByUrlPlugin.key,
        'args': {'repourls': repos, 'inject_proxy': inject_proxy}}])
    runner.run()
    repo_content = repocontent
    if inject_proxy:
        repo_content = '%sproxy = %s\n\n' % (repocontent.decode('utf-8'), inject_proxy)

    assert len(workflow.files) == 2
    for pattern in patterns:
        for filename, content in workflow.files.items():
            if fnmatch(filename, os.path.join(YUM_REPOS_DIR, pattern)):
                assert content == repo_content  # only because they're all the same
                del workflow.files[filename]
                break
        else:
            raise RuntimeError("no filename in %s matching pattern %s" %
                               (list(workflow.files.keys()), pattern))


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
