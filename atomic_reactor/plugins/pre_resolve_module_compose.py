"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Takes a reference to a module, and looks up or triggers a compose in the on-demand compose
server (ODCS). In addition to retrieving the URL for a composed yum repository, the module
and all its dependencies are resolved to particular versions matching the ones that the
repository is built from.


Example configuration:
{
    'name': 'resolve_module_compose',
    'args': {'module_name': 'myapp',
             'module_stream': 'f26',
             'module_version': '20170629185228'}
}
"""


from __future__ import absolute_import

from flatpak_module_tools.flatpak_builder import ModuleInfo

import gi
try:
    gi.require_version('Modulemd', '1.0')
except ValueError as e:
    # Normalize to ImportError to simplify handling
    raise ImportError(str(e))
from gi.repository import Modulemd
from osbs.repo_utils import ModuleSpec

from atomic_reactor.koji_util import get_koji_module_build
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import get_platforms
from atomic_reactor.plugins.build_orchestrate_build import override_build_kwarg
from atomic_reactor.plugins.pre_reactor_config import (get_config,
                                                       get_koji_session, get_odcs_session,
                                                       get_odcs, NO_FALLBACK)


class ComposeInfo(object):
    def __init__(self, source_spec, compose_id, base_module, modules, repo_url,
                 signing_intent, signing_intent_overridden):
        self.source_spec = source_spec
        self.compose_id = compose_id
        self.base_module = base_module
        self.modules = modules
        self.repo_url = repo_url
        self.signing_intent = signing_intent
        self.signing_intent_overridden = signing_intent_overridden

    def koji_metadata(self):
        sorted_modules = [self.modules[k] for k in sorted(self.modules.keys())]

        # We exclude the 'platform' pseudo-module here since we don't enable
        # it for package installation - it doesn't influence the image contents
        return {
            'source_modules': [self.source_spec],
            'modules': ['-'.join((m.name, m.stream, m.version)) for
                        m in sorted_modules if m.name != 'platform'],
            'odcs': {
                'signing_intent': self.signing_intent,
                'signing_intent_overridden': self.signing_intent_overridden,
            }
        }


WORKSPACE_SOURCE_KEY = 'compose_info'


def get_compose_info(workflow):
    key = ResolveModuleComposePlugin.key
    if key not in workflow.plugin_workspace:
        return None
    return workflow.plugin_workspace[key].get(WORKSPACE_SOURCE_KEY, None)


def set_compose_info(workflow, source):
    key = ResolveModuleComposePlugin.key

    workflow.plugin_workspace.setdefault(key, {})
    workspace = workflow.plugin_workspace[key]
    workspace[WORKSPACE_SOURCE_KEY] = source


class ResolveModuleComposePlugin(PreBuildPlugin):
    key = "resolve_module_compose"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow,
                 compose_ids=tuple(),
                 odcs_url=None, odcs_insecure=False,
                 odcs_openidc_secret_path=None,
                 signing_intent=None,
                 pdc_url=None, pdc_insecure=False):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param compose_ids: use the given compose_ids instead of requesting a new one
        :param odcs_url: URL of ODCS (On Demand Compose Service)
        :param odcs_insecure: If True, don't check SSL certificates for `odcs_url`
        :param odcs_openidc_secret_path: directory to look in for a `token` file (optional)
        :param signing_intent: override the signing intent from git repo configuration
        :param pdc_url: unused
        :param pdc_insecure: unused
        :
        """
        # call parent constructor
        super(ResolveModuleComposePlugin, self).__init__(tasker, workflow)

        self.signing_intent_name = signing_intent
        self.compose_ids = compose_ids
        self.compose_id = None

        self.odcs_fallback = {
            'api_url': odcs_url,
            'insecure': odcs_insecure,
            'auth': {
                'openidc_dir': odcs_openidc_secret_path
            }
        }
        if not get_odcs(self.workflow, self.odcs_fallback)['api_url']:
            raise RuntimeError("odcs_url is required")

        self.data = None

    def read_configs_general(self):
        self.data = self.workflow.source.config.compose

        if not self.data:
            raise RuntimeError('"compose" config in container.yaml is required for Flatpaks')

    def _resolve_modules(self, compose_source):
        koji_session = get_koji_session(self.workflow, fallback=NO_FALLBACK)

        resolved_modules = {}
        for module in compose_source.strip().split():
            module_spec = ModuleSpec.from_str(module)
            build, rpm_list = get_koji_module_build(koji_session, module_spec)

            # The returned RPM list contains source RPMs and RPMs for all
            # architectures.
            rpms = ['{name}-{epochnum}:{version}-{release}.{arch}.rpm'
                    .format(epochnum=rpm['epoch'] or 0, **rpm)
                    for rpm in rpm_list]

            objects = Modulemd.objects_from_string(
                build['extra']['typeinfo']['module']['modulemd_str'])
            assert len(objects) == 1
            mmd = objects[0]
            assert isinstance(mmd, Modulemd.Module)
            # Make sure we have a version 2 modulemd file
            mmd.upgrade()

            resolved_modules[module_spec.name] = ModuleInfo(module_spec.name,
                                                            module_spec.stream,
                                                            module_spec.version,
                                                            mmd, rpms)
        return resolved_modules

    def _resolve_compose(self):
        odcs_config = get_config(self.workflow).get_odcs_config()
        odcs_client = get_odcs_session(self.workflow, self.odcs_fallback)
        self.read_configs_general()

        modules = self.data.get('modules', [])

        if not modules:
            raise RuntimeError('"compose" config has no modules, a module is required for Flatpaks')

        source_spec = modules[0]
        if len(modules) > 1:
            self.log.info("compose config contains multiple modules,"
                          "using first module %s", source_spec)

        module = ModuleSpec.from_str(source_spec)
        self.log.info("Resolving module compose for name=%s, stream=%s, version=%s",
                      module.name, module.stream, module.version)

        noprofile_spec = module.to_str(include_profile=False)

        if self.compose_ids:
            if len(self.compose_ids) > 1:
                self.log.info("Multiple compose_ids, using first compose %d", self.compose_ids[0])
            self.compose_id = self.compose_ids[0]

        if self.signing_intent_name is not None:
            signing_intent_name = self.signing_intent_name
        else:
            signing_intent_name = self.data.get('signing_intent',
                                                odcs_config.default_signing_intent)
        signing_intent = odcs_config.get_signing_intent_by_name(signing_intent_name)

        if self.compose_id is None:
            arches = sorted(get_platforms(self.workflow))
            self.compose_id = odcs_client.start_compose(source_type='module',
                                                        source=noprofile_spec,
                                                        sigkeys=signing_intent['keys'],
                                                        arches=arches)['id']

        compose_info = odcs_client.wait_for_compose(self.compose_id)
        if compose_info['state_name'] != "done":
            raise RuntimeError("Compose cannot be retrieved, state='%s'" %
                               compose_info['state_name'])

        compose_source = compose_info['source']
        self.log.info("Resolved list of modules: %s", compose_source)

        resolved_modules = self._resolve_modules(compose_source)
        base_module = resolved_modules[module.name]
        assert base_module.stream == module.stream
        if module.version is not None:
            assert base_module.version == module.version

        return ComposeInfo(source_spec=source_spec,
                           compose_id=self.compose_id,
                           base_module=base_module,
                           modules=resolved_modules,
                           repo_url=compose_info['result_repo'] + '/$basearch/os/',
                           signing_intent=signing_intent_name,
                           signing_intent_overridden=self.signing_intent_name is not None)

    def run(self):
        """
        run the plugin
        """

        self.log.info("Resolving module compose")

        compose_info = self._resolve_compose()
        set_compose_info(self.workflow, compose_info)
        override_build_kwarg(self.workflow, 'compose_ids', [compose_info.compose_id])
