"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os

from atomic_reactor.source import GitSource
from flexmock import flexmock

from atomic_reactor import source
from atomic_reactor.tasks import common
from atomic_reactor.tasks import clone
from tests.constants import DOCKERFILE_GIT


class TestCloneTask:
    """Tests for the CloneTask class."""

    def test_clone_execute(self, tmpdir):
        params = common.TaskParams(
            build_dir=str(tmpdir),
            context_dir="/context",
            config_file="config.yaml",
            user_params={'user': 'foo',
                         'git_uri': DOCKERFILE_GIT,
                         'git_ref': 'master',
                         'git_commit_depth': 1,
                         'git_branch': 'master'},
        )
        src = params.source

        assert isinstance(src, source.GitSource)
        assert src.workdir == str(tmpdir)

        (flexmock(GitSource)
            .should_receive('get')
            .and_return(os.path.join(str(tmpdir), 'docker-hello-world')))

        clone_task = clone.CloneTask(params)
        clone_task.execute()
