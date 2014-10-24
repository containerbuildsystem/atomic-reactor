from dock.core import DockerBuilder
from dock.outer import PrivilegedDockerBuilder


def test_hostdocker_build():
    db = DockerBuilder(
        "https://github.com/TomasTomecek/docker-hello-world.git",
        "dock-test-image",
    )
    db.build_hostdocker("buildroot-fedora")


def test_privileged_build():
    db = PrivilegedDockerBuilder("buildroot-fedora", {
        #"git_url": "https://github.com/TomasTomecek/docker-hello-world.git",
        "git_url": "github.com/TomasTomecek/docker-hello-world.git",
        "local_tag": "dock-test-image",
    })
    db.build()
