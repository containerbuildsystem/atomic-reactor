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
DF_CONTENT_LABEL = '''\
FROM fedora
LABEL "label2"="df value"'''
LABELS_CONF_BASE = {"Config": {"Labels": {"label1": "base value"}}}
LABELS_CONF_BASE_NONE = {"Config": {"Labels": None}}
LABELS_CONF = OrderedDict({'label1': 'value 1', 'label2': 'long value'})
LABELS_CONF_ONE = {'label2': 'long value'}
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
EXPECTED_OUTPUT5 = [r"""FROM fedora
LABEL "labelnew"="base value"
"""]
EXPECTED_OUTPUT6 = [r"""FROM fedora
LABEL "labelnew"="long value" "label2"="long value"
""", r"""FROM fedora
LABEL "label2"="long value" "labelnew"="long value"
"""]
EXPECTED_OUTPUT7 = [r"""FROM fedora
LABEL "label2"="df value"
LABEL "labelnew"="df value"
"""]
EXPECTED_OUTPUT8 = [r"""FROM fedora
LABEL "label1"="df value"
LABEL "label2"="df value"
""", r"""FROM fedora
LABEL "label2"="df value"
LABEL "label1"="df value"
""",
]

@pytest.mark.parametrize('df_content, labels_conf_base, labels_conf, dont_overwrite, aliases, expected_output', [
    (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF, [], {}, EXPECTED_OUTPUT),
    (DF_CONTENT, LABELS_CONF_BASE, json.dumps(LABELS_CONF), [], {}, EXPECTED_OUTPUT),
    (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF_WRONG, [], {}, RuntimeError()),
    (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF, ["label1", ], {}, EXPECTED_OUTPUT2),
    (DF_CONTENT, LABELS_CONF_BASE, LABELS_BLANK, ["label1", ], {}, EXPECTED_OUTPUT3),
    (DF_CONTENT_SINGLE_LINE, LABELS_CONF_BASE, LABELS_CONF, ["label1", ], {}, EXPECTED_OUTPUT4),
    (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF, [], {"not": "present"}, EXPECTED_OUTPUT),
    (DF_CONTENT_SINGLE_LINE, LABELS_CONF_BASE, LABELS_BLANK, [], {"label1": "labelnew"}, EXPECTED_OUTPUT5),
    (DF_CONTENT_SINGLE_LINE, LABELS_CONF_BASE, LABELS_CONF_ONE, [], {"label2": "labelnew"}, EXPECTED_OUTPUT6),
    (DF_CONTENT_LABEL, LABELS_CONF_BASE, LABELS_BLANK, [], {"label2": "labelnew"}, EXPECTED_OUTPUT7),
    (DF_CONTENT_LABEL, LABELS_CONF_BASE, LABELS_BLANK, [], {"label2": "labelnew", "x": "y"}, EXPECTED_OUTPUT7),
    (DF_CONTENT_LABEL, LABELS_CONF_BASE_NONE, LABELS_BLANK, [], {"label2": "labelnew"}, EXPECTED_OUTPUT7),
    (DF_CONTENT_LABEL, LABELS_CONF_BASE, LABELS_BLANK, [], {"label2": "label1"}, EXPECTED_OUTPUT8),
])
def test_add_labels_plugin(tmpdir, docker_tasker,
                           df_content, labels_conf_base, labels_conf, dont_overwrite, aliases, expected_output):
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
            'args': {
                'labels': labels_conf,
                'dont_overwrite': dont_overwrite,
                'auto_labels': [],
                'aliases': aliases,
            }
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
    ('com.redhat.build-host', 'the-build-host'),
    ('Build_Host', 'the-build-host'),
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
            'args': {'labels': {}, "dont_overwrite": [], "auto_labels": [auto_label],
                     'aliases': {'Build_Host': 'com.redhat.build-host'}}
        }]
    )

    runner.run()
    assert re.match(value_re_part, df.labels[auto_label])

@pytest.mark.parametrize('df_old_as_plugin_arg', [True, False])
@pytest.mark.parametrize('df_new_as_plugin_arg', [True, False])
@pytest.mark.parametrize('base_old, base_new, df_old, df_new, expected_old, expected_new, expected_log', [
    (None,  None,  None,  None,  None,  None, None                             ),
    (None,  None,  None,  'A',   None,  'A',  None                             ),
    (None,  None,  'A',   None,  'A',   'A',  'as an alias for label'          ),
    (None,  None,  'A',   'A',   'A',   'A',  'already exists'                 ),
    (None,  None,  'A',   'B',   'A',   'B',  'should probably have same value'),
    (None,  'A',   None,  None,  None,  'A',  None                             ),
    (None,  'A',   None,  'A',   None,  'A',  None                             ),
    (None,  'A',   None,  'B',   None,  'B',  None                             ),
    (None,  'A',   'A',   None,  'A',   'A',  'as an alias for label'          ),
    (None,  'A',   'B',   None,  'B',   'B',  'as an alias for label'          ),
    (None,  'A',   'A',   'A',   'A',   'A',  'already exists'                 ),
    (None,  'A',   'A',   'B',   'A',   'B',  'should probably have same value'),
    (None,  'A',   'B',   'A',   'B',   'A',  'should probably have same value'),
    (None,  'A',   'B',   'B',   'B',   'B',  'already exists'                 ),
    (None,  'A',   'B',   'C',   'B',   'C',  'should probably have same value'),
    ('A',   None,  None,  None,  'A',   'A',  'as an alias for label'          ),
    ('A',   None,  None,  'A',   'A',   'A',  'already exists'                 ),
    ('A',   None,  None,  'B',   'A',   'B',  'should probably have same value'),
    ('A',   None,  'A',   None,  'A',   'A',  'as an alias for label'          ),
    ('A',   None,  'B',   None,  'B',   'B',  'as an alias for label'          ),
    ('A',   None,  'A',   'A',   'A',   'A',  'already exists'                 ),
    ('A',   None,  'A',   'B',   'A',   'B',  'should probably have same value'),
    ('A',   None,  'B',   'A',   'B',   'A',  'should probably have same value'),
    ('A',   None,  'B',   'B',   'B',   'B',  'already exists'                 ),
    ('A',   None,  'B',   'C',   'B',   'C',  'should probably have same value'),
    ('A',   'A',   None,  None,  'A',   'A',  'as an alias for label'          ),
    ('A',   'A',   None,  'A',   'A',   'A',  'already exists'                 ),
    ('A',   'A',   None,  'B',   'A',   'B',  'should probably have same value'),
    ('A',   'A',   'A',   None,  'A',   'A',  'as an alias for label'          ),
    ('A',   'A',   'B',   None,  'B',   'B',  'as an alias for label'          ),
    ('A',   'A',   'A',   'A',   'A',   'A',  'already exists'                 ),
    ('A',   'A',   'A',   'B',   'A',   'B',  'should probably have same value'),
    ('A',   'A',   'B',   'A',   'B',   'A',  'should probably have same value'),
    ('A',   'A',   'B',   'B',   'B',   'B',  'already exists'                 ),
    ('A',   'A',   'B',   'C',   'B',   'C',  'should probably have same value'),
    ('A',   'B',   None,  None,  'A',   'A',  'as an alias for label'          ), #really?
    ('A',   'B',   None,  'A',   'A',   'A',  'already exists'                 ),
    ('A',   'B',   None,  'B',   'A',   'B',  'should probably have same value'),
    ('A',   'B',   None,  'C',   'A',   'C',  'should probably have same value'),
    ('A',   'B',   'A',   None,  'A',   'A',  'as an alias for label'          ),
    ('A',   'B',   'B',   None,  'B',   'B',  'as an alias for label'          ),
    ('A',   'B',   'C',   None,  'C',   'C',  'as an alias for label'          ),
    ('A',   'B',   'A',   'A',   'A',   'A',  'already exists'                 ),
    ('A',   'B',   'A',   'B',   'A',   'B',  'should probably have same value'),
    ('A',   'B',   'A',   'C',   'A',   'C',  'should probably have same value'),
    ('A',   'B',   'B',   'A',   'B',   'A',  'should probably have same value'),
    ('A',   'B',   'B',   'B',   'B',   'B',  'already exists'                 ),
    ('A',   'B',   'B',   'C',   'B',   'C',  'should probably have same value'),
    ('A',   'B',   'C',   'A',   'C',   'A',  'should probably have same value'),
    ('A',   'B',   'C',   'B',   'C',   'B',  'should probably have same value'),
    ('A',   'B',   'C',   'C',   'C',   'C',  'already exists'                 ),
    ('A',   'B',   'C',   'D',   'C',   'D',  'should probably have same value'),
])
def test_add_labels_aliases(tmpdir, docker_tasker, caplog,
                            df_old_as_plugin_arg, df_new_as_plugin_arg,
                            base_old, base_new, df_old, df_new, expected_old, expected_new, expected_log):
    if MOCK:
        mock_docker()

    df_content = "FROM fedora\n"
    plugin_labels = {}
    if df_old:
        if df_old_as_plugin_arg:
            plugin_labels["label_old"] = df_old
        else:
            df_content += 'LABEL label_old="{0}"\n'.format(df_old)
    if df_new:
        if df_new_as_plugin_arg:
            plugin_labels["label_new"] = df_new
        else:
            df_content += 'LABEL label_new="{0}"\n'.format(df_new)

    base_labels = {"Config": {"Labels": {}}}
    if base_old:
        base_labels["Config"]["Labels"]["label_old"] = base_old
    if base_new:
        base_labels["Config"]["Labels"]["label_new"] = base_new

    df = DockerfileParser(str(tmpdir))
    df.content = df_content

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    flexmock(workflow, base_image_inspect=base_labels)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {
                'labels': plugin_labels,
                'dont_overwrite': [],
                'auto_labels': [],
                'aliases': {"label_old": "label_new"},
            }
        }]
    )

    runner.run()
    assert AddLabelsPlugin.key is not None
    result_old = df.labels.get("label_old") or base_labels["Config"]["Labels"].get("label_old")
    result_new = df.labels.get("label_new") or base_labels["Config"]["Labels"].get("label_new")
    assert result_old == expected_old
    assert result_new == expected_new

    if expected_log:
        assert expected_log in caplog.text()
