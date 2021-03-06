from datetime import timedelta
import logging
from urllib.parse import urlparse

import requests
from requests.exceptions import ConnectTimeout, ReadTimeout
import simplejson

from django.contrib import messages
from django.contrib.auth import logout
from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect
from django.utils import timezone

from standup.auth0.models import IdToken
from standup.auth0.settings import app_settings


logger = logging.getLogger(__name__)


class Auth0LookupError(Exception):
    pass


def renew_id_token(id_token):
    """Renews id token and returns delegation result or None

    :arg str id_token: the id token to renew

    :returns: delegation result (dict) or ``None``

    """
    url = 'https://%s/delegation' % app_settings.AUTH0_DOMAIN
    response = requests.post(url, json={
        'client_id': app_settings.AUTH0_CLIENT_ID,
        'api_type': 'app',
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        'id_token': id_token,
    }, timeout=app_settings.AUTH0_PATIENCE_TIMEOUT)

    try:
        result = response.json()
    except simplejson.JSONDecodeError:
        # This can happen if the response was someting like a 502 error
        return

    # If the response.status_code is not 200, it's still JSON but it won't have a id_token.
    return result.get('id_token')


class ValidateIdToken(object):
    """For users authenticated with an id_token, we need to check that it's still valid. For
    example, the user could have been blocked (e.g. leaving the company) if so we need to ask the
    user to log in again.

    """

    exception_paths = (
        # Exclude the AUTH0_CALLBACK_URL path, otherwise this can loop
        urlparse(app_settings.AUTH0_CALLBACK_URL).path,
    )

    def is_expired(self, request):
        """Returns whether or not the id_token is expired

        For simplicity purposes, this returns False if there is a token and it's
        not expired and True in all other cases.

        :arg request: the Django Request object which has a ``.user`` property with
            the user in question

        :returns: bool--False if not expired and True in all other cases

        """
        try:
            token = IdToken.objects.get(user=request.user)
        except IdToken.DoesNotExist:
            # If there's no id_token, then we treat it as expired.
            return True

        # Return whether the id_token expiration has passed
        return bool(token.expire < timezone.now())

    def update_expiration(self, request):
        """Updates any bookkeeping related to checking expiration

        :arg request: the Django Request object which has a ``.user`` property with
            the user in question

        """
        # Prior to this getting called, the middleware code updates the db record, so this is a
        # no-op
        pass

    def process_request(self, request):
        if (
                request.method != 'POST' and
                # FIXME(willkg): We might want to do this for AJAX, too, otherwise one-page webapps
                # might never renew.
                not request.is_ajax() and
                request.user.is_active and
                request.user.email and
                request.path not in self.exception_paths
        ):
            # Verify their domain is one of the domains we need to look at
            domain = request.user.email.lower().split('@', 1)[1]
            if domain not in app_settings.AUTH0_ID_TOKEN_DOMAINS:
                return

            if not self.is_expired(request):
                return

            # The id_token has expired, so we renew it now
            try:
                token = IdToken.objects.get(user=request.user)

            except IdToken.DoesNotExist:
                # If there is no IdToken then something is weird because they should have one. So
                # log them out and tell them to sign in
                messages.error(
                    request,
                    # FIXME(willkg): This is Mozilla specific.
                    'You can\'t log in with that email address using the provider you '
                    'used. Please log in with the Mozilla LDAP provider.',
                    fail_silently=True
                )
                logout(request)
                return HttpResponseRedirect(reverse(app_settings.AUTH0_SIGNIN_VIEW))

            try:
                new_id_token = renew_id_token(token.id_token)

            except (ConnectTimeout, ReadTimeout):
                # Log the user out because their id_token didn't renew and send them to
                # home page
                messages.error(
                    request,
                    'Unable to validate your authentication with Auth0. This can happen when '
                    'there is temporary network problem. Please sign in again.',
                    fail_silently=True
                )
                logout(request)
                return HttpResponseRedirect(reverse(app_settings.AUTH0_SIGNIN_VIEW))

            if new_id_token:
                # Save new token and we're all set
                token.id_token = new_id_token
                token.expire = timezone.now() + timedelta(seconds=app_settings.AUTH0_ID_TOKEN_EXPIRY)
                token.save()

                self.update_expiration(request)
                logger.debug('ValidateIdToken: token renewed for %s' % request.user)
                return

            # If we don't have a new id_token, then it's not valid anymore. We log the user
            # out and send them to the home page
            messages.error(
                request,
                'Unable to validate your authentication with Auth0. This is most likely due '
                'to an expired authentication session. Please sign in again.',
                fail_silently=True
            )
            logout(request)
            return HttpResponseRedirect(reverse(app_settings.AUTH0_SIGNIN_VIEW))


class ValidateIdTokenUsingCache(ValidateIdToken):
    """Uses cache to check id_token expiration"""
    def is_expired(self, request):
        cache_key = 'auth0:renew_id_token:%s' % request.user.id

        # Look up expiration in cache to see if id_token needs to be renewed
        # FIXME(willkg): Try named cache and if that doesn't exist, fall back to default.
        return bool(cache.get(cache_key))

    def update_expiration(self, request):
        cache_key = 'auth0:renew_id_token:%s' % request.user.id

        cache.set(cache_key, True, app_settings.AUTH0_ID_TOKEN_EXPIRY)
