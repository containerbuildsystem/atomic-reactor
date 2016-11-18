"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, unicode_literals

from atomic_reactor.constants import INSPECT_CONFIG
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import df_parser


class NothingToCheck(Exception):
    pass


class DisallowedDistributionScope(Exception):
    pass


class DistributionScopePlugin(PreBuildPlugin):
    """Apply distribution-scope rule.

    Possible values for this label are defined here:
    https://github.com/projectatomic/ContainerApplicationGenericLabels

    They are:

    (most restrictive)
    - private
    - authoritative-source-only
    - restricted
    - public
    (least restrictive)

    The rule we want to apply is to prevent images from having a
    less restrictive scope than their parent images.

    If the distribution-scope for this image is set to a less
    restrictive value than the distribution-scope label inherited from
    the parent image, fail the build.
    """

    # Name of this plugin
    key = 'distribution_scope'

    # Exceptions from this plugin should fail the build
    is_allowed_to_fail = False

    # The label and its possible values.

    # LABEL name used for specifying scope
    SCOPE_LABEL = 'distribution-scope'

    # Valid distribution-scope choice values, most restrictive first
    SCOPE_NAME = [
        "private",
        "authoritative-source-only",
        "restricted",
        "public",
    ]

    def get_scope(self, which, labels):
        """
        :param which: str, description of the image this belongs to
        :param labels: dict, labels on the image
        """

        try:
            scope_choice = labels[self.SCOPE_LABEL]
        except (KeyError, TypeError):
            self.log.debug("no distribution scope set for %s image", which)
            raise NothingToCheck

        try:
            scope = self.SCOPE_NAME.index(scope_choice)
        except ValueError:
            self.log.warning("invalid label %s=%s for %s image",
                             self.SCOPE_LABEL, scope_choice, which)
            raise NothingToCheck

        return scope

    def run(self):
        try:
            # Find out the intended scope for this image
            labels = df_parser(self.workflow.builder.df_path, workflow=self.workflow).labels
            scope = self.get_scope('current', labels)

            # Find out the parent's intended scope
            parent_labels = self.workflow.base_image_inspect[INSPECT_CONFIG]['Labels']
            parent_scope = self.get_scope('parent', parent_labels)
        except NothingToCheck:
            self.log.debug("no checks performed")
            return

        if scope > parent_scope:
            error = ("{label}={scope} but parent has {label}={parent_scope}"
                     .format(label=self.SCOPE_LABEL,
                             scope=self.SCOPE_NAME[scope],
                             parent_scope=self.SCOPE_NAME[parent_scope]))
            self.log.error("%s", error)
            raise DisallowedDistributionScope(error)

        self.log.info("distribution scope checked")
