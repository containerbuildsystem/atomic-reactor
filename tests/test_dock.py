from dock.core import DockerTasker
from dock.outer import PrivilegedBuildManager, DockerhostBuildManager


TEST_IMAGE = "dock-test-image"
LOCAL_REGISTRY = "172.17.42.1:5000"


def test_hostdocker_build():
    image_name = "dock-test-ssh-image"
    m = DockerhostBuildManager("buildroot-dh-fedora", {
        "git_url": "https://github.com/fedora-cloud/Fedora-Dockerfiles.git",
        "git_dockerfile_path": "ssh/",
        "image": image_name,
        "parent_registry": LOCAL_REGISTRY,  # faster
    })
    results = m.build()
    dt = DockerTasker()
    img = dt.pull_image(image_name, LOCAL_REGISTRY)
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
    dt.remove_image(img)
    dt.remove_image(image_name)


def test_privileged_gitrepo_build():
    image_name = "dock-test-ssh-image"
    m = PrivilegedBuildManager("buildroot-fedora", {
        "git_url": "https://github.com/fedora-cloud/Fedora-Dockerfiles.git",
        "git_dockerfile_path": "ssh/",
        "image": image_name,
        "parent_registry": LOCAL_REGISTRY,  # faster
    })
    results = m.build()
    dt = DockerTasker()
    img = dt.pull_image(image_name, LOCAL_REGISTRY)
    dt.remove_image(img)
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
    dt.remove_image(img)


def test_privileged_build():
    m = PrivilegedBuildManager("buildroot-fedora", {
        "git_url": "https://github.com/TomasTomecek/docker-hello-world.git",
        "image": TEST_IMAGE,
        "parent_registry": LOCAL_REGISTRY,  # faster
    })
    results = m.build()
    dt = DockerTasker()
    img = dt.pull_image(TEST_IMAGE, LOCAL_REGISTRY)
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
    dt.remove_image(img)
