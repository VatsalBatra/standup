#!/bin/bash -ex

export DJANGO_SETTINGS_MODULE=standup.settings
export DATABASE_URL=sqlite://
export SECRET_KEY=itsasekrit
export STATICFILES_STORAGE=django.contrib.staticfiles.storage.StaticFilesStorage

export AUTH0_CLIENT_ID=ou812
export AUTH0_CLIENT_SECRET=secret_ou812
export AUTH0_DOMAIN=foo
export AUTH0_CALLBACK_URL=http://testserver/auth/login

flake8
py.test $@
