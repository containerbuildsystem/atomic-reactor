"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import logging
import os
import sys
import codecs
import encodings

import pytest
import flexmock

from atomic_reactor.core import DockerTasker
from atomic_reactor.plugin import InputPluginsRunner
import atomic_reactor.cli.main
from atomic_reactor.constants import BUILD_JSON_ENV

from tests.util import uuid_value
from tests.constants import DOCKERFILE_GIT, DOCKERFILE_OK_PATH, MOCK

if MOCK:
    from tests.docker_mock import mock_docker

PRIV_BUILD_IMAGE = uuid_value()
DH_BUILD_IMAGE = uuid_value()


logger = logging.getLogger('atomic_reactor.tests')

if MOCK:
    mock_docker()
dt = DockerTasker()
reactor_root = os.path.dirname(os.path.dirname(__file__))

with_all_sources = pytest.mark.parametrize('source_provider, uri', [
    ('git', DOCKERFILE_GIT),
    ('path', DOCKERFILE_OK_PATH),
])

# TEST-SUITE SETUP


def teardown_module(module):
    if MOCK:
        return
    dt.remove_image(PRIV_BUILD_IMAGE, force=True)
    dt.remove_image(DH_BUILD_IMAGE, force=True)


# TESTS

class TestCLISuite(object):

    def exec_cli(self, command):
        saved_args = sys.argv
        sys.argv = command
        atomic_reactor.cli.main.run()
        sys.argv = saved_args

    def test_log_encoding(self, caplog, monkeypatch):
        if MOCK:
            mock_docker()

        (flexmock(InputPluginsRunner)
            .should_receive('__init__')
            .and_raise(RuntimeError))

        monkeypatch.setenv('LC_ALL', 'en_US.UTF-8')
        monkeypatch.setenv(BUILD_JSON_ENV, '{}')
        command = [
            "main.py",
            "--verbose",
            "inside-build",
        ]
        with caplog.at_level(logging.INFO):
            with pytest.raises(RuntimeError):
                self.exec_cli(command)

        # first message should be 'log encoding: <encoding>'
        match = caplog.records[0].message.split(':')
        if not match:
            raise RuntimeError

        encoding = codecs.getreader(match[1])
        assert encoding == encodings.utf_8.StreamReader
