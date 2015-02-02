import uuid
import pytest

@pytest.fixture()
def temp_image_name():
    u = uuid.uuid4()
    try:
        return u.get_hex()  # py2
    except AttributeError:
        return u.hex  # py3
