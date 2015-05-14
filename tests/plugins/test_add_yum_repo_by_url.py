"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

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
Dockerfile = namedtuple('Dockerfile', ['lines_before_add',
                                       'lines_before_remove',
                                       'lines_after_remove'])


DOCKERFILES = {
    "no maintainer":
    Dockerfile(["# Simple example with no MAINTAINER line\n",
                "FROM base\n"],
               # add goes here
               [" RUN yum -y update\n"],
               # remove goes here
               []),

    "no yum":
    Dockerfile(["FROM base\n",
                "# This time there is a MAINTAINER line\n",
                "# but it's the last last there is\n",
                "MAINTAINER Example <example@example.com>\n"],
               # add goes here
               [],
               # remove goes here
               []),

    "cmd":
    Dockerfile([" From base\n",
                "LABEL 'a'='b'\n",
                "MAINTAINER Example <example@example.com>\n"],
               # add goes here
               ["RUN some command\n",
                "RUN some other command\n",
                "VOLUME ['/data']\n",
                "# rm line expected on following line\n"],
               # remove goes here
               ["CMD ['/bin/bash']\n"]),

    "entrypoint":
    Dockerfile(["FROM base\n",
                "MAINTAINER Example <example@example.com\n"],
               # add goes here
               ["RUN yum update -y\n",
                "RUN yum install -y example\n"],
               # remove goes here
               ["ENTRYPOINT ['/bin/bash']\n",
                "CMD ['/bin/ls']\n"]),
}


class X(object):
    pass


def prepare(df_path):
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(DOCKERFILE_GIT, "test-image")
    setattr(workflow, 'builder', X)

    workflow.repos['yum'] = []

    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'df_path', str(df_path))
    setattr(workflow.builder, 'df_dir', os.path.dirname(str(df_path)))
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='21'))
    setattr(workflow.builder, 'git_dockerfile_path', None)
    setattr(workflow.builder, 'git_path', None)
    (flexmock(requests.Response, content=repocontent)
        .should_receive('raise_for_status')
        .and_return(None))
    (flexmock(requests, get=lambda *_: requests.Response()))
    return tasker, workflow


def test_no_repourls(tmpdir):
    for df in DOCKERFILES.values():
        with NamedTemporaryFile(mode="w+t",
                                prefix="Dockerfile",
                                dir=str(tmpdir)) as f:
            f.writelines(df.lines_before_add +
                         df.lines_before_remove +
                         df.lines_after_remove)
            f.flush()
            tasker, workflow = prepare(f.name)
            runner = PreBuildPluginsRunner(tasker, workflow, [{
                'name': AddYumRepoByUrlPlugin.key,
                'args': {'repourls': []}}])
            runner.run()
            assert AddYumRepoByUrlPlugin.key is not None

            f.seek(0)
            # Should be unchanged
            assert f.readlines() == (df.lines_before_add +
                                     df.lines_before_remove +
                                     df.lines_after_remove)


def test_single_repourl(tmpdir):
    for df in DOCKERFILES.values():
        with NamedTemporaryFile(mode="w+t",
                                prefix="Dockerfile",
                                dir=str(tmpdir)) as f:
            f.writelines(df.lines_before_add +
                         df.lines_before_remove +
                         df.lines_after_remove)
            f.flush()
            tasker, workflow = prepare(f.name)
            url = 'http://example.com/example%20repo.repo'
            filename = 'example repo.repo'
            runner = PreBuildPluginsRunner(tasker, workflow, [{
                'name': AddYumRepoByUrlPlugin.key,
                'args': {'repourls': [url]}}])
            runner.run()

            # Should have downloaded the repo file content.
            repos_dir = os.path.join(str(tmpdir), "repos")
            repofile = os.path.join(repos_dir, filename)
            with open(repofile, "rb") as fp:
                assert fp.read() == repocontent

            # Remove the repos/ directory.
            os.remove(repofile)
            os.rmdir(repos_dir)

            # Examine the Dockerfile.

            f.seek(0)
            newdf = f.readlines()
            before_add = len(df.lines_before_add)
            before_remove = len(df.lines_before_remove)

            # Start of file should be unchanged.
            assert newdf[:before_add] == df.lines_before_add

            # Should see a single add line.
            after_add = before_add + 1
            assert (newdf[before_add:after_add] ==
                    ["ADD 'repos'/* '/etc/yum.repos.d/'\n"])

            # Lines from there up to the remove line should be unchanged.
            before_remove = after_add + len(df.lines_before_remove)
            assert (newdf[after_add:before_remove] ==
                    df.lines_before_remove)

            # There should be a final 'rm'
            remove = newdf[before_remove]
            assert remove == "RUN rm -f '/etc/yum.repos.d/%s'\n" % filename

            # Lines after that should be unchanged.
            after_remove = before_remove + 1
            assert newdf[after_remove:] == df.lines_after_remove


def test_multiple_repourls(tmpdir):
    for df in DOCKERFILES.values():
        with NamedTemporaryFile(mode="w+t",
                                prefix="Dockerfile",
                                dir=str(tmpdir)) as f:
            f.writelines(df.lines_before_add +
                         df.lines_before_remove +
                         df.lines_after_remove)
            f.flush()
            tasker, workflow = prepare(f.name)
            url1 = 'http://example.com/a/b/c/myrepo.repo'
            filename1 = 'myrepo.repo'
            url2 = 'http://example.com/repo-2.repo'
            filename2 = 'repo-2.repo'
            runner = PreBuildPluginsRunner(tasker, workflow, [{
                'name': AddYumRepoByUrlPlugin.key,
                'args': {'repourls': [url1, url2]}}])
            runner.run()

            # Remove the repos/ directory.
            repos_dir = os.path.join(str(tmpdir), "repos")
            for repofile in [filename1, filename2]:
                os.remove(os.path.join(repos_dir, repofile))

            os.rmdir(repos_dir)

            # Examine the Dockerfile.
            f.seek(0)
            newdf = f.readlines()
            before_add = len(df.lines_before_add)
            before_remove = len(df.lines_before_remove)

            # Start of file should be unchanged.
            assert newdf[:before_add] == df.lines_before_add

            # Should see a single add line.
            after_add = before_add + 1
            assert (newdf[before_add:after_add] ==
                    ["ADD 'repos'/* '/etc/yum.repos.d/'\n"])

            # Lines from there up to the remove line should be unchanged.
            before_remove = after_add + len(df.lines_before_remove)
            assert (newdf[after_add:before_remove] ==
                    df.lines_before_remove)

            # For the 'rm' line, they could be in either order
            remove = newdf[before_remove]
            rmline = "RUN rm -f '/etc/yum.repos.d/%s' '/etc/yum.repos.d/%s'\n"
            assert remove in [rmline % (filename1, filename2),
                              rmline % (filename2, filename1)]

            # Lines after that should be unchanged.
            after_remove = before_remove + 1
            assert newdf[after_remove:] == df.lines_after_remove
