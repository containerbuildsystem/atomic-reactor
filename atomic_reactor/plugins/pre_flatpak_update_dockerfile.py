"""
Copyright (c) 2017-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Updates the Dockerfile created by pre_flatpak_create_dockerfile with
information from a module compose created by pre_resolve_composes.
"""
import functools
import os

from flatpak_module_tools.flatpak_builder import FlatpakBuilder

from atomic_reactor.constants import PLUGIN_RESOLVE_COMPOSES_KEY
from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_flatpak_create_dockerfile import (FLATPAK_INCLUDEPKGS_FILENAME,
                                                                  FLATPAK_CLEANUPSCRIPT_FILENAME)
from atomic_reactor.util import is_flatpak_build, map_to_user_params
from atomic_reactor.utils.flatpak_util import FlatpakUtil


class FlatpakUpdateDockerfilePlugin(PreBuildPlugin):
    key = "flatpak_update_dockerfile"
    is_allowed_to_fail = False

    args_from_user_params = map_to_user_params("compose_ids")

    def __init__(self, workflow):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(FlatpakUpdateDockerfilePlugin, self).__init__(workflow)

    def update_dockerfile(self, builder, compose_info, build_dir: BuildDir):
        # Update the dockerfile

        # We need to enable all the modules other than the platform pseudo-module
        enable_modules_str = ' '.join(builder.get_enable_modules())

        install_packages_str = ' '.join(builder.get_install_packages())

        replacements = {
            '@ENABLE_MODULES@': enable_modules_str,
            '@INSTALL_PACKAGES@': install_packages_str,
            '@RELEASE@': compose_info.main_module.version,
        }

        dockerfile = build_dir.dockerfile
        content = dockerfile.content

        # Perform the substitutions; simple approach - should be efficient enough
        for old, new in replacements.items():
            content = content.replace(old, new)

        dockerfile.content = content

    def create_includepkgs_file_and_cleanupscript(self, builder, build_dir: BuildDir):
        # Create a file describing which packages from the base yum repositories are included
        includepkgs = builder.get_includepkgs()
        includepkgs_path = build_dir.path / FLATPAK_INCLUDEPKGS_FILENAME
        with open(includepkgs_path, 'w') as f:
            f.write('includepkgs = ' + ','.join(includepkgs) + '\n')

        # Create the cleanup script
        cleanupscript = build_dir.path / FLATPAK_CLEANUPSCRIPT_FILENAME
        with open(cleanupscript, 'w') as f:
            f.write(builder.get_cleanup_script())
        os.chmod(cleanupscript, 0o0500)
        return [includepkgs_path, cleanupscript]

    def run(self):
        """
        run the plugin
        """
        if not is_flatpak_build(self.workflow):
            self.log.info('not flatpak build, skipping plugin')
            return

        resolve_comp_result = self.workflow.data.prebuild_results.get(PLUGIN_RESOLVE_COMPOSES_KEY)
        flatpak_util = FlatpakUtil(workflow_config=self.workflow.conf,
                                   source_config=self.workflow.source.config,
                                   composes=resolve_comp_result['composes'])

        compose_info = flatpak_util.get_flatpak_compose_info()
        source = flatpak_util.get_flatpak_source_info()

        builder = FlatpakBuilder(source, None, None)

        builder.precheck()

        flatpak_update = functools.partial(self.update_dockerfile, builder, compose_info)
        self.workflow.build_dir.for_each_platform(flatpak_update)

        create_files = functools.partial(self.create_includepkgs_file_and_cleanupscript,
                                         builder)
        self.workflow.build_dir.for_all_platforms_copy(create_files)
