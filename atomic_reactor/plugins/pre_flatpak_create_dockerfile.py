"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Takes a flatpak.json from the git repository, and the module information
looked up by pre_resolve_module_compose, and outputs
a Dockerfile that will build a filesystem image for the module
at /var/tmp/flatpak-build.

Example configuration:
{
    'name': 'flatpak_create_dockerfile',
    'args': {'base_image': 'registry.fedoraproject.org/fedora:latest'}
}
"""

import json
import os

from atomic_reactor.constants import FLATPAK_FILENAME, DOCKERFILE_FILENAME, YUM_REPOS_DIR
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_resolve_module_compose import get_compose_info
from atomic_reactor.plugins.build_orchestrate_build import override_build_kwarg
from atomic_reactor.rpm_util import rpm_qf_args
from atomic_reactor.util import render_yum_repo

DOCKERFILE_TEMPLATE = '''FROM {base_image}

LABEL name="{name}"
LABEL com.redhat.component="{name}"
LABEL version="{stream}"
LABEL release="{version}"

RUN dnf -y --nogpgcheck --disablerepo=* --enablerepo=atomic-reactor-module-* \\
    --installroot=/var/tmp/flatpak-build install {packages}
RUN rpm --root=/var/tmp/flatpak-build {rpm_qf_args} > /var/tmp/flatpak-build.rpm_qf
COPY cleanup.sh /var/tmp/flatpak-build/tmp/
RUN chroot /var/tmp/flatpak-build/ /bin/sh /tmp/cleanup.sh
'''


class FlatpakSourceInfo(object):
    def __init__(self, flatpak_json, compose):
        self.flatpak_json = flatpak_json
        self.compose = compose

        mmd = compose.base_module.mmd
        self.runtime = 'runtime' in mmd.profiles

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
        flatpak_path = os.path.join(self.workflow.builder.df_dir, FLATPAK_FILENAME)
        with open(flatpak_path, 'r') as fp:
            flatpak_json = json.load(fp)

        compose_info = get_compose_info(self.workflow)
        if compose_info is None:
            raise RuntimeError(
                "resolve_module_compose must be run before flatpak_create_dockerfile")

        return FlatpakSourceInfo(flatpak_json, compose_info)

    def run(self):
        """
        run the plugin
        """

        source = self._load_source()

        set_flatpak_source_info(self.workflow, source)

        # Create the dockerfile

        if source.runtime:
            profile = 'runtime'
        else:
            profile = 'default'

        module_info = source.compose.base_module

        packages = ' '.join(module_info.mmd.profiles[profile].rpms)

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
            for line in source.flatpak_json.get('cleanup-commands', []):
                f.write(line)
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
