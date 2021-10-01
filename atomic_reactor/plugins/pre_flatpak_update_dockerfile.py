"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Updates the Dockerfile created by pre_flatpak_create_dockerfile with
information from a module compose created by pre_resolve_composes.

When this plugin runs in a worker build, the composes from pre_resolve_composes
are passed in via the compose_ids parameter, and looked up again in ODCS.
"""

import os

from flatpak_module_tools.flatpak_builder import FlatpakSourceInfo, FlatpakBuilder, ModuleInfo

import gi
try:
    gi.require_version('Modulemd', '2.0')
except ValueError as e:
    # Normalize to ImportError to simplify handling
    raise ImportError(str(e)) from e
from gi.repository import Modulemd

from osbs.repo_utils import ModuleSpec

from atomic_reactor.constants import PLUGIN_RESOLVE_COMPOSES_KEY
from atomic_reactor.config import get_koji_session, get_odcs_session
from atomic_reactor.utils.koji import get_koji_module_build
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_flatpak_create_dockerfile import (FLATPAK_INCLUDEPKGS_FILENAME,
                                                                  FLATPAK_CLEANUPSCRIPT_FILENAME,
                                                                  get_flatpak_source_spec)
from atomic_reactor.util import df_parser, is_flatpak_build


# ODCS API constant
SOURCE_TYPE_MODULE = 2

WORKSPACE_SOURCE_KEY = 'source_info'
WORKSPACE_COMPOSE_KEY = 'compose_info'


class ComposeInfo(object):
    def __init__(self, source_spec, main_module, modules):
        self.source_spec = source_spec
        self.main_module = main_module
        self.modules = modules

    def koji_metadata(self):
        sorted_modules = [self.modules[k] for k in sorted(self.modules.keys())]

        # We exclude the 'platform' pseudo-module here since we don't enable
        # it for package installation - it doesn't influence the image contents
        return {
            'source_modules': [self.source_spec],
            'modules': ['-'.join((m.name, m.stream, m.version)) for
                        m in sorted_modules if m.name != 'platform'],
        }


def get_flatpak_source_info(workflow):
    key = FlatpakUpdateDockerfilePlugin.key
    if key not in workflow.plugin_workspace:
        return None
    return workflow.plugin_workspace[key].get(WORKSPACE_SOURCE_KEY, None)


def set_flatpak_source_info(workflow, source):
    key = FlatpakUpdateDockerfilePlugin.key

    workflow.plugin_workspace.setdefault(key, {})
    workspace = workflow.plugin_workspace[key]
    workspace[WORKSPACE_SOURCE_KEY] = source


def get_flatpak_compose_info(workflow):
    key = FlatpakUpdateDockerfilePlugin.key
    if key not in workflow.plugin_workspace:
        return None
    return workflow.plugin_workspace[key].get(WORKSPACE_COMPOSE_KEY, None)


def set_flatpak_compose_info(workflow, source):
    key = FlatpakUpdateDockerfilePlugin.key

    workflow.plugin_workspace.setdefault(key, {})
    workspace = workflow.plugin_workspace[key]
    workspace[WORKSPACE_COMPOSE_KEY] = source


class FlatpakUpdateDockerfilePlugin(PreBuildPlugin):
    key = "flatpak_update_dockerfile"
    is_allowed_to_fail = False

    def __init__(self, workflow, compose_ids=tuple()):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param compose_ids: compose_ids forwarded from the orchestrator build
        """
        # call parent constructor
        super(FlatpakUpdateDockerfilePlugin, self).__init__(workflow)

        self.compose_ids = compose_ids

    def _load_composes(self):
        odcs_client = get_odcs_session(self.workflow.conf)
        self.log.info(odcs_client)

        composes = []
        for compose_id in self.compose_ids:
            composes.append(odcs_client.wait_for_compose(compose_id))

        return composes

    def _resolve_modules(self, modules):
        koji_session = get_koji_session(self.workflow.conf)

        resolved_modules = {}
        for module_spec in modules:
            build, rpm_list = get_koji_module_build(koji_session, module_spec)

            # The returned RPM list contains source RPMs and RPMs for all
            # architectures.
            rpms = ['{name}-{epochnum}:{version}-{release}.{arch}.rpm'
                    .format(epochnum=rpm['epoch'] or 0, **rpm)
                    for rpm in rpm_list]

            # strict=False - don't break if new fields are added
            mmd = Modulemd.ModuleStream.read_string(
                build['extra']['typeinfo']['module']['modulemd_str'], strict=False)
            # Make sure we have a version 2 modulemd file
            mmd = mmd.upgrade(Modulemd.ModuleStreamVersionEnum.TWO)

            resolved_modules[module_spec.name] = ModuleInfo(module_spec.name,
                                                            module_spec.stream,
                                                            module_spec.version,
                                                            mmd, rpms)
        return resolved_modules

    def _build_compose_info(self, modules):
        source_spec = get_flatpak_source_spec(self.workflow)
        assert source_spec is not None  # flatpak_create_dockerfile must be run first
        main_module = ModuleSpec.from_str(source_spec)

        resolved_modules = self._resolve_modules(modules)

        main_module_info = resolved_modules[main_module.name]
        assert main_module_info.stream == main_module.stream
        if main_module.version is not None:
            assert main_module_info.version == main_module.version

        return ComposeInfo(source_spec=source_spec,
                           main_module=main_module_info,
                           modules=resolved_modules)

    def _load_compose_info(self):
        source_spec = get_flatpak_source_spec(self.workflow)
        assert source_spec is not None  # flatpak_create_dockerfile must be run first
        main_module = ModuleSpec.from_str(source_spec)

        resolve_comp_result = self.workflow.prebuild_results.get(PLUGIN_RESOLVE_COMPOSES_KEY)
        if resolve_comp_result:
            # In the orchestrator, we can get the compose info directly from
            # the resolve_composes plugin
            composes = resolve_comp_result['composes']
        else:
            # But in a worker, resolve_composes doesn't run, so we have
            # to load the compose info ourselves
            assert self.compose_ids

            composes = self._load_composes()

        for compose_info in composes:
            if compose_info['source_type'] != SOURCE_TYPE_MODULE:
                continue

            modules = [ModuleSpec.from_str(s) for s in compose_info['source'].split()]
            for module in modules:
                if module.name == main_module.name and module.stream == main_module.stream:
                    set_flatpak_compose_info(self.workflow, self._build_compose_info(modules))
                    return

        self.log.debug('Compose info: %s', composes)
        raise RuntimeError("Can't find main module %s in compose result" % source_spec)

    def _load_source(self):
        flatpak_yaml = self.workflow.source.config.flatpak

        compose_info = get_flatpak_compose_info(self.workflow)

        module_spec = ModuleSpec.from_str(compose_info.source_spec)

        source_info = FlatpakSourceInfo(flatpak_yaml,
                                        compose_info.modules,
                                        compose_info.main_module,
                                        module_spec.profile)
        set_flatpak_source_info(self.workflow, source_info)

    def run(self):
        """
        run the plugin
        """
        if not is_flatpak_build(self.workflow):
            self.log.info('not flatpak build, skipping plugin')
            return

        self._load_compose_info()
        compose_info = get_flatpak_compose_info(self.workflow)

        self._load_source()
        source = get_flatpak_source_info(self.workflow)

        builder = FlatpakBuilder(source, None, None)

        builder.precheck()

        # Update the dockerfile

        # We need to enable all the modules other than the platform pseudo-module
        enable_modules_str = ' '.join(builder.get_enable_modules())

        install_packages_str = ' '.join(builder.get_install_packages())

        replacements = {
            '@ENABLE_MODULES@': enable_modules_str,
            '@INSTALL_PACKAGES@': install_packages_str,
            '@RELEASE@': compose_info.main_module.version,
        }

        dockerfile = df_parser(self.workflow.df_path, workflow=self.workflow)
        content = dockerfile.content

        # Perform the substitutions; simple approach - should be efficient enough
        for old, new in replacements.items():
            content = content.replace(old, new)

        dockerfile.content = content

        # Create a file describing which packages from the base yum repositories are included

        includepkgs = builder.get_includepkgs()
        includepkgs_path = os.path.join(self.workflow.df_dir,
                                        FLATPAK_INCLUDEPKGS_FILENAME)
        with open(includepkgs_path, 'w') as f:
            f.write('includepkgs = ' + ','.join(includepkgs) + '\n')

        # Create the cleanup script

        cleanupscript = os.path.join(self.workflow.df_dir,
                                     FLATPAK_CLEANUPSCRIPT_FILENAME)
        with open(cleanupscript, 'w') as f:
            f.write(builder.get_cleanup_script())
        os.chmod(cleanupscript, 0o0500)
