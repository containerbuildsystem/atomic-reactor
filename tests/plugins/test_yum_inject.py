"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function

import os
try:
    from collections import OrderedDict
except ImportError:
    # Python 2.6
    from ordereddict import OrderedDict
from dock.core import DockerTasker
from dock.inner import DockerBuildWorkflow
from dock.plugin import PreBuildPluginsRunner, PostBuildPluginsRunner
from dock.plugins.pre_inject_yum_repo import InjectYumRepoPlugin, alter_yum_commands
from dock.util import ImageName


git_url = "https://github.com/TomasTomecek/docker-hello-world.git"
TEST_IMAGE = "fedora:latest"


class X(object):
    pass


def test_yuminject_plugin(tmpdir):
    df = """\
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    tmp_df = os.path.join(str(tmpdir), 'Dockerfile')
    with open(tmp_df, mode="w") as fd:
        fd.write(df)

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(git_url, "test-image")
    setattr(workflow, 'builder', X)

    metalink = 'https://mirrors.fedoraproject.org/metalink?repo=fedora-$releasever&arch=$basearch'

    workflow.repos['yum'] = [OrderedDict(
        (('name', 'my-repo'),
        ('metalink', metalink),
        ('enabled', 1),
        ('gpgcheck', 0)),
    )]

    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'df_path', tmp_df)
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='21'))
    setattr(workflow.builder, 'git_dockerfile_path', None)
    setattr(workflow.builder, 'git_path', None)
    runner = PreBuildPluginsRunner(tasker, workflow, [{
                                       'name': InjectYumRepoPlugin.key,
                                       'args': {}}])
    runner.run()
    assert InjectYumRepoPlugin.key is not None
    with open(tmp_df, 'r') as fd:
        altered_df = fd.read()
    expected_output = r"""FROM fedora
RUN printf "[my-repo]\nname=my-repo\nmetalink=https://mirrors.fedoraproject.org/metalink?repo=fedora-\$releasever&arch=\$basearch\nenabled=1\ngpgcheck=0\n" >/etc/yum.repos.d/dock-injected.repo && yum install -y python-django && yum clean all && rm -f /etc/yum.repos.d/dock-injected.repo
CMD blabla"""
    assert expected_output == altered_df


def test_yuminject_multiline(tmpdir):
    df = """\
FROM fedora
RUN yum install -y httpd \
                   uwsgi
CMD blabla"""
    tmp_df = os.path.join(str(tmpdir), 'Dockerfile')
    with open(tmp_df, mode="w") as fd:
        fd.write(df)

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(git_url, "test-image")
    setattr(workflow, 'builder', X)

    metalink = r'https://mirrors.fedoraproject.org/metalink?repo=fedora-$releasever&arch=$basearch'

    workflow.repos['yum'] = [OrderedDict(
        (('name', 'my-repo'),
        ('metalink', metalink),
        ('enabled', 1),
        ('gpgcheck', 0)),
    )]
    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'df_path', tmp_df)
    setattr(workflow.builder, 'base_image', ImageName(repo='Fedora', tag='21'))
    setattr(workflow.builder, 'git_dockerfile_path', None)
    setattr(workflow.builder, 'git_path', None)
    runner = PreBuildPluginsRunner(tasker, workflow,
                                   [{'name': InjectYumRepoPlugin.key, 'args': {}}])
    runner.run()
    assert InjectYumRepoPlugin.key is not None
    with open(tmp_df, 'r') as fd:
        altered_df = fd.read()
    expected_output = r"""FROM fedora
RUN printf "[my-repo]\nname=my-repo\nmetalink=https://mirrors.fedoraproject.org/metalink?repo=fedora-\$releasever&arch=\$basearch\nenabled=1\ngpgcheck=0\n" >/etc/yum.repos.d/dock-injected.repo && yum install -y httpd                    uwsgi && yum clean all && rm -f /etc/yum.repos.d/dock-injected.repo
CMD blabla"""
    assert altered_df == expected_output


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
