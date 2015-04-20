from __future__ import print_function, unicode_literals

import uuid
import pytest
import requests
import requests.exceptions
from tests.constants import LOCALHOST_REGISTRY_HTTP, DOCKER0_REGISTRY_HTTP

from dock.util import ImageName


def get_uuid():
    u = uuid.uuid4()
    try:
        return u.get_hex()  # py2
    except AttributeError:
        return u.hex  # py3


@pytest.fixture()
def temp_image_name():
    return ImageName(repo=("dock-tests-%s" % get_uuid()))


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
