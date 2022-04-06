"""
Copyright (c) 2017-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Writes a Dockerfile using information from container.yaml - the Dockerfile
results in an image with the actual filesystem tree we care about at
/var/tmp/flatpak-build. The Dockerfile will later be updated by the
flatpak_update_dockerfile plugin to have specifics from the composed module.
"""

from pathlib import Path
from typing import List

from atomic_reactor.dirs import BuildDir
from osbs.repo_utils import ModuleSpec

from atomic_reactor.constants import RELATIVE_REPOS_PATH, YUM_REPOS_DIR
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.utils.flatpak_util import FlatpakUtil
from atomic_reactor.utils.rpm import rpm_qf_args
from atomic_reactor.util import is_flatpak_build


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


class FlatpakCreateDockerfilePlugin(PreBuildPlugin):
    key = "flatpak_create_dockerfile"
    is_allowed_to_fail = False

    def __init__(self, workflow):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(FlatpakCreateDockerfilePlugin, self).__init__(workflow)

        self.default_base_image = self.workflow.conf.flatpak_base_image

    def run(self):
        """
        run the plugin
        """
        if not is_flatpak_build(self.workflow):
            self.log.info('not flatpak build, skipping plugin')
            return

        flatpak_util = FlatpakUtil(workflow_config=self.workflow.conf,
                                   source_config=self.workflow.source.config,
                                   composes=None)
        source_spec = flatpak_util.get_flatpak_source_spec()
        module_info = ModuleSpec.from_str(source_spec)

        # Load additional information from the flatpak section

        flatpak_yaml = self.workflow.source.config.flatpak

        base_image = flatpak_yaml.get('base_image', self.default_base_image)
        name = flatpak_yaml.get('name', module_info.name)
        component = flatpak_yaml.get('component', module_info.name)

        # Create the dockerfile

        def _create_dockerfile(build_dir: BuildDir) -> List[Path]:
            content = DOCKERFILE_TEMPLATE.format(name=name,
                                                 component=component,
                                                 cleanupscript=FLATPAK_CLEANUPSCRIPT_FILENAME,
                                                 includepkgs=FLATPAK_INCLUDEPKGS_FILENAME,
                                                 stream=module_info.stream.replace('-', '_'),
                                                 base_image=base_image,
                                                 relative_repos_path=RELATIVE_REPOS_PATH,
                                                 rpm_qf_args=rpm_qf_args(),
                                                 yum_repos_dir=YUM_REPOS_DIR)
            build_dir.dockerfile_path.write_text(content, "utf-8")
            return [build_dir.dockerfile_path]

        created_files = self.workflow.build_dir.for_all_platforms_copy(_create_dockerfile)

        dockerfile_path = created_files[0]
        self.workflow.reset_dockerfile_images(str(dockerfile_path))
