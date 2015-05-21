# -*- coding: utf-8 -*-
"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import os
from dock.constants import YUM_REPOS_DIR, DEFAULT_YUM_REPOFILE_NAME, RELATIVE_REPOS_PATH

try:
    from collections import OrderedDict
except ImportError:
    # Python 2.6
    from ordereddict import OrderedDict
from dock.core import DockerTasker
from dock.inner import DockerBuildWorkflow
from dock.plugin import PreBuildPluginsRunner
from dock.plugins.pre_inject_yum_repo import InjectYumRepoPlugin, alter_yum_commands
from dock.util import ImageName, render_yum_repo, DockerfileParser
from tests.constants import DOCKERFILE_GIT
import os.path
from collections import namedtuple
import requests
from flexmock import flexmock


class X(object):
    pass


SOURCE = {'provider': 'git', 'uri': DOCKERFILE_GIT}


def test_yuminject_plugin_notwrapped(tmpdir):
    df_content = """\
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())

    metalink = 'https://mirrors.fedoraproject.org/metalink?repo=fedora-$releasever&arch=$basearch'

    workflow.files[os.path.join(YUM_REPOS_DIR, DEFAULT_YUM_REPOFILE_NAME)] = render_yum_repo(OrderedDict(
        (('name', 'my-repo'),
         ('metalink', metalink),
         ('enabled', 1),
         ('gpgcheck', 0)),
    ))

    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'df_path', df.dockerfile_path)
    setattr(workflow.builder, 'df_dir', str(tmpdir))
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='21'))
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': InjectYumRepoPlugin.key,
        'args': {
            "wrap_commands": False
        }
    }])
    runner.run()
    assert InjectYumRepoPlugin.key is not None

    expected_output = r"""FROM fedora
ADD dock-repos/* '/etc/yum.repos.d/'
RUN yum install -y python-django
RUN rm -f '/etc/yum.repos.d/dock-injected.repo'
CMD blabla"""
    assert expected_output == df.content


def test_yuminject_plugin_wrapped(tmpdir):
    df_content = """\
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(DOCKERFILE_GIT, "test-image")
    setattr(workflow, 'builder', X)

    metalink = 'https://mirrors.fedoraproject.org/metalink?repo=fedora-$releasever&arch=$basearch'

    workflow.files[os.path.join(YUM_REPOS_DIR, DEFAULT_YUM_REPOFILE_NAME)] = render_yum_repo(OrderedDict(
        (('name', 'my-repo'),
         ('metalink', metalink),
         ('enabled', '1'),
         ('gpgcheck', '0')),
    ))

    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'df_path', df.dockerfile_path)
    setattr(workflow.builder, 'df_dir', str(tmpdir))
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='21'))
    setattr(workflow.builder, 'git_dockerfile_path', None)
    setattr(workflow.builder, 'git_path', None)
    runner = PreBuildPluginsRunner(tasker, workflow, [{
        'name': InjectYumRepoPlugin.key,
        'args': {
            "wrap_commands": True
        }
    }])
    runner.run()
    assert InjectYumRepoPlugin.key is not None

    expected_output = """FROM fedora
RUN printf "[my-repo]\nname=my-repo\nmetalink=https://mirrors.fedoraproject.org/metalink?repo=fedora-\\$releasever&arch=\\$basearch\nenabled=1\ngpgcheck=0\n" >/etc/yum.repos.d/dock-injected.repo && yum install -y python-django && yum clean all && rm -f /etc/yum.repos.d/dock-injected.repo
CMD blabla"""
    assert df.content == expected_output


def test_yuminject_multiline_wrapped(tmpdir):
    df_content = """\
FROM fedora
RUN yum install -y httpd \
                   uwsgi
CMD blabla"""
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X)

    metalink = 'https://mirrors.fedoraproject.org/metalink?repo=fedora-$releasever&arch=$basearch'

    workflow.files[os.path.join(YUM_REPOS_DIR, DEFAULT_YUM_REPOFILE_NAME)] = render_yum_repo(OrderedDict(
        (('name', 'my-repo'),
        ('metalink', metalink),
        ('enabled', '1'),
        ('gpgcheck', '0')),
    ))
    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'df_path', df.dockerfile_path)
    setattr(workflow.builder, 'df_dir', str(tmpdir))
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='21'))
    setattr(workflow.builder, 'source', X())
    setattr(workflow.builder.source, 'dockerfile_path', None)
    setattr(workflow.builder.source, 'path', None)
    runner = PreBuildPluginsRunner(tasker, workflow,
                                   [{'name': InjectYumRepoPlugin.key, 'args': {
                                       "wrap_commands": True
                                   }}])
    runner.run()
    assert InjectYumRepoPlugin.key is not None

    expected_output = """FROM fedora
RUN printf "[my-repo]\nname=my-repo\nmetalink=https://mirrors.fedoraproject.org/metalink?repo=fedora-\\$releasever&arch=\\$basearch\nenabled=1\ngpgcheck=0\n" >/etc/yum.repos.d/dock-injected.repo && yum install -y httpd                    uwsgi && yum clean all && rm -f /etc/yum.repos.d/dock-injected.repo
CMD blabla"""
    assert df.content == expected_output


def test_yuminject_multiline_notwrapped(tmpdir):
    df_content = """\
FROM fedora
RUN yum install -y httpd \
                   uwsgi
CMD blabla"""
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(DOCKERFILE_GIT, "test-image")
    setattr(workflow, 'builder', X)

    metalink = r'https://mirrors.fedoraproject.org/metalink?repo=fedora-$releasever&arch=$basearch'

    workflow.files[os.path.join(YUM_REPOS_DIR, DEFAULT_YUM_REPOFILE_NAME)] = render_yum_repo(OrderedDict(
        (('name', 'my-repo'),
         ('metalink', metalink),
         ('enabled', "1"),
         ('gpgcheck', "0")),
    ))
    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'df_path', df.dockerfile_path)
    setattr(workflow.builder, 'df_dir', str(tmpdir))
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='21'))
    setattr(workflow.builder, 'git_dockerfile_path', None)
    setattr(workflow.builder, 'git_path', None)
    runner = PreBuildPluginsRunner(tasker, workflow,
                                   [{'name': InjectYumRepoPlugin.key, 'args': {
                                       "wrap_commands": False
                                   }}])
    runner.run()
    assert InjectYumRepoPlugin.key is not None

    expected_output = r"""FROM fedora
ADD dock-repos/* '/etc/yum.repos.d/'
RUN yum install -y httpd                    uwsgi
RUN rm -f '/etc/yum.repos.d/dock-injected.repo'
CMD blabla"""
    assert df.content == expected_output


def test_yuminject_multiline_wrapped_with_chown(tmpdir):
    df_content = """\
FROM fedora
RUN yum install -y --setopt=tsflags=nodocs bind-utils gettext iproute v8314 mongodb24-mongodb mongodb24 && \
    yum clean all && \
    mkdir -p /var/lib/mongodb/data && chown -R mongodb:mongodb /var/lib/mongodb/ && \
    test "$(id mongodb)" = "uid=184(mongodb) gid=998(mongodb) groups=998(mongodb)" && \
    chmod o+w -R /var/lib/mongodb && chmod o+w -R /opt/rh/mongodb24/root/var/lib/mongodb
CMD blabla"""
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(DOCKERFILE_GIT, "test-image")
    setattr(workflow, 'builder', X)

    metalink = r'https://mirrors.fedoraproject.org/metalink?repo=fedora-$releasever&arch=$basearch'

    workflow.files[os.path.join(YUM_REPOS_DIR, DEFAULT_YUM_REPOFILE_NAME)] = render_yum_repo(OrderedDict(
        (('name', 'my-repo'),
         ('metalink', metalink),
         ('enabled', 1),
         ('gpgcheck', 0)),
    ))
    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'df_path', df.dockerfile_path)
    setattr(workflow.builder, 'df_dir', str(tmpdir))
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='21'))
    setattr(workflow.builder, 'git_dockerfile_path', None)
    setattr(workflow.builder, 'git_path', None)
    runner = PreBuildPluginsRunner(tasker, workflow,
                                   [{'name': InjectYumRepoPlugin.key, 'args': {
                                       "wrap_commands": True
                                   }}])
    runner.run()
    assert InjectYumRepoPlugin.key is not None

    expected_output = """FROM fedora
RUN printf "[my-repo]\nname=my-repo\nmetalink=https://mirrors.fedoraproject.org/metalink?repo=fedora-\\$releasever&arch=\
\\$basearch\nenabled=1\ngpgcheck=0\n" >/etc/yum.repos.d/dock-injected.repo && \
yum install -y --setopt=tsflags=nodocs bind-utils gettext iproute v8314 mongodb24-mongodb mongodb24 &&     \
yum clean all &&     mkdir -p /var/lib/mongodb/data && chown -R mongodb:mongodb /var/lib/mongodb/ &&     \
test "$(id mongodb)" = "uid=184(mongodb) gid=998(mongodb) groups=998(mongodb)" &&     \
chmod o+w -R /var/lib/mongodb && chmod o+w -R /opt/rh/mongodb24/root/var/lib/mongodb && \
yum clean all && rm -f /etc/yum.repos.d/dock-injected.repo
CMD blabla"""
    assert df.content == expected_output


def test_complex_df():
    df = """\
FROM fedora
RUN asd
RUN  yum install x
ENV x=y
RUN yum install \
    x \
    y \
    && something else
CMD asd"""
    wrap_cmd = "RUN test && %(yum_command)s && asd"
    out = alter_yum_commands(df, wrap_cmd)
    expected_output = """\
FROM fedora
RUN asd
RUN test && yum install x && asd
ENV x=y
RUN test && yum install     x     y     && something else && asd
CMD asd"""
    assert out == expected_output


repocontent = '''\
[repo]
name=asd
'''
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
    "user":
    Dockerfile(["FROM base\n",
                "MAINTAINER Example <example@example.com\n"],
               # add goes here
               ["RUN yum update -y\n",
                "ENV asd qwe\n"],
               # remove goes here
               ["USER foo\n",
                "RUN uname\n",
                "LABEL x y\n",
                "CMD ['/bin/ls']\n"]),
}


def prepare(df_path):
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(DOCKERFILE_GIT, "test-image")
    setattr(workflow, 'builder', X)

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
    for df_content in DOCKERFILES.values():
        df = DockerfileParser(str(tmpdir))
        df.lines = df_content.lines_before_add + \
                   df_content.lines_before_remove + \
                   df_content.lines_after_remove

        tasker, workflow = prepare(df.dockerfile_path)
        runner = PreBuildPluginsRunner(tasker, workflow, [{
            'name': InjectYumRepoPlugin.key,
        }])
        runner.run()
        assert InjectYumRepoPlugin.key is not None

        assert df.lines == df_content.lines_before_add + \
                           df_content.lines_before_remove + \
                           df_content.lines_after_remove

def test_single_repourl(tmpdir):
    for df_content in DOCKERFILES.values():
        df = DockerfileParser(str(tmpdir))
        df.lines = df_content.lines_before_add + \
                   df_content.lines_before_remove + \
                   df_content.lines_after_remove
        tasker, workflow = prepare(df.dockerfile_path)
        filename = 'test.repo'
        repo_path = os.path.join(YUM_REPOS_DIR, filename)
        workflow.files[repo_path] = repocontent
        runner = PreBuildPluginsRunner(tasker, workflow, [{
                'name': InjectYumRepoPlugin.key,
                'args': {'wrap_commands': False}}])
        runner.run()

        # Was it written correctly?
        repos_dir = os.path.join(str(tmpdir), RELATIVE_REPOS_PATH)
        repofile = os.path.join(repos_dir, filename)
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

        # There should be a final 'rm'
        remove = newdf[before_remove]
        assert remove == "RUN rm -f '/etc/yum.repos.d/%s'\n" % filename

        # Lines after that should be unchanged.
        after_remove = before_remove + 1
        assert newdf[after_remove:] == df_content.lines_after_remove


def test_multiple_repourls(tmpdir):
    for df_content in DOCKERFILES.values():
        df = DockerfileParser(str(tmpdir))
        df.lines = df_content.lines_before_add + \
                   df_content.lines_before_remove + \
                   df_content.lines_after_remove
        tasker, workflow = prepare(df.dockerfile_path)
        filename1 = 'myrepo.repo'
        filename2 = 'repo-2.repo'
        repo_path1 = os.path.join(YUM_REPOS_DIR, filename1)
        repo_path2 = os.path.join(YUM_REPOS_DIR, filename2)
        workflow.files[repo_path1] = repocontent
        workflow.files[repo_path2] = repocontent
        runner = PreBuildPluginsRunner(tasker, workflow, [{
                'name': InjectYumRepoPlugin.key,
                'args': {'wrap_commands': False}}])
        runner.run()

        # Remove the repos/ directory.
        repos_dir = os.path.join(str(tmpdir), RELATIVE_REPOS_PATH)
        for repofile in [filename1, filename2]:
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
        remove = newdf[before_remove]
        rmline = "RUN rm -f '/etc/yum.repos.d/%s' '/etc/yum.repos.d/%s'\n"
        assert remove in [rmline % (filename1, filename2),
                          rmline % (filename2, filename1)]

        # Lines after that should be unchanged.
        after_remove = before_remove + 1
        assert newdf[after_remove:] == df_content.lines_after_remove
