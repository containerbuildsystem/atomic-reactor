"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Writes a Dockerfile using information from container.yaml - the Dockerfile
results in an image with the actual filesystem tree we care about at
/var/tmp/flatpak-build. The Dockerfile will later be updated by the
flatpak_update_dockerfile plugin to have specifics from the composed module.
"""

from __future__ import absolute_import

import os

from osbs.repo_utils import ModuleSpec

from atomic_reactor.constants import DOCKERFILE_FILENAME, RELATIVE_REPOS_PATH, YUM_REPOS_DIR
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_reactor_config import get_flatpak_base_image
from atomic_reactor.utils.rpm import rpm_qf_args


# /var/tmp/flatpak-build is the final image we'll turn into a Flaptak
# In order for 'dnf module enable' to work correctly, we need an
# /etc/os-release in the install root with the correct PLATFORM_ID
# for our base package set. To make that work, we install system-release
# into a *different* install root and copy /etc/os-release over.
#
# We also have to redo the addition of yum repos from the "pre_inject_yum_repo"
# plugin after first removing any yum repos in the base image - we want
# /only/ the yum repos from atomic_reactor, and nothing else.
DOCKERFILE_TEMPLATE = '''FROM {base_image}

LABEL name="{name}"
LABEL com.redhat.component="{component}"
LABEL version="{stream}"
LABEL release="@RELEASE@"

RUN rm -f {yum_repos_dir}*
ADD {relative_repos_path}* {yum_repos_dir}

ADD {includepkgs} /tmp/

RUN mkdir -p /var/tmp/flatpak-build/dev && \
    for i in null zero random urandom ; do cp -a /dev/$i /var/tmp/flatpak-build/dev ; done

RUN cat /tmp/atomic-reactor-includepkgs >> /etc/dnf/dnf.conf && \\
    INSTALLDIR=/var/tmp/flatpak-build && \\
    DNF='\\
    dnf -y --nogpgcheck \\
    ' && \\
    $DNF --installroot=$INSTALLDIR-init install system-release && \\
    mkdir -p $INSTALLDIR/etc/ && \\
    cp $INSTALLDIR-init/etc/os-release $INSTALLDIR/etc/os-release && \\
    $DNF --installroot=$INSTALLDIR module enable @ENABLE_MODULES@ && \\
    $DNF --installroot=$INSTALLDIR install @INSTALL_PACKAGES@
RUN rpm --root=/var/tmp/flatpak-build {rpm_qf_args} > /var/tmp/flatpak-build.rpm_qf
COPY {cleanupscript} /var/tmp/flatpak-build/tmp/
RUN chroot /var/tmp/flatpak-build/ /bin/sh /tmp/cleanup.sh
'''


FLATPAK_INCLUDEPKGS_FILENAME = 'atomic-reactor-includepkgs'
FLATPAK_CLEANUPSCRIPT_FILENAME = 'cleanup.sh'
WORKSPACE_SOURCE_SPEC_KEY = 'source_spec'


def get_flatpak_source_spec(workflow):
    key = FlatpakCreateDockerfilePlugin.key
    if key not in workflow.plugin_workspace:
        return None
    return workflow.plugin_workspace[key].get(WORKSPACE_SOURCE_SPEC_KEY, None)


def set_flatpak_source_spec(workflow, module_info):
    key = FlatpakCreateDockerfilePlugin.key

    workflow.plugin_workspace.setdefault(key, {})
    workspace = workflow.plugin_workspace[key]
    workspace[WORKSPACE_SOURCE_SPEC_KEY] = module_info


class FlatpakCreateDockerfilePlugin(PreBuildPlugin):
    key = "flatpak_create_dockerfile"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow,
                 base_image=None):
        """
        constructor

        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param base_image: host image used to install packages when creating the Flatpak
        """
        # call parent constructor
        super(FlatpakCreateDockerfilePlugin, self).__init__(tasker, workflow)

        self.default_base_image = get_flatpak_base_image(workflow, base_image)

    def _load_source_spec(self):
        # Find out the name:stream of the module we're building from (the version is
        # not known until ODCS resolves the module to a particular build)

        modules = self.workflow.source.config.compose.get('modules', [])

        if not modules:
            raise RuntimeError('"compose" config has no modules, a module is required for Flatpaks')

        source_spec = modules[0]
        if len(modules) > 1:
            self.log.info("compose config contains multiple modules,"
                          "using first module %s", source_spec)

        set_flatpak_source_spec(self.workflow, source_spec)

    def run(self):
        """
        run the plugin
        """

        self._load_source_spec()
        source_spec = get_flatpak_source_spec(self.workflow)
        module_info = ModuleSpec.from_str(source_spec)

        # Load additional information from the flatpak section

        flatpak_yaml = self.workflow.source.config.flatpak

        base_image = flatpak_yaml.get('base_image', self.default_base_image)
        name = flatpak_yaml.get('name', module_info.name)
        component = flatpak_yaml.get('component', module_info.name)

        # Create the dockerfile

        df_path = os.path.join(self.workflow.builder.df_dir, DOCKERFILE_FILENAME)
        with open(df_path, 'w') as fp:
            fp.write(DOCKERFILE_TEMPLATE.format(name=name,
                                                component=component,
                                                cleanupscript=FLATPAK_CLEANUPSCRIPT_FILENAME,
                                                includepkgs=FLATPAK_INCLUDEPKGS_FILENAME,
                                                stream=module_info.stream.replace('-', '_'),
                                                base_image=base_image,
                                                relative_repos_path=RELATIVE_REPOS_PATH,
                                                rpm_qf_args=rpm_qf_args(),
                                                yum_repos_dir=YUM_REPOS_DIR))

        self.workflow.builder.set_df_path(df_path)
