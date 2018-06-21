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
             'module_version': '20170629185228',
             'odcs_url': 'https://odcs.fedoraproject.org/odcs/1'},
             'pdc_url': 'https://pdc.fedoraproject.org/rest_api/v1',}
}
"""

import gi
try:
    gi.require_version('Modulemd', '1.0')
except ValueError as e:
    # Normalize to ImportError to simplify handling
    raise ImportError(str(e))
from gi.repository import Modulemd

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import split_module_spec
from atomic_reactor.plugins.pre_reactor_config import (get_pdc_session, get_odcs_session,
                                                       get_pdc, get_odcs)


class ModuleInfo(object):
    def __init__(self, name, stream, version, mmd, rpms):
        self.name = name
        self.stream = stream
        self.version = version
        self.mmd = mmd
        self.rpms = rpms


class ComposeInfo(object):
    def __init__(self, source_spec, compose_id, base_module, modules, repo_url):
        self.source_spec = source_spec
        self.compose_id = compose_id
        self.base_module = base_module
        self.modules = modules
        self.repo_url = repo_url

    def koji_metadata(self):
        sorted_modules = [self.modules[k] for k in sorted(self.modules.keys())]

        return {
            'source_modules': [self.source_spec],
            'modules': ['-'.join((m.name, m.stream, m.version)) for
                        m in sorted_modules]
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
                 pdc_url=None, pdc_insecure=False):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param compose_ids: use the given compose_ids instead of requesting a new one
        :param odcs_url: URL of ODCS (On Demand Compose Service)
        :param odcs_insecure: If True, don't check SSL certificates for `odcs_url`
        :param odcs_openidc_secret_path: directory to look in for a `token` file (optional)
        :param pdc_url: URL of PDC (Product Definition center))
        :param pdc_insecure: If True, don't check SSL certificates for `pdc_url`
        :
        """
        # call parent constructor
        super(ResolveModuleComposePlugin, self).__init__(tasker, workflow)

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

        self.pdc_fallback = {
            'api_url': pdc_url,
            'insecure': pdc_insecure
        }
        if not get_pdc(self.workflow, self.pdc_fallback)['api_url']:
            raise RuntimeError("pdc_url is required")
        self.data = None

    def read_configs_general(self):
        self.data = self.workflow.source.config.compose

        if not self.data:
            raise RuntimeError('"compose" config in container.yaml is required for Flatpaks')

    def _resolve_modules(self, compose_source):
        resolved_modules = {}
        # The effect of develop=True is that requests to the PDC are made without authentication;
        # since we our interaction with the PDC is read-only, this is fine for our needs and
        # makes things simpler.
        pdc_client = get_pdc_session(self.workflow, self.pdc_fallback)

        for module_spec in compose_source.strip().split():
            try:
                module = split_module_spec(module_spec)
                if not module.version:
                    raise RuntimeError
            except RuntimeError:
                raise RuntimeError("Cannot parse resolved module in compose: %s" % module_spec)

            query = {
                'variant_id': module.name,
                'variant_version': module.stream,
                'variant_release': module.version,
                'active': True,
            }

            self.log.info("Looking up module metadata for '%s' in the PDC", module_spec)
            retval = pdc_client['unreleasedvariants/'](page_size=-1,
                                                       fields=['modulemd', 'rpms'], **query)
            # Error handling
            if not retval:
                raise RuntimeError("Failed to find module in PDC %r" % query)
            if len(retval) != 1:
                raise RuntimeError("Multiple modules in the PDC matched %r" % query)

            objects = Modulemd.objects_from_string(retval[0]['modulemd'])
            assert len(objects) == 1
            mmd = objects[0]
            assert isinstance(mmd, Modulemd.Module)
            # Make sure we have a version 2 modulemd file
            mmd.upgrade()
            rpms = set(retval[0]['rpms'])

            resolved_modules[module.name] = ModuleInfo(module.name, module.stream, module.version,
                                                       mmd, rpms)
        return resolved_modules

    def _resolve_compose(self):
        odcs_client = get_odcs_session(self.workflow, self.odcs_fallback)
        self.read_configs_general()

        modules = self.data.get('modules', [])

        if not modules:
            raise RuntimeError('"compose" config has no modules, a module is required for Flatpaks')

        source_spec = modules[0]
        if len(modules) > 1:
            self.log.info("compose config contains multiple modules,"
                          "using first module %s", source_spec)

        module = split_module_spec(source_spec)
        self.log.info("Resolving module compose for name=%s, stream=%s, version=%s",
                      module.name, module.stream, module.version)

        noprofile_spec = module.to_str(include_profile=False)

        if self.compose_ids:
            if len(self.compose_ids) > 1:
                self.log.info("Multiple compose_ids, using first compose %d", self.compose_ids[0])
            self.compose_id = self.compose_ids[0]

        if self.compose_id is None:
            self.compose_id = odcs_client.start_compose(source_type='module',
                                                        source=noprofile_spec)['id']

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
                           repo_url=compose_info['result_repo'] + '/$basearch/os/')

    def run(self):
        """
        run the plugin
        """

        self.log.info("Resolving module compose")

        compose_info = self._resolve_compose()
        set_compose_info(self.workflow, compose_info)
