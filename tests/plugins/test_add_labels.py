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
from dock.core import DockerTasker
from dock.inner import DockerBuildWorkflow
from dock.plugin import PreBuildPluginsRunner
from dock.plugins.pre_add_labels_in_df import AddLabelsPlugin
from dock.util import ImageName, DockerfileParser


class X(object):
    image_id = "xxx"
    git_dockerfile_path = None
    git_path = None
    base_image = ImageName(repo="qwe", tag="asd")


def test_addlabels_plugin(tmpdir):
    df_content = """\
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow("asd", "test-image")
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
