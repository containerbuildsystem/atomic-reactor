"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

image_component_rpm_tags = [
    'NAME',
    'VERSION',
    'RELEASE',
    'ARCH',
    'EPOCH',
    'SIZE',
    'SIGMD5',
    'BUILDTIME',
    'SIGPGP:pgpsig',
    'SIGGPG:pgpsig',
]


def rpm_qf_args(tags=None, separator=';'):
    """
    Return the arguments to pass to rpm to list RPMs in the format expected
    by parse_rpm_output.
    """

    if tags is None:
        tags = image_component_rpm_tags

    fmt = separator.join(["%%{%s}" % tag for tag in tags])
    return r"-qa --qf '{0}\n'".format(fmt)


def parse_rpm_output(output, tags=None, separator=';'):
    """
    Parse output of the rpm query.

    :param output: list, decoded output (str) from the rpm subprocess
    :param tags: list, str fields used for query output
    :return: list, dicts describing each rpm package
    """

    if tags is None:
        tags = image_component_rpm_tags

    def field(tag):
        """
        Get a field value by name
        """
        try:
            value = fields[tags.index(tag)]
        except ValueError:
            return None

        if value == '(none)':
            return None

        return value

    components = []
    sigmarker = 'Key ID '
    for rpm in output:
        fields = rpm.rstrip('\n').split(separator)
        if len(fields) < len(tags):
            continue

        signature = field('SIGPGP:pgpsig') or field('SIGGPG:pgpsig')
        if signature:
            parts = signature.split(sigmarker, 1)
            if len(parts) > 1:
                signature = parts[1]

        component_rpm = {
            'type': 'rpm',
            'name': field('NAME'),
            'version': field('VERSION'),
            'release': field('RELEASE'),
            'arch': field('ARCH'),
            'sigmd5': field('SIGMD5'),
            'signature': signature,
        }

        # Special handling for epoch as it must be an integer or None
        epoch = field('EPOCH')
        if epoch is not None:
            epoch = int(epoch)

        component_rpm['epoch'] = epoch

        if component_rpm['name'] != 'gpg-pubkey':
            components.append(component_rpm)

    return components
