import os
import smtplib

from dockerfile_parse import DockerfileParser
from flexmock import flexmock
import pytest
import requests
import six

from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.pre_check_and_set_rebuild import CheckAndSetRebuildPlugin
from atomic_reactor.plugins.exit_sendmail import SendMailPlugin
from atomic_reactor.source import GitSource
from atomic_reactor.util import ImageName

MS, MF = SendMailPlugin.MANUAL_SUCCESS, SendMailPlugin.MANUAL_FAIL
AS, AF = SendMailPlugin.AUTO_SUCCESS, SendMailPlugin.AUTO_FAIL
AC = SendMailPlugin.AUTO_CANCELED


class TestSendMailPlugin(object):
    def test_fails_with_unknown_states(self):
        p = SendMailPlugin(None, None, send_on=['unknown_state', MS])
        with pytest.raises(PluginFailedException) as e:
            p.run()
        assert str(e.value) == 'Unknown state(s) "unknown_state" for sendmail plugin'

    @pytest.mark.parametrize('rebuild, success, canceled, send_on, expected', [
        # make sure that right combinations only succeed for the specific state
        (False, True, False, [MS], True),
        (False, True, False, [MF, AS, AF, AC], False),
        (False, False, False, [MF], True),
        (False, False, False, [MS, AS, AF, AC], False),
        (True, True, False, [AS], True),
        (True, True, False, [MS, MF, AF, AC], False),
        (True, False, False, [AF], True),
        (True, False, False, [MS, MF, AS, AC], False),
        (True, False, True, [AC], True),
        # auto_fail would also give us True in this case
        (True, False, True, [MS, MF, AS], False),
        # also make sure that a random combination of more plugins works ok
        (True, False, False, [AF, MS], True)
    ])
    def test_should_send(self, rebuild, success, canceled, send_on, expected):
        p = SendMailPlugin(None, None, send_on=send_on)
        assert p._should_send(rebuild, success, canceled) == expected

    @pytest.mark.parametrize('autorebuild, submitter', [
        (True, 'John Smith <jsmith@foobar.com>'),
        (False, 'John Smith <jsmith@foobar.com>'),
        (True, None),
        (False, None),
    ])
    def test_render_mail(self, autorebuild, submitter):
        # just test a random combination of the method inputs and hope it's ok for other
        #   combinations
        class WF(object):
            image = ImageName.parse('foo/bar:baz')
            openshift_build_selflink = '/builds/blablabla'
        kwargs = {'url': 'https://something.com'}
        if submitter:
            kwargs['submitter'] = submitter
        p = SendMailPlugin(None, WF(), **kwargs)
        subject, body = p._render_mail(autorebuild, False, False)

        exp_subject = 'Image foo/bar:baz; Status failed; Submitted by '
        exp_body = [
            'Image: foo/bar:baz',
            'Status: failed',
            'Submitted by: ',
            'Logs: https://something.com/builds/blablabla/log'
        ]
        if autorebuild:
            exp_subject += '<autorebuild>'
            exp_body[2] += '<autorebuild>'
        elif submitter:
            exp_subject += submitter
            exp_body[2] += submitter
        else:
            exp_subject += 'unknown'
            exp_body[2] += 'unknown'

        assert subject == exp_subject
        assert body == '\n'.join(exp_body)

    def test_get_pdc_token(self, tmpdir):
        tokenfile = os.path.join(str(tmpdir), SendMailPlugin.PDC_TOKEN_FILE)
        p = SendMailPlugin(None, None, pdc_secret_path=str(tmpdir))
        with open(tokenfile, 'w') as f:
            f.write('thisistoken')
        assert p._get_pdc_token() == 'thisistoken'

    @pytest.mark.parametrize('df_labels, pdc_component_df_label, expected', [
        ({}, 'Foo', None),
        ({'Foo': 'Bar'}, 'Foo', 'Bar'),
    ])
    def test_get_component_label(self, df_labels, pdc_component_df_label, expected):
        class WF(object):
            class builder(object):
                df_path = '/foo/bar'
        p = SendMailPlugin(None, WF(), pdc_component_df_label=pdc_component_df_label)
        flexmock(DockerfileParser, labels=df_labels)
        if expected is None:
            with pytest.raises(PluginFailedException):
                p._get_component_label()
        else:
            assert p._get_component_label() == expected

    def test_get_receivers_list_raises_unless_GitSource(self):
        class WF(object):
            source = None
        p = SendMailPlugin(None, WF())
        flexmock(p).should_receive('_get_component_label').and_return('foo')

        with pytest.raises(PluginFailedException) as e:
            p._get_receivers_list()
        assert str(e.value) == 'Source is not of type "GitSource", panic!'

    @pytest.mark.parametrize('value', [
        True,
        False
    ])
    def test_get_receivers_list_passes_verify_cert(self, value):
        class WF(object):
            source = GitSource('git', 'foo', provider_params={'git_commit': 'foo'})
        p = SendMailPlugin(None, WF(), pdc_verify_cert=value)
        flexmock(p).should_receive('_get_component_label').and_return('foo')
        flexmock(p).should_receive('_get_pdc_token').and_return('foo')
        flexmock(requests).should_receive('get').with_args(object, headers=object, params=object,
                                                           verify=value).and_raise(RuntimeError)

        with pytest.raises(RuntimeError):
            p._get_receivers_list()

    def test_get_receivers_list_passes_pdc_token(self):
        class WF(object):
            source = GitSource('git', 'foo', provider_params={'git_commit': 'foo'})
        p = SendMailPlugin(None, WF())
        flexmock(p).should_receive('_get_component_label').and_return('foo')
        flexmock(p).should_receive('_get_pdc_token').and_return('thisistoken')
        headers = {'Authorization': 'Token thisistoken'}
        flexmock(requests).should_receive('get').with_args(object, headers=headers, params=object,
                                                           verify=True).and_raise(RuntimeError)

        with pytest.raises(RuntimeError):
            p._get_receivers_list()

    def test_get_receivers_list_request_exception(self):
        class WF(object):
            source = GitSource('git', 'foo', provider_params={'git_commit': 'foo'})
        p = SendMailPlugin(None, WF())
        flexmock(p).should_receive('_get_component_label').and_return('foo')
        flexmock(p).should_receive('_get_pdc_token').and_return('foo')
        flexmock(requests).should_receive('get').and_raise(requests.RequestException('foo'))

        with pytest.raises(RuntimeError) as e:
            p._get_receivers_list()
        assert str(e.value) == 'foo'

    def test_get_receivers_list_wrong_status_code(self):
        class WF(object):
            source = GitSource('git', 'foo', provider_params={'git_commit': 'foo'})
        p = SendMailPlugin(None, WF())
        flexmock(p).should_receive('_get_component_label').and_return('foo')
        flexmock(p).should_receive('_get_pdc_token').and_return('foo')

        class R(object):
            status_code = 404
            text = 'bazinga!'
        flexmock(requests).should_receive('get').and_return(R())

        with pytest.raises(RuntimeError) as e:
            p._get_receivers_list()
        assert str(e.value) == 'PDC returned non-200 status code (404), see referenced build log'

    def test_get_receivers_passes_proper_params(self):
        class WF(object):
            source = GitSource('git', 'foo', provider_params={'git_commit': 'branch'})
        p = SendMailPlugin(None, WF(), pdc_contact_role='role')
        flexmock(p).should_receive('_get_component_label').and_return('component')
        flexmock(p).should_receive('_get_pdc_token').and_return('foo')

        params = {'global_component': 'component', 'dist_git_branch': 'branch', 'role': 'role'}
        flexmock(requests).should_receive('get').with_args(object, headers=object, params=params,
                                                           verify=object).\
            and_raise(requests.RequestException())

        with pytest.raises(RuntimeError):
            p._get_receivers_list()

    @pytest.mark.parametrize('pdc_response, pdc_contact_role, expected', [
        ({'count': 0, 'results': []},
         SendMailPlugin.PDC_CONTACT_ROLE,
         'no {0} role for the component'.format(SendMailPlugin.PDC_CONTACT_ROLE)),
        ({'count': 1, 'results': [{'contact': {'email': 'foo@bar.com'}}]},
         SendMailPlugin.PDC_CONTACT_ROLE,
         ['foo@bar.com']),
        ({'count': 2,
          'results':
            [{'contact': {'email': 'foo@bar.com'}}, {'contact': {'email': 'spam@spam.com'}}]},
         SendMailPlugin.PDC_CONTACT_ROLE,
         ['foo@bar.com', 'spam@spam.com']),
    ])
    def test_get_receivers_pdc_actually_responds(self, pdc_response, pdc_contact_role, expected):
        class WF(object):
            source = GitSource('git', 'foo', provider_params={'git_commit': 'foo'})
        p = SendMailPlugin(None, WF(), pdc_contact_role=pdc_contact_role)
        flexmock(p).should_receive('_get_component_label').and_return('foo')
        flexmock(p).should_receive('_get_pdc_token').and_return('foo')

        class R(object):
            status_code = 200

            def json(self):
                return pdc_response
        flexmock(requests).should_receive('get').and_return(R())

        if isinstance(expected, str):
            with pytest.raises(RuntimeError) as e:
                p._get_receivers_list()
            assert str(e.value) == expected
        else:
            assert p._get_receivers_list() == expected

    def test_send_mail(self):
        p = SendMailPlugin(None, None, from_address='foo@bar.com', smtp_uri='smtp.spam.com')

        class SMTP(object):
            def sendmail(self, from_addr, to, msg):
                pass

            def quit(self):
                pass

        smtp_inst = SMTP()
        flexmock(smtplib).should_receive('SMTP').and_return(smtp_inst)
        flexmock(smtp_inst).should_receive('sendmail').\
            with_args('foo@bar.com', ['spam@spam.com'], str)
        flexmock(smtp_inst).should_receive('quit')
        p._send_mail(['spam@spam.com'], 'subject', 'body')

    def test_run_ok(self):
        class WF(object):
            build_failed = True
            autorebuild_canceled = False
            prebuild_results = {CheckAndSetRebuildPlugin.key: True}
            image = ImageName.parse('repo/name')
        receivers = ['foo@bar.com', 'x@y.com']
        p = SendMailPlugin(None, WF(), send_on=[AF])

        flexmock(p).should_receive('_should_send').with_args(True, False, False).and_return(True)
        flexmock(p).should_receive('_get_receivers_list').and_return(receivers)
        flexmock(p).should_receive('_send_mail').with_args(receivers, six.text_type, six.text_type)

        p.run()

    def test_run_fails_to_obtain_receivers(self):
        class WF(object):
            build_failed = True
            autorebuild_canceled = False
            prebuild_results = {CheckAndSetRebuildPlugin.key: True}
            image = ImageName.parse('repo/name')
        error_addresses = ['error@address.com']
        p = SendMailPlugin(None, WF(), send_on=[AF], error_addresses=error_addresses)

        flexmock(p).should_receive('_should_send').with_args(True, False, False).and_return(True)
        flexmock(p).should_receive('_get_receivers_list').and_raise(RuntimeError())
        flexmock(p).should_receive('_send_mail').with_args(error_addresses, six.text_type,
                                                           six.text_type)

        p.run()

    def test_run_does_nothing_if_conditions_not_met(self):
        class WF(object):
            build_failed = True
            autorebuild_canceled = False
            prebuild_results = {CheckAndSetRebuildPlugin.key: True}
            image = ImageName.parse('repo/name')
        p = SendMailPlugin(None, WF(), send_on=[MS])

        flexmock(p).should_receive('_should_send').with_args(True, False, False).and_return(False)
        flexmock(p).should_receive('_get_receivers_list').times(0)
        flexmock(p).should_receive('_send_mail').times(0)

        p.run()
