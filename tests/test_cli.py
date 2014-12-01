import subprocess
from dock.core import DockerTasker


PRIV_BUILD_IMAGE = "buildroot-fedora"
DH_BUILD_IMAGE = "buildroot-dh-fedora"
TEST_IMAGE = "test-image"
TEST_BUILD_IMAGE = "test-build-image"
GIT_URL = "https://github.com/TomasTomecek/docker-hello-world.git"
DOCKER0_ADDRESS = "172.17.42.1"  # docker's first choice, may change actually
LOCAL_REGISTRY = "%s:5000" % DOCKER0_ADDRESS


def test_simple_privileged_build():
    command = [
        "python",
        "dock/cli/main.py",
        "-v",
        "build",
        "--method", "privileged",
        "--build-image", PRIV_BUILD_IMAGE,
        "--image", TEST_IMAGE,
        "--git-url", GIT_URL,
        "--source-registry", LOCAL_REGISTRY,
    ]
    subprocess.check_call(command)


def test_simple_dh_build():
    command = [
        "python",
        "dock/cli/main.py",
        "-v",
        "build",
        "--method", "hostdocker",
        "--build-image", DH_BUILD_IMAGE,
        "--image", TEST_IMAGE,
        "--git-url", GIT_URL,
        "--source-registry", LOCAL_REGISTRY,
    ]
    subprocess.check_call(command)
    tasker = DockerTasker()
    assert tasker.image_exists(TEST_IMAGE)


def test_create_build_image():
    command = [
        "python",
        "dock/cli/main.py",
        "-v",
        "create-build-image",
        "--dock-local-path", "./",
        "./images/privileged-builder",
        TEST_BUILD_IMAGE,
    ]
    subprocess.check_call(command)
    tasker = DockerTasker()
    assert tasker.image_exists(TEST_BUILD_IMAGE)
    tasker.remove_image(TEST_BUILD_IMAGE)


def test_create_build_image_with_dock_cli():
    command = [
        "dock",
        "-v",
        "create-build-image",
        "--dock-tarball-path", "/usr/share/dock/dock.tar.gz",
        "/usr/share/dock/images/privileged-builder",
        TEST_BUILD_IMAGE,
    ]
    subprocess.check_call(command)
    tasker = DockerTasker()
    assert tasker.image_exists(TEST_BUILD_IMAGE)
    tasker.remove_image(TEST_BUILD_IMAGE)
