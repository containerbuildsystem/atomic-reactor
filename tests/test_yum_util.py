"""
Copyright (c) 2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Very small subset of tests for the YumRepo class. Most testing
is done in test_add_yum_repo_by_url
"""
import sys
from atomic_reactor.yum_util import YumRepo
import pytest


@pytest.mark.parametrize(('repourl', 'filename'), (
    ('http://example.com/a/b/c/myrepo.repo', 'myrepo-d0856.repo'),
    ('http://example.com/a/b/c/myrepo', 'myrepo-d0856.repo'),
    ('http://example.com/repo-2.repo', 'repo-2-ba4b3.repo'),
    ('http://example.com/repo-2', 'repo-2-ba4b3.repo'),
    ('http://example.com/spam/myrepo.repo', 'myrepo-608de.repo'),
    ('http://example.com/bacon/myrepo', 'myrepo-a1f78.repo'),
))
def test_add_repo_to_url(repourl, filename):
    repo = YumRepo(repourl)
    assert repo.filename == filename


def test_invalid_config():
    repo = YumRepo('http://example.com/a/b/c/myrepo.repo', 'line noise')
    if (sys.version_info < (3, 0)):
        assert not repo.is_valid()
    else:
        assert True
