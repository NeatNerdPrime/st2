# Licensed to the StackStorm, Inc ('StackStorm') under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import functools
from collections import namedtuple
import six
import sys
import traceback

import jsonschema
import routes
from swagger_spec_validator.validator20 import validate_spec, deref
from webob import exc, Request, Response

from st2common.exceptions import auth as auth_exc
from st2common import hooks
from st2common import log as logging
from st2common.util.jsonify import json_encode

LOG = logging.getLogger(__name__)


def op_resolver(op_id):
    module_name, func_name = op_id.split(':', 1)
    __import__(module_name)
    module = sys.modules[module_name]
    return functools.reduce(getattr, func_name.split('.'), module)


def abort_unauthorized(msg=None):
    raise exc.HTTPUnauthorized('Unauthorized - %s' % msg if msg else 'Unauthorized')


class NotFoundException(Exception):
    pass


class ErrorHandlingMiddleware(object):
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        try:
            try:
                resp = self.app(environ, start_response)
            except NotFoundException:
                raise exc.HTTPNotFound()
        except Exception as e:
            # Mostly hacking to avoid making changes to the hook
            State = namedtuple('State', 'response')
            Response = namedtuple('Response', 'status headers')

            state = State(
                response=Response(
                    status=getattr(e, 'code', 500),
                    headers={}
                )
            )

            if hasattr(e, 'detail') and not getattr(e, 'comment'):
                setattr(e, 'comment', getattr(e, 'detail'))

            resp = hooks.JSONErrorResponseHook().on_error(state, e)(environ, start_response)
        return resp


class Router(object):
    def __init__(self, arguments=None, debug=False):
        self.debug = debug

        self.arguments = arguments or {}

        self.spec = {}
        self.spec_resolver = None
        self.routes = routes.Mapper()

    def add_spec(self, spec, default=True):
        info = spec.get('info', {})
        LOG.debug('Adding API: %s %s', info.get('title', 'untitled'), info.get('version', '0.0.0'))

        self.spec_resolver = validate_spec(copy.deepcopy(spec))
        self.spec = spec

        for (path, methods) in six.iteritems(spec['paths']):
            for (method, endpoint) in six.iteritems(methods):
                conditions = {
                    'method': [method.upper()]
                }
                m = self.routes.submapper(_api_path=path, _api_method=method, conditions=conditions)
                m.connect(None, self.spec.get('basePath', '') + path)
                if default:
                    m.connect(None, path)

        for route in self.routes.matchlist:
            LOG.debug('Route registered: %s %s', route.routepath, route.conditions)

    def __call__(self, req):
        """Invoke router as a view."""
        match = self.routes.match(req.path, req.environ)

        if match is None:
            raise NotFoundException

        # To account for situation when match may return multiple values
        try:
            path_vars = match[0]
        except KeyError:
            path_vars = match

        path = path_vars.pop('_api_path')
        method = path_vars.pop('_api_method')
        endpoint = self.spec['paths'][path][method]

        # Handle security
        user = None

        if 'security' in endpoint:
            security = endpoint.get('security')
        else:
            security = self.spec.get('security', [])

        if security:
            try:
                security_definitions = self.spec.get('securityDefinitions', {})
                for statement in security:
                    declaration, options = statement.copy().popitem()
                    definition = security_definitions[declaration]

                    if definition['type'] == 'apiKey':
                        if definition['in'] == 'header':
                            token = req.headers.get(definition['name'])
                        elif definition['in'] == 'query':
                            token = req.GET.get(definition['name'])
                        else:
                            token = None

                        if token:
                            auth_func = op_resolver(definition['x-operationId'])
                            auth_resp = auth_func(token)

                            user = auth_resp.user

                if not user:
                    raise auth_exc.NoAuthSourceProvidedError('One of Token or API key required.')
            except (auth_exc.NoAuthSourceProvidedError,
                    auth_exc.MultipleAuthSourcesError) as e:
                LOG.error(str(e))
                return abort_unauthorized(str(e))
            except auth_exc.TokenNotProvidedError as e:
                LOG.exception('Token is not provided.')
                return abort_unauthorized(str(e))
            except auth_exc.TokenNotFoundError as e:
                LOG.exception('Token is not found.')
                return abort_unauthorized(str(e))
            except auth_exc.TokenExpiredError as e:
                LOG.exception('Token has expired.')
                return abort_unauthorized(str(e))
            except auth_exc.ApiKeyNotProvidedError as e:
                LOG.exception('API key is not provided.')
                return abort_unauthorized(str(e))
            except auth_exc.ApiKeyNotFoundError as e:
                LOG.exception('API key is not found.')
                return abort_unauthorized(str(e))
            except auth_exc.ApiKeyDisabledError as e:
                LOG.exception('API key is disabled.')
                return abort_unauthorized(str(e))

        # Collect parameters
        kw = {}
        for param in endpoint.get('parameters', []) + endpoint.get('x-parameters', []):
            name = param['name']
            type = param['in']
            required = param.get('required', False)

            if type == 'query':
                kw[name] = req.GET.get(name)
            elif type == 'path':
                kw[name] = path_vars[name]
            elif type == 'header':
                kw[name] = req.headers.get(name)
            elif type == 'body':
                if req.body:
                    data = req.json
                    try:
                        jsonschema.validate(data, deref(param['schema'], self.spec_resolver))
                    except (jsonschema.ValidationError, ValueError) as e:
                        raise exc.HTTPBadRequest(detail=e.message,
                                                 comment=traceback.format_exc())

                    class Body(object):
                        def __init__(self, **entries):
                            self.__dict__.update(entries)

                    kw[name] = Body(**data)
                else:
                    kw[name] = None
            elif type == 'formData':
                kw[name] = req.POST.get(name)
            elif type == 'environ':
                kw[name] = req.environ.get(name.upper())

            if required and not kw[name]:
                detail = 'Required parameter "%s" is missing' % name
                raise exc.HTTPBadRequest(detail=detail)

        # Call the controller
        func = op_resolver(endpoint['operationId'])
        resp = func(**kw)

        # Handle response
        if resp is not None:
            if not hasattr(resp, '__call__'):
                resp = Response(json_encode(resp), content_type='application/json')

            return resp

    def as_wsgi(self, environ, start_response):
        """Invoke router as an wsgi application."""
        req = Request(environ)
        resp = self(req)
        return resp(environ, start_response)