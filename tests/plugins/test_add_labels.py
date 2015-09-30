"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

try:
    from collections import OrderedDict
except ImportError:
    # Python 2.6
    from ordereddict import OrderedDict
from dockerfile_parse import DockerfileParser
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.plugins.pre_add_labels_in_df import AddLabelsPlugin
from atomic_reactor.util import ImageName
from atomic_reactor.source import VcsInfo
import re
import json
import pytest
from flexmock import flexmock
from tests.constants import MOCK_SOURCE, DOCKERFILE_GIT, DOCKERFILE_SHA1, MOCK
from tests.fixtures import docker_tasker
if MOCK:
    from tests.docker_mock import mock_docker


class MockSource(object):
    dockerfile_path = None
    path = None
    def get_vcs_info(self):
        return VcsInfo(vcs_type="git", vcs_url=DOCKERFILE_GIT, vcs_ref=DOCKERFILE_SHA1)


class X(object):
    image_id = "xxx"
    source = MockSource()
    base_image = ImageName(repo="qwe", tag="asd")

DF_CONTENT = """\
FROM fedora
RUN yum install -y python-django
CMD blabla"""
DF_CONTENT_SINGLE_LINE = """\
FROM fedora"""
LABELS_CONF_BASE = {"Config": {"Labels": {"label1": "base value"}}}
LABELS_CONF = OrderedDict({'label1': 'value 1', 'label2': 'long value'})
LABELS_CONF_WRONG = [('label1', 'value1'), ('label2', 'value2')]
LABELS_BLANK = {}
# Can't be sure of the order of the labels, expect either
EXPECTED_OUTPUT = ["""FROM fedora
RUN yum install -y python-django
CMD blabla
LABEL "label1"="value 1" "label2"="long value"
""", """\
FROM fedora
RUN yum install -y python-django
CMD blabla
LABEL "label2"="long value" "label1"="value 1"
"""]
EXPECTED_OUTPUT2 = [r"""FROM fedora
RUN yum install -y python-django
CMD blabla
LABEL "label2"="long value"
"""]
EXPECTED_OUTPUT3 = [DF_CONTENT]
EXPECTED_OUTPUT4 = [r"""FROM fedora
LABEL "label2"="long value"
"""]

@pytest.mark.parametrize('df_content, labels_conf_base, labels_conf, dont_overwrite, expected_output', [
    (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF, [], EXPECTED_OUTPUT),
    (DF_CONTENT, LABELS_CONF_BASE, json.dumps(LABELS_CONF), [], EXPECTED_OUTPUT),
    (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF_WRONG, [], RuntimeError()),
    (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF, ["label1", ], EXPECTED_OUTPUT2),
    (DF_CONTENT, LABELS_CONF_BASE, LABELS_BLANK, ["label1", ], EXPECTED_OUTPUT3),
    (DF_CONTENT_SINGLE_LINE, LABELS_CONF_BASE, LABELS_CONF, ["label1", ], EXPECTED_OUTPUT4),
])
def test_add_labels_plugin(tmpdir, docker_tasker,
                           df_content, labels_conf_base, labels_conf, dont_overwrite, expected_output):
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    if MOCK:
        mock_docker()

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    flexmock(workflow, base_image_inspect=labels_conf_base)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {'labels': labels_conf, "dont_overwrite": dont_overwrite, "auto_labels": []}
        }]
    )

    if isinstance(expected_output, RuntimeError):
        with pytest.raises(RuntimeError):
            runner.run()
    else:
        runner.run()
        assert AddLabelsPlugin.key is not None
        assert df.content in expected_output

@pytest.mark.parametrize('auto_label, value_re_part', [
    ('build-date', r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z'),
    ('architecture', 'x86_64'),
    ('vcs-type', 'git'),
    ('vcs-url', DOCKERFILE_GIT),
    ('vcs-ref', DOCKERFILE_SHA1),
])
def test_add_labels_plugin_generated(tmpdir, docker_tasker, auto_label, value_re_part):
    df = DockerfileParser(str(tmpdir))
    df.content = DF_CONTENT

    if MOCK:
        mock_docker()

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    flexmock(workflow, source=MockSource())
    flexmock(workflow, base_image_inspect=LABELS_CONF_BASE)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {'labels': {}, "dont_overwrite": [], "auto_labels": [auto_label]}
        }]
    )

    runner.run()
    assert re.search('LABEL "{0}"="{1}"'.format(auto_label, value_re_part), df.content)
