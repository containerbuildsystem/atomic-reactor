from dock.core import DockerTasker
from dock.outer import PrivilegedBuildManager, DockerhostBuildManager
from dock.util import ImageName
from tests.constants import LOCALHOST_REGISTRY, DOCKERFILE_GIT, TEST_IMAGE, MOCK

if MOCK:
    from tests.docker_mock import mock_docker

def test_hostdocker_build():
    if MOCK:
        mock_docker()

    image_name = ImageName(repo="dock-test-ssh-image")
    remote_image = image_name.copy()
    remote_image.registry = LOCALHOST_REGISTRY
    m = DockerhostBuildManager("buildroot-dh-fedora", {
        "git_url": "https://github.com/fedora-cloud/Fedora-Dockerfiles.git",
        "git_dockerfile_path": "ssh/",
        "image": remote_image.to_str(),
        "parent_registry": LOCALHOST_REGISTRY,  # faster
        "target_registries_insecure": True,
        "parent_registry_insecure": True,
    })
    results = m.build()
    dt = DockerTasker()
    dt.pull_image(remote_image, insecure=True)
    assert len(results.build_logs) > 0
    # assert isinstance(results.built_img_inspect, dict)
    # assert len(results.built_img_inspect.items()) > 0
    # assert isinstance(results.built_img_info, dict)
    # assert len(results.built_img_info.items()) > 0
    # assert isinstance(results.base_img_info, dict)
    # assert len(results.base_img_info.items()) > 0
    # assert len(results.base_plugins_output) > 0
    # assert len(results.built_img_plugins_output) > 0
    dt.remove_container(results.container_id)
    dt.remove_image(remote_image)


def test_hostdocker_error_build():
    if MOCK:
        mock_docker()

    image_name = TEST_IMAGE
    m = DockerhostBuildManager("buildroot-dh-fedora", {
        "git_url": DOCKERFILE_GIT,
        "git_commit": "error-build",
        "image": image_name,
        "parent_registry": LOCALHOST_REGISTRY,  # faster
        "target_registries_insecure": True,
        "parent_registry_insecure": True,
        })
    results = m.build()
    dt = DockerTasker()
    assert len(results.build_logs) > 0
    assert results.return_code != 0
    dt.remove_container(results.container_id)


def test_privileged_gitrepo_build():
    if MOCK:
        mock_docker()

    image_name = ImageName(repo="dock-test-ssh-image")
    remote_image = image_name.copy()
    remote_image.registry = LOCALHOST_REGISTRY
    m = PrivilegedBuildManager("buildroot-fedora", {
        "git_url": "https://github.com/fedora-cloud/Fedora-Dockerfiles.git",
        "git_dockerfile_path": "ssh/",
        "image": remote_image.to_str(),
        "parent_registry": LOCALHOST_REGISTRY,  # faster
        "target_registries_insecure": True,
        "parent_registry_insecure": True,
    })
    results = m.build()
    dt = DockerTasker()
    dt.pull_image(remote_image, insecure=True)
    assert len(results.build_logs) > 0
    # assert isinstance(results.built_img_inspect, dict)
    # assert len(results.built_img_inspect.items()) > 0
    # assert isinstance(results.built_img_info, dict)
    # assert len(results.built_img_info.items()) > 0
    # assert isinstance(results.base_img_info, dict)
    # assert len(results.base_img_info.items()) > 0
    # assert len(results.base_plugins_output) > 0
    # assert len(results.built_img_plugins_output) > 0
    dt.remove_container(results.container_id)
    dt.remove_image(remote_image)

def test_privileged_build():
    if MOCK:
        mock_docker()

    image_name = ImageName(repo=TEST_IMAGE)
    remote_image = image_name.copy()
    remote_image.registry = LOCALHOST_REGISTRY
    m = PrivilegedBuildManager("buildroot-fedora", {
        "git_url": DOCKERFILE_GIT,
        "image": remote_image.to_str(),
        "parent_registry": LOCALHOST_REGISTRY,  # faster
        "target_registries_insecure": True,
        "parent_registry_insecure": True,
    })
    results = m.build()
    dt = DockerTasker()
    dt.pull_image(remote_image, insecure=True)
    assert len(results.build_logs) > 0
    # assert isinstance(results.built_img_inspect, dict)
    # assert len(results.built_img_inspect.items()) > 0
    # assert isinstance(results.built_img_info, dict)
    # assert len(results.built_img_info.items()) > 0
    # assert isinstance(results.base_img_info, dict)
    # assert len(results.base_img_info.items()) > 0
    # assert len(results.base_plugins_output) > 0
    # assert len(results.built_img_plugins_output) > 0
    dt.remove_container(results.container_id)
    dt.remove_image(remote_image)
