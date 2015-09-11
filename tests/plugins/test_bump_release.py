"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

try:
    import pygit2
except ImportError:
    pygit2 = None

import pytest
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.\
    pre_check_and_set_rebuild import CheckAndSetRebuildPlugin
from atomic_reactor.plugins.pre_bump_release import BumpReleasePlugin
from atomic_reactor.source import GitSource
from atomic_reactor.util import ImageName
from tests.constants import SOURCE, MOCK
if MOCK:
    from tests.docker_mock import mock_docker

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


class MockedPopen(subprocess.Popen):
    def __init__(self, cmd, *args, **kwargs):
        # Don't actually push any commits
        if 'push' in cmd:
            cmd = ['/bin/echo', 'push faked']

        # Deal with there being no remote for our branch
        elif cmd[1:] == ['config', '--get', 'branch.%s.remote' % BRANCH]:
            cmd = ['/bin/echo', 'origin']
        elif cmd[1] == 'rev-parse' and cmd[2].startswith('origin/'):
            cmd[2] = cmd[2][len('origin/'):]

        super(MockedPopen, self).__init__(cmd, *args, **kwargs)


def prepare(tmpdir, df_path, git_ref, source=None,
            build_process_failed=False, is_rebuild=True,
            author_name=None, author_email=None,
            commit_message=None, git_commit=None):
    if author_name is None:
        author_name = "OSBS Build System"
    if author_email is None:
        author_email = "root@example.com"

    if MOCK:
        mock_docker()
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
    flexmock(subprocess, Popen=MockedPopen)
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


@pytest.mark.skipif(pygit2 is None,
                    reason="pygit2 required for this test")
def test_bump_release_failed_build(tmpdir):
    with DFWithRelease() as (df_path, commit):
        workflow, args, runner = prepare(tmpdir, df_path, commit,
                                         build_process_failed=True)
        original_content = open(df_path).readlines()
        runner.run()
        assert open(df_path).readlines() == original_content


@pytest.mark.skipif(pygit2 is None,
                    reason="pygit2 required for this test")
def test_bump_release_not_rebuild_extra_commits(tmpdir):
    with DFWithRelease() as (df_path, commit):
        workflow, args, runner = prepare(tmpdir, df_path, "wrongcommit",
                                         is_rebuild=False)
        with pytest.raises(PluginFailedException):
            runner.run()


@pytest.mark.skipif(pygit2 is None,
                    reason="pygit2 required for this test")
def test_bump_release_not_rebuild_no_extra_commits(tmpdir):
    with DFWithRelease() as (df_path, commit):
        workflow, args, runner = prepare(tmpdir, df_path, commit,
                                         is_rebuild=False)
        runner.run()


@pytest.mark.skipif(pygit2 is None,
                    reason="pygit2 required for this test")
def test_bump_release_branch_not_found(tmpdir):
    with DFWithRelease() as (df_path, commit):
        workflow, args, runner = prepare(tmpdir, df_path, commit,
                                         git_commit='wrong')
        with pytest.raises(PluginFailedException):
            runner.run()


@pytest.mark.skipif(pygit2 is None,
                    reason="pygit2 required for this test")
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
    with DFWithRelease(label=label) as (df_path, commit):
        workflow, args, runner = prepare(tmpdir, df_path, commit,
                                         commit_message='foo')
        runner.run()
        assert open(df_path).readlines()[1].rstrip() == expected
        repo = pygit2.init_repository(os.path.dirname(df_path))

        # We're on the 'branch' branch
        assert repo.head.name == repo.lookup_branch(BRANCH).name

        # and one commit ahead of where we were
        assert repo.head.peel().parents[0].hex == commit

        # Examine metadata for newest commit
        author = repo.head.peel().author
        assert author.name == args['author_name']
        assert author.email == args['author_email']

        committer = repo.head.peel().committer
        assert ('committer_name' not in args or
                committer.name == args['committer_name'])
        assert ('committer_email' not in args or
                committer.email == args['committer_email'])

        if 'commit_message' in args:
            assert repo.head.peel().message.rstrip() == args['commit_message']
