"""
Copyright (c) 2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Very small subset of tests for the YumRepo class. Most testing
is done in test_add_yum_repo_by_url
"""
from fnmatch import fnmatch
import os
import sys
from atomic_reactor.utils.yum import YumRepo
import pytest


@pytest.mark.parametrize(('repourl', 'add_hash', 'pattern'), (
    ('http://example.com/a/b/c/myrepo.repo', True, 'myrepo-?????.repo'),
    ('http://example.com/a/b/c/myrepo', True, 'myrepo-?????.repo'),
    ('http://example.com/repo-2.repo', True, 'repo-2-?????.repo'),
    ('http://example.com/repo-2', True, 'repo-2-?????.repo'),
    ('http://example.com/spam/myrepo.repo', True, 'myrepo-?????.repo'),
    ('http://example.com/bacon/myrepo', True, 'myrepo-?????.repo'),
    ('http://example.com/spam/myrepo-608de.repo', False, 'myrepo-?????.repo'),
))
def test_add_repo_to_url(repourl, add_hash, pattern):
    repo = YumRepo(repourl, add_hash=add_hash)
    assert repo.repourl == repourl
    assert fnmatch(repo.filename, pattern)


def test_invalid_config():
    repo = YumRepo('http://example.com/a/b/c/myrepo.repo', 'line noise')
    if (sys.version_info < (3, 0)):
        assert not repo.is_valid()
    else:
        assert True


def test_write_content(tmpdir):
    test_content = 'test_content'
    repo = YumRepo(
        repourl='http://example.com/a/b/c/myrepo.repo', content=test_content,
        dst_repos_dir=str(tmpdir)
    )
    repo.write_content()

    with open(os.path.join(str(tmpdir), repo.filename)) as f:
        assert f.read() == test_content
