"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

try:
    import pygit2
    NO_PYGIT2 = False
except ImportError:
    NO_PYGIT2 = True
    import inspect
    import os
    import sys

    # Find our mocked pygit2 module
    import tests.pygit2 as pygit2
    mock_pygit2_path = os.path.dirname(os.path.dirname
                                       (inspect.getfile(pygit2.Remote)))
    if mock_pygit2_path not in sys.path:
        sys.path.append(mock_pygit2_path)

    # Now load it properly, the same way the plugin will
    del pygit2
    import pygit2

import pytest
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.\
    pre_check_and_set_rebuild import CheckAndSetRebuildPlugin
from atomic_reactor.plugins.pre_bump_release import BumpReleasePlugin
from atomic_reactor.source import GitSource
from atomic_reactor.util import ImageName
from tests.constants import SOURCE

from copy import deepcopy
import os
import shutil
import tempfile
import subprocess

from flexmock import flexmock


BRANCH = 'branch'


class DFWithRelease(object):
    def __init__(self, label=None):
        self.path = tempfile.mkdtemp()
        if label is None:
            label = "LABEL Release 1"
        self.label = label

    def __fini__(self):
        shutil.rmtree(self.path)

    def __enter__(self):
        repo = pygit2.init_repository(self.path)
        repo.remotes.create('origin', '/dev/null')

        # Set up branch 'master'
        filename = 'Dockerfile'
        dockerfile_path = os.path.join(self.path, filename)
        open(dockerfile_path, mode="w+t").close()
        index = repo.index
        index.add(filename)
        author = pygit2.Signature('Test', 'test@example.com')
        committer = pygit2.Signature('Test', 'test@example.com')
        oid = repo.create_commit('HEAD', author, committer,
                                 '', index.write_tree(), [])
        master = repo.head

        # Now set up our branch
        branch = repo.create_branch(BRANCH, repo.get(oid))
        repo.checkout(refname=branch)
        with open(dockerfile_path, mode="w+t") as dockerfile:
            dockerfile.write('FROM baseimage\n{0}\n'.format(self.label))

        index = repo.index
        index.add(filename)
        repo.create_commit(branch.name, author, committer,
                           '', index.write_tree(),
                           [repo.head.peel().hex])
        branch.upstream = branch
        return dockerfile_path, repo.head.peel().hex

    def __exit__(self, exc, value, tb):
        pass


class X(object):
    pass


class MockRemote(object):
    def __init__(self, has_set_push_url=False):
        self.has_set_push_url = has_set_push_url

    def push(self, name, has_set_push_url=False):
        pass

    @property
    def push_url(self):
        return ''

    @push_url.setter
    def push_url(self, url):
        if self.has_set_push_url:
            raise AttributeError("can't set attribute")

    def save(self):
        if self.has_set_push_url:
            raise AttributeError("must not call save()")


class MockRemotesCollection(object):
    def __init__(self, has_set_push_url=False):
        self.has_set_push_url = has_set_push_url

    def create(self, name, url):
        pass

    def set_push_url(self, name, url):
        if not self.has_set_push_url:
            raise AttributeError("no such attribute 'set_push_url'")

    def __getitem__(self, item):
        return MockRemote(has_set_push_url=self.has_set_push_url)


class MockIndex(object):
    def add(self, name):
        pass

    def write_tree(self):
        pass


class MockSignature(object):
    def __init__(self, name, email):
        self.name = name
        self.email = email


class MockCommit(object):
    def __init__(self):
        self.author = MockSignature('name', 'email')
        self.committer = self.author
        self.hex = '0'


class MockBranch(object):
    def __init__(self, name):
        self.shorthand = self.name = name
        self.target = MockCommit()
        self.upstream = self

    def peel(self):
        return MockCommit()


class MockRepository(object):
    def __init__(self, *args, **kwargs):
        has_set_push_url = kwargs.get('has_set_push_url')
        self.remotes = MockRemotesCollection(has_set_push_url=has_set_push_url)
        self.index = MockIndex()
        self.head = flexmock()
        commit = MockCommit()
        self.head.should_receive('peel').and_return(commit)
        self.config = {}
        self.workdir = '/tmp'

    def create_commit(self, name, author, committer, message, tree, parents):
        pass

    def create_branch(self, name, commit):
        return MockBranch(name)

    def lookup_branch(self, name):
        if name == BRANCH:
            return MockBranch(name)
        else:
            return None

    def get(self, oid):
        pass

    def checkout(self, refname=None):
        pass


def maybe_mock_pygit2(has_set_push_url=None):
    flexmock(pygit2.Remote, push=lambda name: None)
    if NO_PYGIT2 or has_set_push_url is not None:
        flexmock(pygit2, Signature=MockSignature)

        def do_init_repository(*args, **kwargs):
            kwargs['has_set_push_url'] = has_set_push_url
            return MockRepository(*args, **kwargs)

        flexmock(pygit2, init_repository=do_init_repository)


class MockedPopen(object):
    def __init__(self, cmd, *args, **kwargs):
        self.cmd = cmd

    def __enter__(self, *args, **kwargs):
        return self

    def __exit__(self, *args, **kwargs):
        pass

    def kill(self):
        pass

    def poll(self):
        return 0

    def wait(self):
        return 0

    def communicate(self, *args, **kwargs):
        return ('', '')


def fake_Popen(cmd, *args, **kwargs):
    return MockedPopen(cmd, *args, **kwargs)


def prepare(tmpdir, df_path, git_ref, source=None,
            build_process_failed=False, is_rebuild=True,
            author_name=None, author_email=None,
            commit_message=None, git_commit=None):
    if author_name is None:
        author_name = "OSBS Build System"
    if author_email is None:
        author_email = "root@example.com"

    tasker = DockerTasker()
    source = deepcopy(SOURCE)
    source['provider_params']['git_commit'] = git_commit or BRANCH
    workflow = DockerBuildWorkflow(source, "test-image")
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'image_id', 'asd123')
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='22'))
    setattr(workflow.builder, 'source', workflow.source)
    setattr(workflow.builder, 'df_path', df_path)
    setattr(workflow, 'plugin_failed', build_process_failed)
    workflow.prebuild_results[CheckAndSetRebuildPlugin.key] = is_rebuild
    flexmock(GitSource, get=lambda: os.path.dirname(df_path))
    flexmock(subprocess, Popen=fake_Popen)
    args = {
        'git_ref': git_ref,
        'author_name': author_name,
        'author_email': author_email,
        'commit_message': commit_message,
        'push_url': '/',
    }
    for omitted in [x for x in args if args[x] is None]:
        del args[omitted]
    runner = PreBuildPluginsRunner(tasker, workflow,
                                   [
                                       {
                                           'name': BumpReleasePlugin.key,
                                           'args': args,
                                       }
                                   ])
    return workflow, args, runner


def test_bump_release_failed_build(tmpdir):
    maybe_mock_pygit2()
    with DFWithRelease() as (df_path, commit):
        workflow, args, runner = prepare(tmpdir, df_path, commit,
                                         build_process_failed=True)
        original_content = open(df_path).readlines()
        runner.run()
        assert open(df_path).readlines() == original_content


def test_bump_release_not_rebuild_extra_commits(tmpdir):
    maybe_mock_pygit2()
    with DFWithRelease() as (df_path, commit):
        workflow, args, runner = prepare(tmpdir, df_path, "wrongcommit",
                                         is_rebuild=False)
        with pytest.raises(PluginFailedException):
            runner.run()


def test_bump_release_not_rebuild_no_extra_commits(tmpdir):
    maybe_mock_pygit2()
    with DFWithRelease() as (df_path, commit):
        workflow, args, runner = prepare(tmpdir, df_path, commit,
                                         is_rebuild=False)
        runner.run()


def test_bump_release_branch_not_found(tmpdir):
    maybe_mock_pygit2()
    with DFWithRelease() as (df_path, commit):
        workflow, args, runner = prepare(tmpdir, df_path, commit,
                                         git_commit='wrong')
        with pytest.raises(PluginFailedException):
            runner.run()


@pytest.mark.parametrize('has_set_push_url', [True, False])
def test_bump_release_set_push_url(tmpdir, has_set_push_url):
    """
    pygit2-0.22 provides a settable Remote.push_url attribute and a
    save() method, but no RemoteCollection.set_push_url() method.

    pygit2-0.23 provides a RemoteCollection.set_push_url() method, but
    Remote.push_url is not settable and Remote.save() raises an
    exception.

    Test that we can cope with either situation.

    """

    maybe_mock_pygit2(has_set_push_url=has_set_push_url)
    with DFWithRelease() as (df_path, commit):
        workflow, args, runner = prepare(tmpdir, df_path, commit,
                                         commit_message='foo')
        # Test that it doesn't raise an exception
        runner.run()


@pytest.mark.parametrize(('label', 'expected'), [
    # Simple case, no '=' or quotes
    ('LABEL Release 1',
     'LABEL Release 2'),

    # No '=' but quotes
    ('LABEL "Release" "2"',
     'LABEL Release 3'),

    # Deal with another label
    ('LABEL Release 3\nLABEL Name foo',
     'LABEL Release 4'),

    # Simple case, '=' but no quotes
    ('LABEL Release=1',
     'LABEL Release=2'),

    # '=' and quotes
    ('LABEL "Release"="2"',
     'LABEL Release=3'),

    # '=', multiple labels, no quotes
    ('LABEL Name=foo Release=3',
     'LABEL Name=foo Release=4'),

    # '=', multiple labels and quotes
    ('LABEL Name=foo "Release"="4"',
     'LABEL Name=foo Release=5'),

    # Release that's not entirely numeric
    ('LABEL Release=1.1',
     'LABEL Release=2.1'),
])
def test_bump_release(tmpdir, label, expected):
    maybe_mock_pygit2()
    with DFWithRelease(label=label) as (df_path, commit):
        workflow, args, runner = prepare(tmpdir, df_path, commit,
                                         commit_message='foo')
        runner.run()
        assert open(df_path).readlines()[1].rstrip() == expected
        repo = pygit2.init_repository(os.path.dirname(df_path))

        # We're on the 'branch' branch
        assert NO_PYGIT2 or repo.head.name == repo.lookup_branch(BRANCH).name

        # and one commit ahead of where we were
        assert NO_PYGIT2 or repo.head.peel().parents[0].hex == commit

        # Examine metadata for newest commit
        author = repo.head.peel().author
        assert NO_PYGIT2 or author.name == args['author_name']
        assert NO_PYGIT2 or author.email == args['author_email']

        committer = repo.head.peel().committer
        assert (NO_PYGIT2 or
                'committer_name' not in args or
                committer.name == args['committer_name'])
        assert (NO_PYGIT2 or
                'committer_email' not in args or
                committer.email == args['committer_email'])

        if 'commit_message' in args:
            assert (NO_PYGIT2 or
                    repo.head.peel().message == args['commit_message'])
