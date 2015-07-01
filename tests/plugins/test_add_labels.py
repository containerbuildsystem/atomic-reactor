"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import os
try:
    from collections import OrderedDict
except ImportError:
    # Python 2.6
    from ordereddict import OrderedDict
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.plugins.pre_add_labels_in_df import AddLabelsPlugin
from atomic_reactor.util import ImageName, DockerfileParser
from tests.constants import MOCK_SOURCE
import json
import pytest


class Y(object):
    pass


class X(object):
    image_id = "xxx"
    source = Y()
    source.dockerfile_path = None
    source.path = None
    base_image = ImageName(repo="qwe", tag="asd")


def test_addlabels_plugin(tmpdir):
    df_content = """\
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    labels_conf = OrderedDict({'label1': 'value 1', 'label2': 'long value'})

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {'labels': labels_conf}
        }]
    )
    runner.run()
    assert AddLabelsPlugin.key is not None

    # Can't be sure of the order of the labels, expect either
    expected_output = [r"""FROM fedora
RUN yum install -y python-django
LABEL "label1"="value 1" "label2"="long value"
CMD blabla""",
                       r"""FROM fedora
RUN yum install -y python-django
LABEL "label2"="long value" "label1"="value 1"
CMD blabla"""]
    assert df.content in expected_output

def test_addlabels_string_args(tmpdir):
    df_content = """\
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    labels_conf = OrderedDict({'label1': 'value 1', 'label2': 'long value'})

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {'labels': json.dumps(labels_conf)}
        }]
    )
    # Should not raise exception even though we pass a string instead
    # of a dict, because it can be decoded as JSON.
    runner.run()

def test_addlabels_bad_args(tmpdir):
    df_content = """\
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    labels_conf = [('label1', 'value1'), ('label2', 'value2')]

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {'labels': labels_conf}
        }]
    )
    # Should fail: labels_conf is not a dict
    with pytest.raises(Exception) as excinfo:
        runner.run()
