"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Python API for dock. This is the official way of interacting with dock.
"""
from dock.inner import DockerBuildWorkflow
from dock.outer import PrivilegedBuildManager, DockerhostBuildManager


__all__ = (
    'build_image_in_privileged_container',
    'build_image_using_hosts_docker',
    'build_image_here',
)


def build_image_in_privileged_container(build_image, git_url, image,
        git_dockerfile_path=None, git_commit=None, parent_registry=None,
        target_registries=None, push_buildroot_to=None,
        parent_registry_insecure=False, target_registries_insecure=False,
        **kwargs):
    """
    build image from provided dockerfile (specified as git url) in privileged image

    :param build_image: str, image where target image should be built
    :param git_url: str, URL to git repo
    :param image: str, tag for built image ([registry/]image_name[:tag])
    :param git_dockerfile_path: str, path to dockerfile within git repo (if not in root)
    :param git_commit: str, git commit to check out
    :param parent_registry: str, registry to pull base image from
    :param target_registries: list of str, list of registries to push image to (might change in future)
    :param push_buildroot_to: str, repository where buildroot should be pushed
    :param parent_registry_insecure: bool, allow connecting to parent registry over plain http
    :param target_registries_insecure: bool, allow connecting to target registries over plain http

    :return: BuildResults
    """
    build_json = {
        "git_url": git_url,
        "image": image,
        "git_dockerfile_path": git_dockerfile_path,
        "git_commit": git_commit,
        "parent_registry": parent_registry,
        "target_registries": target_registries,
        "parent_registry_insecure": parent_registry_insecure,
        "target_registries_insecure": target_registries_insecure,
    }
    build_json.update(kwargs)
    m = PrivilegedBuildManager(build_image, build_json)
    build_response = m.build()
    if push_buildroot_to:
        m.commit_buildroot()
        m.push_buildroot(push_buildroot_to)
    return build_response


def build_image_using_hosts_docker(build_image, git_url, image,
        git_dockerfile_path=None, git_commit=None, parent_registry=None,
        target_registries=None, push_buildroot_to=None,
        parent_registry_insecure=False, target_registries_insecure=False,
        **kwargs):
    """
    build image from provided dockerfile (specified as git url) in container
    using docker from host

    :param build_image: str, image where target image should be built
    :param git_url: str, URL to git repo
    :param image: str, tag for built image ([registry/]image_name[:tag])
    :param git_dockerfile_path: str, path to dockerfile within git repo (if not in root)
    :param git_commit: str, git commit to check out
    :param parent_registry: str, registry to pull base image from
    :param target_registries: list of str, list of registries to push image to (might change in future)
    :param push_buildroot_to: str, repository where buildroot should be pushed
    :param parent_registry_insecure: bool, allow connecting to parent registry over plain http
    :param target_registries_insecure: bool, allow connecting to target registries over plain http

    :return: BuildResults
    """
    build_json = {
        "git_url": git_url,
        "image": image,
        "git_dockerfile_path": git_dockerfile_path,
        "git_commit": git_commit,
        "parent_registry": parent_registry,
        "target_registries": target_registries,
        "parent_registry_insecure": parent_registry_insecure,
        "target_registries_insecure": target_registries_insecure,
    }
    build_json.update(kwargs)
    m = DockerhostBuildManager(build_image, build_json)
    build_response = m.build()
    if push_buildroot_to:
        m.commit_buildroot()
        m.push_buildroot(push_buildroot_to)
    return build_response


def build_image_here(git_url, image,
        git_dockerfile_path=None, git_commit=None, parent_registry=None,
        target_registries=None, parent_registry_insecure=False,
        target_registries_insecure=False, **kwargs):
    """
    build image from provided dockerfile (specified as git url) in current environment

    :param git_url: str, URL to git repo
    :param image: str, tag for built image ([registry/]image_name[:tag])
    :param git_dockerfile_path: str, path to dockerfile within git repo (if not in root)
    :param git_commit: str, git commit to check out
    :param parent_registry: str, registry to pull base image from
    :param target_registries: list of str, list of registries to push image to (might change in future)
    :param parent_registry_insecure: bool, allow connecting to parent registry over plain http
    :param target_registries_insecure: bool, allow connecting to target registries over plain http

    :return: BuildResults
    """
    build_json = {
        "git_url": git_url,
        "image": image,
        "git_dockerfile_path": git_dockerfile_path,
        "git_commit": git_commit,
        "parent_registry": parent_registry,
        "target_registries": target_registries,
        "parent_registry_insecure": parent_registry_insecure,
        "target_registries_insecure": target_registries_insecure,
    }
    build_json.update(kwargs)
    m = DockerBuildWorkflow(**build_json)
    return m.build_docker_image()


def list_dockerfiles_in_git():
    """
    clone provided repo and return all dockerfiles found in the repo

    :return:
    """
