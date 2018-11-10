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

from flatpak_module_tools.flatpak_builder import FlatpakSourceInfo, FlatpakBuilder

from atomic_reactor.constants import DOCKERFILE_FILENAME, YUM_REPOS_DIR
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import get_flatpak_base_image
from atomic_reactor.plugins.pre_resolve_module_compose import get_compose_info
from atomic_reactor.rpm_util import rpm_qf_args
from atomic_reactor.util import render_yum_repo, split_module_spec
from atomic_reactor.yum_util import YumRepo


# /var/tmp/flatpak-build is the final image we'll turn into a Flaptak
# In order for 'dnf module enable' to work correctly, we need an
# /etc/os-release in the install root with the correct PLATFORM_ID
# for our base package set. To make that work, we install system-release
# into a *different* install root and copy /etc/os-release over.
DOCKERFILE_TEMPLATE = '''FROM {base_image}

LABEL name="{name}"
LABEL com.redhat.component="{name}"
LABEL version="{stream}"
LABEL release="{version}"

ADD atomic-reactor-includepkgs /tmp/

RUN mkdir -p /var/tmp/flatpak-build/dev && \
    for i in null zero random urandom ; do cp -a /dev/$i /var/tmp/flatpak-build/dev ; done

RUN cat /tmp/atomic-reactor-includepkgs >> /etc/dnf/dnf.conf && \\
    INSTALLDIR=/var/tmp/flatpak-build && \\
    DNF='\\
    dnf -y --nogpgcheck \\
    --disablerepo=* \\
    --enablerepo=atomic-reactor-koji-plugin-* \\
    --enablerepo=atomic-reactor-module-* \\
    ' && \\
    $DNF --installroot=$INSTALLDIR-init install system-release && \\
    mkdir -p $INSTALLDIR/etc/ && \\
    cp $INSTALLDIR-init/etc/os-release $INSTALLDIR/etc/os-release && \\
    $DNF --installroot=$INSTALLDIR module enable {modules} && \\
    $DNF --installroot=$INSTALLDIR install {packages}
RUN rpm --root=/var/tmp/flatpak-build {rpm_qf_args} > /var/tmp/flatpak-build.rpm_qf
COPY cleanup.sh /var/tmp/flatpak-build/tmp/
RUN chroot /var/tmp/flatpak-build/ /bin/sh /tmp/cleanup.sh
'''


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

        self.base_image = get_flatpak_base_image(workflow, base_image)

    def _load_source(self):
        flatpak_yaml = self.workflow.source.config.flatpak

        compose_info = get_compose_info(self.workflow)
        if compose_info is None:
            raise RuntimeError(
                "resolve_module_compose must be run before flatpak_create_dockerfile")

        module_spec = split_module_spec(compose_info.source_spec)

        return FlatpakSourceInfo(flatpak_yaml,
                                 compose_info.modules,
                                 compose_info.base_module,
                                 module_spec.profile)

    def run(self):
        """
        run the plugin
        """

        source = self._load_source()

        set_flatpak_source_info(self.workflow, source)

        builder = FlatpakBuilder(source, None, None)

        builder.precheck()

        # Create the dockerfile

        module_info = source.base_module

        # We need to enable all the modules other than the platform pseudo-module
        modules_str = ' '.join(builder.get_enable_modules())

        install_packages_str = ' '.join(builder.get_install_packages())

        df_path = os.path.join(self.workflow.builder.df_dir, DOCKERFILE_FILENAME)
        with open(df_path, 'w') as fp:
            fp.write(DOCKERFILE_TEMPLATE.format(name=module_info.name,
                                                stream=module_info.stream.replace('-', '_'),
                                                version=module_info.version,
                                                base_image=self.base_image,
                                                modules=modules_str,
                                                packages=install_packages_str,
                                                rpm_qf_args=rpm_qf_args()))

        self.workflow.builder.set_df_path(df_path)

        includepkgs = builder.get_includepkgs()
        includepkgs_path = os.path.join(self.workflow.builder.df_dir, 'atomic-reactor-includepkgs')
        with open(includepkgs_path, 'w') as f:
            f.write('includepkgs = ' + ','.join(includepkgs) + '\n')

        # Create the cleanup script

        cleanupscript = os.path.join(self.workflow.builder.df_dir, "cleanup.sh")
        with open(cleanupscript, 'w') as f:
            f.write(builder.get_cleanup_script())
        os.chmod(cleanupscript, 0o0755)

        # Add a yum-repository pointing to the compose

        repo_name = 'atomic-reactor-module-{name}-{stream}-{version}'.format(
            name=module_info.name,
            stream=module_info.stream,
            version=module_info.version)

        compose_info = get_compose_info(self.workflow)

        repo = {
            'name': repo_name,
            'baseurl': compose_info.repo_url,
            'enabled': 1,
            'gpgcheck': 0,
        }

        path = YumRepo(os.path.join(YUM_REPOS_DIR, repo_name)).dst_filename
        self.workflow.files[path] = render_yum_repo(repo, escape_dollars=False)
