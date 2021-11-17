"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from pathlib import Path

import pytest
import requests
import requests.exceptions
from tests.constants import LOCALHOST_REGISTRY_HTTP, DOCKER0_REGISTRY_HTTP, TEST_IMAGE
from tests.util import uuid_value

from osbs.utils import ImageName
from atomic_reactor.inner import DockerBuildWorkflow


@pytest.fixture()
def temp_image_name():
    return ImageName(repo=("atomic-reactor-tests-%s" % uuid_value()))


@pytest.fixture()
def is_registry_running():
    """
    is docker registry running (at {docker0,lo}:5000)?
    """
    try:
        lo_response = requests.get(LOCALHOST_REGISTRY_HTTP)
    except requests.exceptions.ConnectionError:
        return False
    if not lo_response.ok:
        return False
    try:
        lo_response = requests.get(DOCKER0_REGISTRY_HTTP)  # leap of faith
    except requests.exceptions.ConnectionError:
        return False
    if not lo_response.ok:
        return False
    return True


@pytest.fixture(params=[True, False])
def reactor_config_map(request):
    return request.param


@pytest.fixture(params=[True, False])
def inspect_only(request):
    return request.param


@pytest.fixture
def user_params(monkeypatch):
    """
    Setting default image_tag in the user params. Any tests requiring to create an instance
    of :class:`DockerBuildWorkflow` requires this fixture.
    """
    monkeypatch.setattr(DockerBuildWorkflow, "_default_user_params", {"image_tag": TEST_IMAGE})


@pytest.fixture
def workflow(build_dir, user_params):
    return DockerBuildWorkflow(build_dir, source=None)


@pytest.mark.optionalhook
def pytest_html_results_table_row(report, cells):
    if report.passed or report.skipped:
        del cells[:]


@pytest.fixture
def build_dir(tmpdir):
    """
    This is the --build-dir passed to atomic-reactor tasks used to initialize
    the RootBuildDir object.
    """
    return Path(tmpdir.join("build_dir").mkdir())


@pytest.fixture
def source_dir(tmpdir):
    """
    The directory holding source files and can be passed to the mock Source object.
    """
    return Path(tmpdir.join("source_dir").mkdir())
