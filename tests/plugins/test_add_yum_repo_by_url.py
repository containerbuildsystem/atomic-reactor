"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from dock.constants import YUM_REPOS_DIR

from dock.core import DockerTasker
from dock.inner import DockerBuildWorkflow
from dock.plugin import PreBuildPluginsRunner, PreBuildPlugin
from dock.plugins.pre_add_yum_repo_by_url import AddYumRepoByUrlPlugin
from dock.util import ImageName
from tests.constants import DOCKERFILE_GIT
from tempfile import NamedTemporaryFile
from collections import namedtuple
import requests
from flexmock import flexmock
import os.path


repocontent = b'''[repo]\n'''


class X(object):
    pass


def prepare():
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(DOCKERFILE_GIT, "test-image")
    setattr(workflow, 'builder', X)

    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='21'))
    setattr(workflow.builder, 'git_dockerfile_path', None)
    setattr(workflow.builder, 'git_path', None)
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
