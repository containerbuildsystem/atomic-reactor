"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import pytest

from atomic_reactor.utils.rpm import rpm_qf_args, parse_rpm_output

FAKE_SIGMD5 = b'0' * 32
FAKE_SIGNATURE = "RSA/SHA256, Tue 30 Aug 2016 00:00:00, Key ID 01234567890abc"


@pytest.mark.parametrize(('tags', 'separator', 'expected'), [
    (None, None,
     r"-qa --qf '%{NAME};%{VERSION};%{RELEASE};%{ARCH};%{EPOCH};%{SIZE};%{SIGMD5};%{BUILDTIME};%{SIGPGP:pgpsig};%{SIGGPG:pgpsig};%{DSAHEADER:pgpsig};%{RSAHEADER:pgpsig}\n'"),  # noqa
    (['NAME', 'VERSION'], "|",
     r"-qa --qf '%{NAME}|%{VERSION}\n'"),
])
def test_rpm_qf_args(tags, separator, expected):
    kwargs = {}
    if tags is not None:
        kwargs['tags'] = tags
    if separator is not None:
        kwargs['separator'] = separator
    assert rpm_qf_args(**kwargs) == expected


def test_parse_rpm_output():

    def fake_rpm_line(
        name, version, release="1", pgp="(none)", gpg="(none)", dsa="(none)", rsa="(none)"
    ) -> str:
        """Create fake line of output in the rpm -qa --qf command."""
        arch = "x86_64"
        epoch = "0"
        size = "2000"
        sigmd5 = FAKE_SIGMD5.decode()
        buildtime = "23000"
        return (
            f"{name};{version};{release};{arch};{epoch};{size};{sigmd5};{buildtime}"
            f";{pgp};{gpg};{dsa};{rsa}"
        )

    res = parse_rpm_output([
        # header+payload signature only (doesn't happen, but let's test it anyway)
        fake_rpm_line("name1", "1.0", pgp=FAKE_SIGNATURE),
        fake_rpm_line("name2", "2.0", gpg=FAKE_SIGNATURE),
        # header+payload AND header-only signature (the usual, backwards-compatible case)
        fake_rpm_line("name3", "3.0", pgp=FAKE_SIGNATURE, rsa=FAKE_SIGNATURE),
        fake_rpm_line("name4", "4.0", gpg=FAKE_SIGNATURE, dsa=FAKE_SIGNATURE),
        # header-only signature only (the new, more efficient case)
        fake_rpm_line("name5", "5.0", dsa=FAKE_SIGNATURE),
        fake_rpm_line("name6", "6.0", rsa=FAKE_SIGNATURE),
        # gpg-pubkey RPMs should be omitted from the results
        fake_rpm_line("gpg-pubkey", "64dab85d", "57d33e22")
    ])

    default_values = {
        'type': 'rpm',
        'release': '1',
        'arch': 'x86_64',
        'epoch': 0,
        'sigmd5': FAKE_SIGMD5.decode(),
        'signature': "01234567890abc",
    }
    assert res == [
        {
            'name': 'name1',
            'version': '1.0',
            **default_values,
        },
        {
            'name': 'name2',
            'version': '2.0',
            **default_values,
        },
        {
            'name': 'name3',
            'version': '3.0',
            **default_values,
        },
        {
            'name': 'name4',
            'version': '4.0',
            **default_values,
        },
        {
            'name': 'name5',
            'version': '5.0',
            **default_values,
        },
        {
            'name': 'name6',
            'version': '6.0',
            **default_values,
        },
    ]

    # Tests with different fields and separator
    res = parse_rpm_output(["1|1.0|name1"],
                           tags=['RELEASE', 'VERSION', 'NAME'],
                           separator="|")

    assert res == [
        {
            'type': 'rpm',
            'name': 'name1',
            'version': '1.0',
            'release': '1',
            'arch': None,
            'epoch': None,
            'sigmd5': None,
            'signature': None,
        }
    ]
