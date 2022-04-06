"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import logging
from typing import Any, Dict, Optional

import gi
from flatpak_module_tools.flatpak_builder import ModuleInfo, FlatpakSourceInfo
from osbs.repo_utils import ModuleSpec

from atomic_reactor.config import get_koji_session, Configuration
from atomic_reactor.source import SourceConfig
from atomic_reactor.utils.koji import get_koji_module_build

try:
    gi.require_version('Modulemd', '2.0')
except ValueError as e:
    # Normalize to ImportError to simplify handling
    raise ImportError(str(e)) from e
from gi.repository import Modulemd

logger = logging.getLogger(__name__)

# ODCS API constant
SOURCE_TYPE_MODULE = 2


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


class FlatpakUtil:
    def __init__(self, workflow_config: Configuration, source_config: SourceConfig,
                 composes=Optional[Dict[str, Any]]):
        self.workflow_config = workflow_config
        self.source_config = source_config
        self.composes = composes

    def get_flatpak_source_spec(self) -> str:
        modules = self.source_config.compose.get('modules', [])

        if not modules:
            raise RuntimeError('"compose" config has no modules, a module is required for Flatpaks')

        source_spec = modules[0]
        if len(modules) > 1:
            logger.info("compose config contains multiple modules, using first module %s",
                        source_spec)

        return source_spec

    def resolve_modules(self, modules) -> Dict[str, ModuleInfo]:
        koji_session = get_koji_session(self.workflow_config)

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

    def build_compose_info(self, modules, source_spec) -> ComposeInfo:
        main_module = ModuleSpec.from_str(source_spec)

        main_module_info = modules[main_module.name]
        assert main_module_info.stream == main_module.stream
        if main_module.version is not None:
            assert main_module_info.version == main_module.version

        return ComposeInfo(source_spec=source_spec,
                           main_module=main_module_info,
                           modules=modules)

    def get_flatpak_compose_info(self) -> ComposeInfo:
        source_spec = self.get_flatpak_source_spec()
        main_module = ModuleSpec.from_str(source_spec)

        for compose_info in self.composes:
            if compose_info['source_type'] != SOURCE_TYPE_MODULE:
                continue

            modules = [ModuleSpec.from_str(s) for s in compose_info['source'].split()]
            for module in modules:
                if module.name == main_module.name and module.stream == main_module.stream:
                    resolved_modules = self.resolve_modules(modules)
                    return self.build_compose_info(resolved_modules, source_spec)

        logger.debug('Compose info: %s', self.composes)
        raise RuntimeError("Can't find main module %s in compose result" % source_spec)

    def get_flatpak_source_info(self) -> FlatpakSourceInfo:
        flatpak_yaml = self.source_config.flatpak
        compose_info = self.get_flatpak_compose_info()

        module_spec = ModuleSpec.from_str(compose_info.source_spec)

        source_info = FlatpakSourceInfo(flatpak_yaml,
                                        compose_info.modules,
                                        compose_info.main_module,
                                        module_spec.profile)
        return source_info
