"""
Copyright (c) 2016, 2017, 2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import functools

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.constants import INSPECT_CONFIG
from atomic_reactor.util import df_parser, LabelFormatter
from osbs.utils import Labels, ImageName


class TagFromConfigPlugin(PostBuildPlugin):
    """Computes tags to be applied to the built image.

    The tags are saved in the tag configuration object in the build workflow. They are later
    applied by the tag_and_push and push_floating_tags plugins.
    """

    key = 'tag_from_config'
    is_allowed_to_fail = False

    def __init__(self, workflow):
        super(TagFromConfigPlugin, self).__init__(workflow)
        self.labels = None

    @functools.cached_property
    def tag_suffixes(self):
        user_params = self.workflow.user_params

        unique_tag = user_params["image_tag"].split(":")[-1]
        tag_suffixes = {"unique": [unique_tag], "primary": [], "floating": []}

        if self.is_in_orchestrator():
            additional_tags = user_params.get("additional_tags", [])

            if user_params.get("scratch"):
                pass
            elif user_params.get("isolated"):
                tag_suffixes["primary"].extend(["{version}-{release}"])
            elif user_params.get("tags_from_yaml"):
                tag_suffixes["primary"].extend(["{version}-{release}"])
                tag_suffixes["floating"].extend(additional_tags)
            else:
                tag_suffixes["primary"].extend(["{version}-{release}"])
                tag_suffixes["floating"].extend(["latest", "{version}"])
                tag_suffixes["floating"].extend(additional_tags)

        return tag_suffixes

    def parse_and_add_tags(self):
        tags = []
        name = self.get_component_name()
        floating_defined = 'floating' in self.tag_suffixes

        for tag_suffix in self.tag_suffixes.get('unique', []):
            tag = '{}:{}'.format(name, tag_suffix)
            if tag not in tags:
                tags.append(tag)
                self.log.debug('Using additional unique tag %s', tag)
                self.workflow.tag_conf.add_unique_image(tag)

        for tag_suffix in self.tag_suffixes.get('floating', []):
            p_suffix = LabelFormatter().vformat(tag_suffix, [], self.labels)
            p_tag = '{}:{}'.format(name, p_suffix)
            if p_tag not in tags:
                tags.append(p_tag)
                self.log.debug('Using additional floating tag %s', p_tag)
                self.workflow.tag_conf.add_floating_image(p_tag)

        for tag_suffix in self.tag_suffixes.get('primary', []):
            p_suffix = LabelFormatter().vformat(tag_suffix, [], self.labels)
            p_tag = '{}:{}'.format(name, p_suffix)
            if p_tag not in tags:
                add_primary = True
                if not floating_defined and '-' not in p_suffix:
                    add_primary = False

                tags.append(p_tag)
                if add_primary:
                    self.log.debug('Using additional primary tag %s', p_tag)
                    self.workflow.tag_conf.add_primary_image(p_tag)
                else:
                    self.log.debug('Using additional floating tag %s', p_tag)
                    self.workflow.tag_conf.add_floating_image(p_tag)

        return tags

    def get_component_name(self):
        try:
            labels = Labels(self.labels)
            _, name = labels.get_name_and_value(Labels.LABEL_TYPE_NAME)
        except KeyError:
            self.log.error('Unable to determine component from "Labels"')
            raise

        organization = self.workflow.conf.registries_organization
        if organization:
            image = ImageName.parse(name)
            image.enclose(organization)
            name = image.get_repo()

        return name

    def run(self):
        self.lookup_labels()
        return self.parse_and_add_tags()

    def lookup_labels(self):
        if self.workflow.build_result.is_image_available():
            if not self.workflow.built_image_inspect:
                raise RuntimeError('There is no inspect data for built image. '
                                   'Has the build succeeded?')
            try:
                self.labels = self.workflow.built_image_inspect[INSPECT_CONFIG]['Labels']
            except (TypeError, KeyError):
                self.log.error('Unable to determine "Labels" from built image')
                raise
        else:
            self.labels = df_parser(self.workflow.df_path, workflow=self.workflow,
                                    env_replace=True).labels
