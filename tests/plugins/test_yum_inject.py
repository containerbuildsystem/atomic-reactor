# -*- coding: utf-8 -*-
"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals, absolute_import

import os
import re
import itertools
from textwrap import dedent
from collections import OrderedDict
import pytest
from atomic_reactor.constants import (YUM_REPOS_DIR, DEFAULT_YUM_REPOFILE_NAME, RELATIVE_REPOS_PATH,
                                      INSPECT_CONFIG)
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.plugins.pre_inject_yum_repo import (
    InjectYumRepoPlugin,
    add_yum_repos_to_dockerfile
)
from atomic_reactor.util import render_yum_repo, df_parser
import os.path
from collections import namedtuple
import requests
from flexmock import flexmock
from tests.constants import SOURCE, MOCK
from tests.util import requires_internet
from tests.stubs import StubInsideBuilder, StubSource
if MOCK:
    from tests.docker_mock import mock_docker


def prepare(df_path, inherited_user=''):
    if MOCK:
        mock_docker()
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow("test-image", source=SOURCE)
    workflow.source = StubSource()
    workflow.builder = (StubInsideBuilder()
                        .for_workflow(workflow)
                        .set_df_path(df_path)
                        .set_inspection_data({
                            INSPECT_CONFIG: {
                                'User': inherited_user,
                            },
                        }))

    (flexmock(requests.Response, content=repocontent)
     .should_receive('raise_for_status')
     .and_return(None))
    (flexmock(requests.Session, get=lambda *_: requests.Response()))
    return tasker, workflow


@requires_internet
def test_yuminject_plugin(tmpdir):
    df_content = """\
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    df = df_parser(str(tmpdir))
    df.content = df_content

    tasker, workflow = prepare(df.dockerfile_path)

    metalink = 'https://mirrors.fedoraproject.org/metalink?repo=fedora-$releasever&arch=$basearch'

    workflow.files[os.path.join(YUM_REPOS_DIR, DEFAULT_YUM_REPOFILE_NAME)] = \
        render_yum_repo(OrderedDict((('name', 'my-repo'),
                                    ('metalink', metalink),
                                    ('enabled', 1),
                                    ('gpgcheck', 0)), ))

    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': InjectYumRepoPlugin.key,
        'args': {}
    }])
    runner.run()
    assert InjectYumRepoPlugin.key is not None

    expected_output = r"""FROM fedora
ADD atomic-reactor-repos/* '/etc/yum.repos.d/'
RUN yum install -y python-django
CMD blabla
RUN rm -f '/etc/yum.repos.d/atomic-reactor-injected.repo'
"""
    assert expected_output == df.content


def test_yuminject_multiline(tmpdir):
    df_content = """\
FROM fedora
RUN yum install -y httpd \
                   uwsgi
CMD blabla"""
    df = df_parser(str(tmpdir))
    df.content = df_content

    tasker, workflow = prepare(df.dockerfile_path)

    metalink = r'https://mirrors.fedoraproject.org/metalink?repo=fedora-$releasever&arch=$basearch'  # noqa

    workflow.files[os.path.join(YUM_REPOS_DIR, DEFAULT_YUM_REPOFILE_NAME)] = \
        render_yum_repo(OrderedDict((('name', 'my-repo'),
                                    ('metalink', metalink),
                                    ('enabled', 1),
                                    ('gpgcheck', 0)), ))
    runner = PreBuildPluginsRunner(tasker, workflow,
                                   [{'name': InjectYumRepoPlugin.key, 'args': {}}])
    runner.run()
    assert InjectYumRepoPlugin.key is not None

    expected_output = r"""FROM fedora
ADD atomic-reactor-repos/* '/etc/yum.repos.d/'
RUN yum install -y httpd                    uwsgi
CMD blabla
RUN rm -f '/etc/yum.repos.d/atomic-reactor-injected.repo'
"""
    assert df.content == expected_output


repocontent = '''\
[repo]
name=asd
'''
Dockerfile = namedtuple('Dockerfile', ['inherited_user',
                                       'lines_before_add',
                                       'lines_before_remove',
                                       'remove_lines'])


DOCKERFILES = {
    "simple":
        Dockerfile('',
                   ["# Simple example\n",
                    "FROM base\n"],
                   # add goes here
                   [" RUN yum -y update\n"],
                   ["RUN rm ...\n"]),

    "no yum":
        Dockerfile('',
                   ["FROM base\n"],
                   # add goes here
                   ["MAINTAINER Example <example@example.com>\n"],
                   ["RUN rm ...\n"]),

    "user":
        Dockerfile('',
                   [" From base\n"],
                   # add goes here
                   [" Run yum update -y\n",
                    " Env asd qwe\n",
                    " User foo\n",
                    " Run uname\n",
                    " Label x y\n",
                    " Cmd ['/bin/ls']\n"],
                   ["USER root\n",
                    "RUN rm ...\n",
                    " User foo\n"]),

    "root":
        Dockerfile('',
                   ["FROM nonroot-base\n"],
                   # add goes here
                   ["USER root\n",
                    "RUN yum -y update\n",
                    "USER user\n",
                    "CMD ['id']\n"],
                   ["USER root\n",
                    "RUN rm ...\n",
                    "USER user\n"]),

    "inherit":
        Dockerfile('inherited',
                   ["FROM inherit-user\n"],
                   # add goes here
                   ["RUN /bin/ls\n"],
                   ["USER root\n",
                    "RUN rm ...\n",
                    "USER inherited\n"]),

    "inherit-root":
        Dockerfile('inherited',
                   ["FROM inherit-root\n"],
                   # add goes here
                   ["USER root\n",
                    "RUN yum -y update\n",
                    "USER user\n"],
                   ["USER root\n",
                    "RUN rm ...\n",
                    "USER user\n"]),
}


def test_no_repourls(tmpdir):
    for df_content in DOCKERFILES.values():
        df = df_parser(str(tmpdir))
        df.lines = df_content.lines_before_add + df_content.lines_before_remove

        tasker, workflow = prepare(df.dockerfile_path,
                                   df_content.inherited_user)
        runner = PreBuildPluginsRunner(tasker, workflow, [{
            'name': InjectYumRepoPlugin.key,
        }])
        runner.run()
        assert InjectYumRepoPlugin.key is not None

        # Should be unchanged.
        assert df.lines == df_content.lines_before_add + df_content.lines_before_remove


def remove_lines_match(actual, expected, repos):
    if len(actual) != len(expected):
        return False

    for aline, eline in zip(actual, expected):
        if eline.startswith("RUN rm"):
            if not aline.startswith("RUN rm -f "):
                assert aline == eline

            assert set(aline.rstrip()[10:].split(' ')) == \
                set(["'/etc/yum.repos.d/%s'" % repo for repo in repos])
        else:
            assert aline == eline

    return True


def test_single_repourl(tmpdir):
    for df_content in DOCKERFILES.values():
        df = df_parser(str(tmpdir))
        df.lines = df_content.lines_before_add + df_content.lines_before_remove
        tasker, workflow = prepare(df.dockerfile_path,
                                   df_content.inherited_user)
        filename = 'test-ccece.repo'
        unique_filename = 'test-ccece.repo'
        repo_path = os.path.join(YUM_REPOS_DIR, filename)
        workflow.files[repo_path] = repocontent
        runner = PreBuildPluginsRunner(tasker, workflow, [{
                'name': InjectYumRepoPlugin.key,
                'args': {}}])
        runner.run()

        # Was it written correctly?
        repos_dir = os.path.join(str(tmpdir), RELATIVE_REPOS_PATH)
        repofile = os.path.join(repos_dir, unique_filename)
        with open(repofile, "r") as fp:
            assert fp.read() == repocontent

        # Remove the repos/ directory.
        os.remove(repofile)
        os.rmdir(repos_dir)

        # Examine the Dockerfile.
        newdf = df.lines
        before_add = len(df_content.lines_before_add)
        before_remove = len(df_content.lines_before_remove)

        # Start of file should be unchanged.
        assert newdf[:before_add] == df_content.lines_before_add

        # Should see a single add line.
        after_add = before_add + 1
        assert (newdf[before_add:after_add] ==
                ["ADD %s* '/etc/yum.repos.d/'\n" % RELATIVE_REPOS_PATH])

        # Lines from there up to the remove line should be unchanged.
        before_remove = after_add + len(df_content.lines_before_remove)
        assert (newdf[after_add:before_remove] ==
                df_content.lines_before_remove)

        # The 'rm' lines should match
        # There should be a final 'rm'
        remove = newdf[before_remove:]
        assert remove_lines_match(remove, df_content.remove_lines, [filename])


def test_multiple_repourls(tmpdir):
    for df_content in DOCKERFILES.values():
        df = df_parser(str(tmpdir))
        df.lines = df_content.lines_before_add + df_content.lines_before_remove
        tasker, workflow = prepare(df.dockerfile_path,
                                   df_content.inherited_user)
        filename1 = 'myrepo-457b5.repo'
        filename2 = 'repo-2-7c47d.repo'
        unique_filename1 = 'myrepo-457b5.repo'
        unique_filename2 = 'repo-2-7c47d.repo'
        repo_path1 = os.path.join(YUM_REPOS_DIR, filename1)
        repo_path2 = os.path.join(YUM_REPOS_DIR, filename2)
        workflow.files[repo_path1] = repocontent
        workflow.files[repo_path2] = repocontent
        runner = PreBuildPluginsRunner(tasker, workflow, [{
                'name': InjectYumRepoPlugin.key,
                'args': {}}])
        runner.run()

        # Remove the repos/ directory.
        repos_dir = os.path.join(str(tmpdir), RELATIVE_REPOS_PATH)
        for repofile in [unique_filename1, unique_filename2]:
            os.remove(os.path.join(repos_dir, repofile))

        os.rmdir(repos_dir)

        # Examine the Dockerfile.
        newdf = df.lines
        before_add = len(df_content.lines_before_add)
        before_remove = len(df_content.lines_before_remove)

        # Start of file should be unchanged.
        assert newdf[:before_add] == df_content.lines_before_add

        # Should see a single add line.
        after_add = before_add + 1
        assert (newdf[before_add:after_add] ==
                ["ADD %s* '/etc/yum.repos.d/'\n" % RELATIVE_REPOS_PATH])

        # Lines from there up to the remove line should be unchanged.
        before_remove = after_add + len(df_content.lines_before_remove)
        assert (newdf[after_add:before_remove] ==
                df_content.lines_before_remove)

        # For the 'rm' line, they could be in either order
        remove = newdf[before_remove:]
        assert remove_lines_match(remove, df_content.remove_lines,
                                  [filename1, filename2])


@pytest.mark.parametrize(('name, inherited_user, dockerfile, expect_cleanup_lines,'
                          'base_from_scratch'), [
    (
        'single_stage',
        '',
        dedent("""\
            FROM base
              ### ADD HERE
            RUN yum -y update
        """),
        ["RUN rm ...\n"],
        False,
    ),
    (
        'multiple_stages',
        '',
        dedent("""\
            FROM builder
              ### ADD HERE
            RUN build /some/stuff
            FROM base
              ### ADD HERE
            RUN yum -y update
            COPY --from=0 /some/stuff /bin/stuff
        """),
        ["RUN rm ...\n"],
        False,
    ),
    (
        'multistage_with_user_confusion',
        'johncleese',
        dedent("""\
            FROM golang:1.9 AS builder1
              ### ADD HERE
            USER grahamchapman
            RUN build /spam/eggs

            FROM jdk:1.8 AS builder2
              ### ADD HERE
            USER ericidle
            RUN yum -y update
            RUN build /bacon/beans

            FROM base
              ### ADD HERE
            COPY --from=builder1 /some/stuff /bin/spam
            COPY --from=builder2 /some/stuff /bin/eggs
            # users in other stages should be ignored
        """),
        dedent("""\
            USER root
            RUN rm ...
            USER johncleese
        """).splitlines(True),
        False,
    ),
    (
        'multistage_with_scratch',
        '',
        dedent("""\
            FROM golang:1.9 AS builder1
              ### ADD HERE
            USER grahamchapman
            RUN build /spam/eggs

            FROM scratch
            USER somebody
            RUN yum install rpm
            RUN build /somebody

            FROM jdk:1.8 AS builder2
              ### ADD HERE
            USER ericidle
            RUN yum -y update
            RUN build /bacon/beans

            FROM base
              ### ADD HERE
            COPY --from=builder1 /some/stuff /bin/spam
            COPY --from=builder2 /some/stuff /bin/eggs
            # users in other stages should be ignored
        """),
        dedent("""\
            RUN rm ...
        """).splitlines(True),
        False,
    ),
    (
        'multistage_with_scratch_with_user',
        'inher_user',
        dedent("""\
            FROM golang:1.9 AS builder1
              ### ADD HERE
            USER grahamchapman
            RUN build /spam/eggs

            FROM scratch
            USER somebody
            RUN yum install rpm
            RUN build /somebody

            FROM jdk:1.8 AS builder2
              ### ADD HERE
            USER ericidle
            RUN yum -y update
            RUN build /bacon/beans

            FROM base
              ### ADD HERE
            COPY --from=builder1 /some/stuff /bin/spam
            COPY --from=builder2 /some/stuff /bin/eggs
            # users in other stages should be ignored
        """),
        dedent("""\
            USER root
            RUN rm ...
            USER inher_user
        """).splitlines(True),
        False,
    ),
    (
        'multistage_with_scratch_last',
        '',
        dedent("""\
            FROM golang:1.9 AS builder1
              ### ADD HERE
            USER grahamchapman
            RUN build /spam/eggs

            FROM scratch
            USER somebody
            RUN yum install rpm
            RUN build /somebody

            FROM jdk:1.8 AS builder2
              ### ADD HERE
            USER ericidle
            RUN yum -y update
            RUN build /bacon/beans

            FROM base
              ### ADD HERE
            COPY --from=builder1 /some/stuff /bin/spam
            COPY --from=builder2 /some/stuff /bin/eggs

            FROM scratch
            USER for_scratch
            RUN yum install python
        """),
        [],
        True,
    ),
    (
        'single_scratch',
        '',
        dedent("""\
            FROM scratch
            RUN yum -y update
        """),
        [],
        True,
    ),
])
def test_multistage_dockerfiles(name, inherited_user, dockerfile, expect_cleanup_lines,
                                base_from_scratch, tmpdir, caplog):
    # expect repo ADD instructions where indicated in the content, and RUN rm at the end.
    # begin by splitting on "### ADD HERE" so we know where to expect changes.
    segments = re.split(r'^.*ADD HERE.*$\n?', dockerfile, flags=re.M)
    segment_lines = [seg.splitlines(True) for seg in segments]

    # build expected contents by manually inserting expected ADD lines between the segments
    for lines in segment_lines[:-1]:
        lines.append("ADD %s* '/etc/yum.repos.d/'\n" % RELATIVE_REPOS_PATH)
    expected_lines = list(itertools.chain.from_iterable(segment_lines))  # flatten lines

    # now run the plugin to transform the given dockerfile
    df = df_parser(str(tmpdir))
    df.content = ''.join(segments)  # dockerfile without the "### ADD HERE" lines
    tasker, workflow = prepare(df.dockerfile_path, inherited_user)
    workflow.builder.set_base_from_scratch(base_from_scratch)
    repo_file = 'myrepo.repo'
    repo_path = os.path.join(YUM_REPOS_DIR, repo_file)
    workflow.files[repo_path] = repocontent
    runner = PreBuildPluginsRunner(tasker, workflow, [{
            'name': InjectYumRepoPlugin.key,
            'args': {}}])
    runner.run()

    # assert the Dockerfile has changed as expected up to the cleanup lines
    new_df = df.lines
    assert new_df[:len(expected_lines)] == expected_lines

    # the rest of the lines should be cleanup lines
    cleanup_lines = new_df[len(expected_lines):]
    assert remove_lines_match(cleanup_lines, expect_cleanup_lines, [repo_file])
    assert "injected yum repo: /etc/yum.repos.d/myrepo.repo" in caplog.text


def test_empty_dockerfile(tmpdir):
    df = df_parser(str(tmpdir))
    df.content = ''
    with pytest.raises(RuntimeError) as exc:
        add_yum_repos_to_dockerfile([], df, '', False)
    assert "No FROM" in str(exc.value)
