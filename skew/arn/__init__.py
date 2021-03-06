# Copyright 2014 Scopely, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import re

from six.moves import zip_longest
import botocore.session
import jmespath

import skew.resources
from skew.arn.endpoint import Endpoint

LOG = logging.getLogger(__name__)
DebugFmtString = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'


class ARNComponent(object):

    def __init__(self, pattern, arn):
        self.pattern = pattern
        self._arn = arn

    def __repr__(self):
        return self.pattern

    def choices(self, context=None):
        """
        This method is responsible for returning all of the possible
        choices for the value of this component.

        The ``context`` can be a list of values of the components
        that precede this component.  The value of one or more of
        those previous components could affect the possible
        choices for this component.

        If no ``context`` is provided this method should return
        all possible choices.
        """
        return []

    def match(self, pattern, context=None):
        """
        This method returns a (possibly empty) list of strings that
        match the regular expression ``pattern`` provided.  You can
        also provide a ``context`` as described above.

        This method calls ``choices`` to get a list of all possible
        choices and then filters the list by performing a regular
        expression search on each choice using the supplied ``pattern``.
        """
        matches = []
        regex = pattern
        if regex == '*':
            regex = '.*'
        regex = re.compile(regex)
        for choice in self.choices(context):
            if regex.search(choice):
                matches.append(choice)
        return matches

    def matches(self, context=None):
        """
        This is a convenience method to return all possible matches
        filtered by the current value of the ``pattern`` attribute.
        """
        return self.match(self.pattern, context)

    def complete(self, prefix='', context=None):
        return [c for c in self.choices(context) if c.startswith(prefix)]


class Resource(ARNComponent):

    def _split_resource(self, resource):
        if '/' in resource:
            resource_type, resource_id = resource.split('/', 1)
        elif ':' in resource:
            resource_type, resource_id = resource.split(':', 1)
        else:
            resource_type = resource
            resource_id = None
        return (resource_type, resource_id)

    def match(self, pattern, context=None):
        resource_type, _ = self._split_resource(pattern)
        return super(Resource, self).match(resource_type)

    def choices(self, context=None):
        if context:
            service = context[2]
        else:
            service = self._arn.service.pattern
        all_resources = skew.resources.all_types(
            self._arn.provider.pattern, service)
        if not all_resources:
            all_resources = ['*']
        return all_resources

    def _get_botocore_session(self, profile):
        LOG.debug('Getting botocore session')
        session = botocore.session.get_session()
        session.profile = profile
        config = session.get_scoped_config()
        LOG.debug(config)
        if 'role_arn' in config:
            LOG.debug('Using AssumeRole to get actual credentials')
            role_arn = config.get('role_arn')
            source_profile = config.get('source_profile')
            session.profile = source_profile
            sts = session.create_client('sts')
            response = sts.assume_role(
                RoleArn=role_arn, RoleSessionName='skew')
            LOG.debug(response)
            session = botocore.session.get_session()
            session.profile = profile
            session.set_credentials(
                response['Credentials']['AccessKeyId'],
                response['Credentials']['SecretAccessKey'],
                response['Credentials']['SessionToken'])
        return session

    def enumerate(self, context):
        LOG.debug('Resource.enumerate %s', context)
        _, provider, service_name, region, account = context
        profile = self._arn.account.map_account_to_profile(account)
        session = self._get_botocore_session(profile)
        service = session.get_service(service_name)
        endpoint = Endpoint(service, region, account)
        resource_type, resource_id = self._split_resource(self.pattern)
        LOG.debug('resource_type=%s, resource_id=%s',
                  resource_type, resource_id)
        for resource_type in self.matches(context):
            kwargs = {}
            resource_path = '.'.join([provider, service_name, resource_type])
            resource_cls = skew.resources.find_resource_class(resource_path)
            do_client_side_filtering = False
            if resource_id and resource_id != '*':
                # If we are looking for a specific resource and the
                # API provides a way to filter on a specific resource
                # id then let's insert the right parameter to do the filtering.
                # If the API does not support that, we will have to filter
                # after we get all of the results.
                filter_name = resource_cls.Meta.filter_name
                if filter_name:
                    if resource_cls.Meta.filter_type == 'list':
                        kwargs[filter_name] = [resource_id]
                    else:
                        kwargs[filter_name] = resource_id
                else:
                    do_client_side_filtering = True
            enum_op, path, extra_args = resource_cls.Meta.enum_spec
            if extra_args:
                kwargs.update(extra_args)
            data = endpoint.call(enum_op, query=path, **kwargs)
            LOG.debug(data)
            for d in data:
                if do_client_side_filtering:
                    # If the API does not support filtering, the resource
                    # class should provide a filter method that will
                    # return True if the returned data matches the
                    # resource ID we are looking for.
                    if not resource_cls.filter(resource_id, d):
                        continue
                resource = resource_cls(endpoint, d, self._arn.query)
                yield resource


class Account(ARNComponent):

    def __init__(self, pattern, arn):
        self._account_map = self._build_account_map()
        super(Account, self).__init__(pattern, arn)

    def _build_account_map(self):
        """
        Builds up a dictionary mapping account IDs to profile names.
        Any profile which includes an ``account_name`` variable is
        included.
        """
        session = botocore.session.get_session()
        account_map = {}
        for profile in session.available_profiles:
            # For some reason botocore is returning a _path value
            # in the call to available_profiles.  Its value is a
            # a string file path but we are interested only in
            # the profiles.
            if not profile.startswith('_'):
                session.profile = profile
                config = session.get_scoped_config()
                account_id = config.get('account_id')
                if account_id:
                    account_map[account_id] = profile
        return account_map

    def choices(self, context=None):
        return list(self._account_map.keys())

    def map_account_to_profile(self, account):
        return self._account_map[account]

    def enumerate(self, context):
        LOG.debug('Account.enumerate %s', context)
        for match in self.matches(context):
            context.append(match)
            for resource in self._arn.resource.enumerate(context):
                yield resource
            context.pop()


class Region(ARNComponent):

    _region_names_limited = ['us-east-1',
                             'us-west-2',
                             'eu-west-1',
                             'ap-southeast-1',
                             'ap-southeast-2',
                             'ap-northeast-1']

    _all_region_names = ['us-east-1',
                         'us-west-1',
                         'us-west-2',
                         'eu-west-1',
                         'eu-central-1',
                         'ap-southeast-1',
                         'ap-southeast-2',
                         'ap-northeast-1',
                         'sa-east-1']

    _service_region_map = {
        'redshift': _region_names_limited,
        'glacier': _region_names_limited,
        'kinesis': _region_names_limited}

    def choices(self, context=None):
        if context:
            service = context[2]
        else:
            service = self._arn.service
        return self._service_region_map.get(
            service, self._all_region_names)

    def enumerate(self, context):
        LOG.debug('Region.enumerate %s', context)
        for match in self.matches(context):
            context.append(match)
            for account in self._arn.account.enumerate(context):
                yield account
            context.pop()


class Service(ARNComponent):

    def choices(self, context=None):
        if context:
            provider = context[1]
        else:
            provider = self._arn.provider.pattern
        return skew.resources.all_services(provider)

    def enumerate(self, context):
        LOG.debug('Service.enumerate %s', context)
        for match in self.matches(context):
            context.append(match)
            for region in self._arn.region.enumerate(context):
                yield region
            context.pop()


class Provider(ARNComponent):

    def choices(self, context=None):
        return ['aws']

    def enumerate(self, context):
        LOG.debug('Provider.enumerate %s', context)
        for match in self.matches(context):
            context.append(match)
            for service in self._arn.service.enumerate(context):
                yield service
            context.pop()


class Scheme(ARNComponent):

    def choices(self, context=None):
        return ['arn']

    def enumerate(self, context):
        LOG.debug('Scheme.enumerate %s', context)
        for match in self.matches(context):
            context.append(match)
            for provider in self._arn.provider.enumerate(context):
                yield provider
            context.pop()


class ARN(object):

    ComponentClasses = [Scheme, Provider, Service, Region, Account, Resource]

    def __init__(self, arn_string='arn:aws:*:*:*:*'):
        self.query = None
        self._components = None
        self._build_components_from_string(arn_string)

    def __repr__(self):
        return ':'.join([str(c) for c in self._components])

    def debug(self):
        self.set_logger('skew', logging.DEBUG)

    def set_logger(self, logger_name, level=logging.DEBUG):
        """
        Convenience function to quickly configure full debug output
        to go to the console.
        """
        log = logging.getLogger(logger_name)
        log.setLevel(level)

        ch = logging.StreamHandler(None)
        ch.setLevel(level)

        # create formatter
        formatter = logging.Formatter(DebugFmtString)

        # add formatter to ch
        ch.setFormatter(formatter)

        # add ch to logger
        log.addHandler(ch)

    def _build_components_from_string(self, arn_string):
        if '|' in arn_string:
            arn_string, query = arn_string.split('|')
            self.query = jmespath.compile(query)
        pairs = zip_longest(
            self.ComponentClasses, arn_string.split(':', 6), fillvalue='*')
        self._components = [c(n, self) for c, n in pairs]

    @property
    def scheme(self):
        return self._components[0]

    @property
    def provider(self):
        return self._components[1]

    @property
    def service(self):
        return self._components[2]

    @property
    def region(self):
        return self._components[3]

    @property
    def account(self):
        return self._components[4]

    @property
    def resource(self):
        return self._components[5]

    def __iter__(self):
        context = []
        for scheme in self.scheme.enumerate(context):
            yield scheme
