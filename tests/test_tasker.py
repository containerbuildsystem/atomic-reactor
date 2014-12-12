import git
from docker.errors import APIError
import pytest

from dock.core import DockerTasker


IMAGE_NAME = "busybox:latest"
TEST_IMAGE = "test-image"
GIT_URL = "https://github.com/TomasTomecek/docker-hello-world.git"


def test_run():
    t = DockerTasker()
    container_id = t.run(IMAGE_NAME, command="id")
    t.wait(container_id)
    t.remove_container(container_id)


def test_run_invalid_command():
    t = DockerTasker()
    command = "eporeporjgpeorjgpeorjgpeorjgpeorjgpeorjg"  # I hope this doesn't exist
    with pytest.raises(APIError):
        t.run(IMAGE_NAME, command=command)
    # remove the container
    containers = t.d.containers(all=True)
    container_id = [c for c in containers if c["Command"] == command][0]['Id']
    t.remove_container(container_id)


def test_image_exists():
    t = DockerTasker()
    assert t.image_exists(IMAGE_NAME) is True


def test_image_doesnt_exist():
    t = DockerTasker()
    assert t.image_exists("lerknglekrnglekrnglekrnglekrng") is False


def test_logs():
    t = DockerTasker()
    container_id = t.run(IMAGE_NAME, command="id")
    t.wait(container_id)
    output = t.logs(container_id, stderr=True, stream=False)
    assert "\n".join(output).startswith("uid=")
    t.remove_container(container_id)


def test_remove_container():
    t = DockerTasker()
    container_id = t.run(IMAGE_NAME, command="id")
    t.wait(container_id)
    t.remove_container(container_id)


def test_remove_image():
    t = DockerTasker()
    container_id = t.run(IMAGE_NAME, command="id")
    t.wait(container_id)
    image_id = t.commit_container(container_id, repository=TEST_IMAGE)
    t.remove_container(container_id)
    t.remove_image(TEST_IMAGE)
    assert not t.image_exists(TEST_IMAGE)


def test_commit_container():
    t = DockerTasker()
    container_id = t.run(IMAGE_NAME, command="id")
    t.wait(container_id)
    image_id = t.commit_container(container_id, message="test message", repository=TEST_IMAGE)
    assert t.image_exists(image_id)
    t.remove_container(container_id)
    t.remove_image(TEST_IMAGE)


def test_inspect_image():
    t = DockerTasker()
    inspect_data = t.inspect_image(IMAGE_NAME)
    assert isinstance(inspect_data, dict)


def test_tag_image():
    t = DockerTasker()
    expected_img = "somewhere.example.com/test-image:1"
    img = t.tag_image(IMAGE_NAME, 'test-image', reg_uri="somewhere.example.com", tag='1')
    assert t.image_exists(expected_img)
    assert img == expected_img
    t.remove_image(expected_img)


def test_push_image():
    t = DockerTasker()
    expected_img = "localhost:5000/test-image:1"
    t.tag_image(IMAGE_NAME, 'test-image', reg_uri="localhost:5000", tag='1')
    output = t.push_image(expected_img)
    assert output is not None
    t.remove_image(expected_img)


def test_tag_and_push():
    t = DockerTasker()
    expected_img = "localhost:5000/test-image:1"
    output = t.tag_and_push_image(IMAGE_NAME, 'test-image', reg_uri="localhost:5000", tag='1')
    assert output is not None
    assert t.image_exists(expected_img)
    t.remove_image(expected_img)


def test_pull_image():
    t = DockerTasker()
    expected_img = "localhost:5000/busybox"
    t.tag_and_push_image('busybox', 'busybox', 'localhost:5000')
    got_image = t.pull_image('busybox', 'localhost:5000')
    assert expected_img == got_image
    assert len(t.last_logs) > 0
    t.remove_image(got_image)


def test_get_image_info_by_id_nonexistent():
    t = DockerTasker()
    response = t.get_image_info_by_image_id("asd")
    assert response is None


def test_get_image_info_by_id():
    t = DockerTasker()
    image_id = t.get_image_info_by_image_name("busybox")[0]['Id']
    response = t.get_image_info_by_image_id(image_id)
    assert isinstance(response, dict)


def test_get_image_info_by_name_tag_in_name():
    t = DockerTasker()
    response = t.get_image_info_by_image_name(image_name="busybox:latest")
    assert len(response) == 0


def test_build_image_from_path(tmpdir):
    tmpdir_path = str(tmpdir.realpath())
    git.Repo.clone_from(GIT_URL, tmpdir_path)
    df = tmpdir.join("Dockerfile")
    assert df.check()
    t = DockerTasker()
    response = t.build_image_from_path(tmpdir_path, TEST_IMAGE, stream=False, use_cache=True)
    print list(response)
    assert response is not None
    assert t.image_exists(TEST_IMAGE)
    t.remove_image(TEST_IMAGE)


def test_build_image_from_git():
    t = DockerTasker()
    response = t.build_image_from_git(GIT_URL, TEST_IMAGE, stream=False, use_cache=True)
    assert response is not None
    print list(response)
    assert t.image_exists(TEST_IMAGE)
    t.remove_image(TEST_IMAGE)
