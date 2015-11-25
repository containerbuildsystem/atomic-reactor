"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


How to test:

1. Install dockpulp, e.g.:
    pip install --upgrade git+https://github.com/release-engineering/dockpulp.git

2. Get pulp instance ( :> )

3. Create dockpulp config:
    vim /etc/dockpulp.conf

4. Login to pulp:
    dock-pulp -s instance login -u user -p password

5. Run these tests:
    PULP_INSTANCE="this" py.test integration-tests/
"""

import os
import logging

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.util import ImageName
from atomic_reactor.plugins.post_push_to_pulp import PulpUploader

import dockpulp
import pytest
import docker
from docker.errors import APIError


logger = logging.getLogger('atomic_reactor.tests')

SOURCE = {"provider": "git", "uri": "git://example.com/test.git"}


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
def test_push(tmpdir):
    """
    this is an integration test which should be run against real pulp
    """
    client = docker.AutoVersionClient()
    try:
        client.inspect_image("busybox:latest")
    except APIError:
        client.pull("busybox", tag="latest")
    image = client.get_image("busybox:latest")
    image_tar_path = os.path.join(str(tmpdir), "busybox.tar")
    image_file = open(image_tar_path, "w")
    image_file.write(image.data)
    image_file.close()
    registry_name = os.environ.get("PULP_INSTANCE", None) or "dev"
    secret_path = os.path.expanduser("~/.pulp/")

    image_names = [ImageName.parse("test/busybox-test")]

    workflow = DockerBuildWorkflow(SOURCE, "test/busybox-test")

    uploader = PulpUploader(workflow, registry_name, image_tar_path, logger,
                            pulp_secret_path=secret_path)
    uploader.push_tarball_to_pulp(image_names)
