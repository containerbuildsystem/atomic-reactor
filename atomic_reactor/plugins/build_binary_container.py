"""
Copyright (c) 2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.plugin import BuildStepPlugin


class BinaryContainerPlugin(BuildStepPlugin):

    key = 'binary_container'

    def run(self):
        #raise RuntimeError('in the correct plugin')
        return {"not": "None"}
