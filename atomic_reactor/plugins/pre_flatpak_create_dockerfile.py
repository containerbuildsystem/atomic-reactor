"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Combines the module information looked up by pre_resolve_module_compose,
combines it with additional information from container.yaml, and
generates a Dockerfile that will build a filesystem image for the module
at /var/tmp/flatpak-build.

Example configuration:
{
    'name': 'flatpak_create_dockerfile',
    'args': {'base_image': 'registry.fedoraproject.org/fedora:latest'}
}
"""

import os
import yaml

from atomic_reactor.constants import DOCKERFILE_FILENAME, REPO_CONTAINER_CONFIG, YUM_REPOS_DIR
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_resolve_module_compose import get_compose_info
from atomic_reactor.plugins.build_orchestrate_build import override_build_kwarg
from atomic_reactor.rpm_util import rpm_qf_args
from atomic_reactor.util import render_yum_repo, split_module_spec

DOCKERFILE_TEMPLATE = '''FROM {base_image}

LABEL name="{name}"
LABEL com.redhat.component="{name}"
LABEL version="{stream}"
LABEL release="{version}"

RUN dnf -y --nogpgcheck \\
    --disablerepo=* \\
    --enablerepo=atomic-reactor-koji-plugin-* \\
    --enablerepo=atomic-reactor-module-* \\
    --installroot=/var/tmp/flatpak-build install {packages}
RUN rpm --root=/var/tmp/flatpak-build {rpm_qf_args} > /var/tmp/flatpak-build.rpm_qf
COPY cleanup.sh /var/tmp/flatpak-build/tmp/
RUN chroot /var/tmp/flatpak-build/ /bin/sh /tmp/cleanup.sh
'''


class FlatpakSourceInfo(object):
    def __init__(self, flatpak_yaml, compose):
        self.flatpak_yaml = flatpak_yaml
        self.compose = compose

        mmd = compose.base_module.mmd
        # A runtime module must have a 'runtime' profile, but can have other
        # profiles for SDKs, minimal runtimes, etc.
        self.runtime = 'runtime' in mmd.profiles

        module_spec = split_module_spec(compose.source_spec)
        if module_spec.profile:
            self.profile = module_spec.profile
        elif self.runtime:
            self.profile = 'runtime'
        else:
            self.profile = 'default'

        assert self.profile in mmd.profiles

    def koji_metadata(self):
        metadata = self.compose.koji_metadata()
        metadata['flatpak'] = True

        return metadata


WORKSPACE_SOURCE_KEY = 'source_info'


def get_flatpak_source_info(workflow):
    key = FlatpakCreateDockerfilePlugin.key
    if key not in workflow.plugin_workspace:
        return None
    return workflow.plugin_workspace[key].get(WORKSPACE_SOURCE_KEY, None)


def set_flatpak_source_info(workflow, source):
    key = FlatpakCreateDockerfilePlugin.key

    workflow.plugin_workspace.setdefault(key, {})
    workspace = workflow.plugin_workspace[key]
    workspace[WORKSPACE_SOURCE_KEY] = source


class FlatpakCreateDockerfilePlugin(PreBuildPlugin):
    key = "flatpak_create_dockerfile"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow,
                 base_image=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param base_image: host image used to install packages when creating the Flatpak
        """
        # call parent constructor
        super(FlatpakCreateDockerfilePlugin, self).__init__(tasker, workflow)

        self.base_image = base_image

    def _load_source(self):
        container_yaml_path = os.path.join(self.workflow.builder.df_dir, REPO_CONTAINER_CONFIG)
        with open(container_yaml_path, 'r') as fp:
            container_yaml = yaml.safe_load(fp)
        flatpak_yaml = container_yaml['flatpak']

        compose_info = get_compose_info(self.workflow)
        if compose_info is None:
            raise RuntimeError(
                "resolve_module_compose must be run before flatpak_create_dockerfile")

        return FlatpakSourceInfo(flatpak_yaml, compose_info)

    def run(self):
        """
        run the plugin
        """

        source = self._load_source()

        set_flatpak_source_info(self.workflow, source)

        module_info = source.compose.base_module

        # For a runtime, certain information is duplicated between the container.yaml
        # and the modulemd, check that it matches
        if source.runtime:
            flatpak_yaml = source.flatpak_yaml
            flatpak_xmd = module_info.mmd.xmd['flatpak']

            def check(condition, what):
                if not condition:
                    raise RuntimeError(
                        "Mismatch for {} betweeen module xmd and container.yaml".format(what))

            check(flatpak_yaml['branch'] == flatpak_xmd['branch'], "'branch'")
            check(source.profile in flatpak_xmd['runtimes'], 'profile name')

            profile_xmd = flatpak_xmd['runtimes'][source.profile]

            check(flatpak_yaml['id'] == profile_xmd['id'], "'id'")
            check(flatpak_yaml.get('runtime', None) ==
                  profile_xmd.get('runtime', None), "'runtime'")
            check(flatpak_yaml.get('sdk', None) == profile_xmd.get('sdk', None), "'sdk'")

        # Create the dockerfile

        packages = ' '.join(module_info.mmd.profiles[source.profile].rpms)

        df_path = os.path.join(self.workflow.builder.df_dir, DOCKERFILE_FILENAME)
        with open(df_path, 'w') as fp:
            fp.write(DOCKERFILE_TEMPLATE.format(name=module_info.name,
                                                stream=module_info.stream,
                                                version=module_info.version,
                                                base_image=self.base_image,
                                                packages=packages,
                                                rpm_qf_args=rpm_qf_args()))

        self.workflow.builder.set_df_path(df_path)

        # Create the cleanup script

        cleanupscript = os.path.join(self.workflow.builder.df_dir, "cleanup.sh")
        with open(cleanupscript, 'w') as f:
            cleanup_commands = source.flatpak_yaml.get('cleanup-commands')
            if cleanup_commands is not None:
                f.write(cleanup_commands.rstrip())
                f.write("\n")
        os.chmod(cleanupscript, 0o0755)

        # Add a yum-repository pointing to the compose

        repo_name = 'atomic-reactor-module-{name}-{stream}-{version}'.format(
            name=module_info.name,
            stream=module_info.stream,
            version=module_info.version)

        repo = {
            'name': repo_name,
            'baseurl': source.compose.repo_url,
            'enabled': 1,
            'gpgcheck': 0,
        }

        path = os.path.join(YUM_REPOS_DIR, repo_name + '.repo')
        self.workflow.files[path] = render_yum_repo(repo, escape_dollars=False)

        override_build_kwarg(self.workflow, 'module_compose_id', source.compose.compose_id)
