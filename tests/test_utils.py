"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals

import os
import six

try:
    from collections import OrderedDict
except ImportError:
    # Python 2.6
    from ordereddict import OrderedDict
import docker
from dock.util import ImageName, DockerfileParser, \
    wait_for_command, clone_git_repo, LazyGit, figure_out_dockerfile, render_yum_repo
from tests.constants import NON_ASCII, DOCKERFILE_SHA1, DOCKERFILE_GIT, INPUT_IMAGE, MOCK

if MOCK:
    from tests.docker_mock import mock_docker

TEST_DATA = {
    "repository.com/image-name": ImageName(registry="repository.com", repo="image-name"),
    "repository.com/prefix/image-name:1": ImageName(registry="repository.com",
                                                    namespace="prefix",
                                                    repo="image-name", tag="1"),
    "repository.com/prefix/image-name": ImageName(registry="repository.com",
                                                  namespace="prefix",
                                                  repo="image-name"),
    "image-name": ImageName(repo="image-name"),
    "registry:5000/image-name:latest": ImageName(registry="registry:5000",
                                                 repo="image-name", tag="latest"),
    "registry:5000/image-name": ImageName(registry="registry:5000", repo="image-name"),
    "fedora:20": ImageName(repo="fedora", tag="20"),
    "prefix/image-name:1": ImageName(namespace="prefix", repo="image-name", tag="1"),
    }

def test_image_name_parse():
    for inp, parsed in TEST_DATA.items():
        assert ImageName.parse(inp) == parsed

def test_image_name_format():
    for expected, image_name in TEST_DATA.items():
        assert image_name.to_str() == expected


def test_wait_for_command():
    if MOCK:
        mock_docker()

    d = docker.Client()
    logs_gen = d.pull(INPUT_IMAGE, stream=True)
    assert wait_for_command(logs_gen) is not None


def test_clone_git_repo(tmpdir):
    tmpdir_path = str(tmpdir.realpath())
    commit_id = clone_git_repo(DOCKERFILE_GIT, tmpdir_path)
    assert commit_id is not None
    assert len(commit_id) == 40  # current git hashes are this long
    assert os.path.isdir(os.path.join(tmpdir_path, '.git'))


def test_clone_git_repo_by_sha1(tmpdir):
    tmpdir_path = str(tmpdir.realpath())
    commit_id = clone_git_repo(DOCKERFILE_GIT, tmpdir_path, commit=DOCKERFILE_SHA1)
    assert commit_id is not None
    print(six.text_type(commit_id))
    print(commit_id)
    assert six.text_type(commit_id, encoding="ascii") == six.text_type(DOCKERFILE_SHA1)
    assert len(commit_id) == 40  # current git hashes are this long
    assert os.path.isdir(os.path.join(tmpdir_path, '.git'))


def test_dockerfileparser_non_ascii(tmpdir):
        df_content = """\
FROM fedora
CMD {0}""".format(NON_ASCII)
        df_lines = ["FROM fedora\n", "CMD {0}".format(NON_ASCII)]

        tmpdir_path = str(tmpdir.realpath())
        df = DockerfileParser(tmpdir_path)

        df.content = ""
        df.content = df_content
        assert df.content == df_content
        assert df.lines == df_lines

        df.content = ""
        df.lines = df_lines
        assert df.content == df_content
        assert df.lines == df_lines

def test_get_baseimg_from_df(tmpdir):
    tmpdir_path = str(tmpdir.realpath())
    clone_git_repo(DOCKERFILE_GIT, tmpdir_path)
    base_img = DockerfileParser(tmpdir_path).get_baseimage()
    assert base_img.startswith('fedora')


def test_get_labels_from_df(tmpdir):
    tmpdir_path = str(tmpdir.realpath())
    clone_git_repo(DOCKERFILE_GIT, tmpdir_path)
    df = DockerfileParser(tmpdir_path)
    lines = df.lines
    lines.insert(-1, 'LABEL "label1"="value 1" "label2"=myself label3="" label4\n')
    lines.insert(-1, 'LABEL label5=5\n')
    lines.insert(-1, 'LABEL "label6"=6\n')
    lines.insert(-1, 'LABEL label7\n')
    lines.insert(-1, 'LABEL "label8"\n')
    lines.insert(-1, 'LABEL "label9"="asd \  \nqwe"\n')
    lines.insert(-1, 'LABEL "label10"="{0}"\n'.format(NON_ASCII))
    df.lines = lines
    labels = df.get_labels()
    assert len(labels) == 10
    assert labels.get('label1') == 'value 1'
    assert labels.get('label2') == 'myself'
    assert labels.get('label3') == ''
    assert labels.get('label4') == ''
    assert labels.get('label5') == '5'
    assert labels.get('label6') == '6'
    assert labels.get('label7') == ''
    assert labels.get('label8') == ''
    assert labels.get('label9') == 'asd qwe'
    assert labels.get('label10') == '{0}'.format(NON_ASCII)


def test_figure_out_dockerfile(tmpdir):
    tmpdir_path = str(tmpdir.realpath())
    clone_git_repo(DOCKERFILE_GIT, tmpdir_path)
    path, dir = figure_out_dockerfile(tmpdir_path)
    assert os.path.isfile(path)
    assert os.path.isdir(dir)


def test_lazy_git():
    lazy_git = LazyGit(git_url=DOCKERFILE_GIT)
    with lazy_git:
        assert lazy_git.git_path is not None
        assert lazy_git.commit_id is not None
        assert len(lazy_git.commit_id) == 40  # current git hashes are this long


def test_lazy_git_with_tmpdir(tmpdir):
    t = str(tmpdir.realpath())
    lazy_git = LazyGit(git_url=DOCKERFILE_GIT, tmpdir=t)
    assert lazy_git._tmpdir == t
    assert lazy_git.git_path is not None
    assert lazy_git.commit_id is not None
    assert len(lazy_git.commit_id) == 40  # current git hashes are this long


def test_render_yum_repo_unicode():
    yum_repo = OrderedDict((
        ("name", "asd"),
        ("baseurl", "http://example.com/$basearch/test.repo"),
        ("enabled", "1"),
        ("gpgcheck", "0"),
    ))
    rendered_repo = render_yum_repo(yum_repo)
    assert rendered_repo == """\
[asd]
name=asd
baseurl=http://example.com/\$basearch/test.repo
enabled=1
gpgcheck=0
"""
